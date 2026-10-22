# Model integration notes

One section per model: source, pinned version, adaptation differences from the original, known limitations.

All models implement `models.base.TrajectoryForecaster` and are registered in `models/registry.py`.

They are built by `factory(store, dataset_id)` and receive only an `AISHistory`, which rejects any row after `as_of`, built by `history_from_store()` over `ParquetStore.history()`. Unsupported requests, failures, exceptions and non-finite outputs all come back as typed results with no points. `reads_beyond_own_history` has no default and must be declared. Every registered model must pass the leakage-invariance harness (`evaluation/invariance.py`). The unit test runs it on the synthetic fixture; the golden test runs it on real NOAA cutoffs.

## Persistence (`persistence_v1`)
Holds the last reported position. It warns if that report is more than 30 min older than `as_of`.

## Constant velocity, standard (`constant_velocity_v1`)
- Anchors on the most recent report with usable SOG/COG. Zero SOG needs no COG.
- Extrapolates SOG × lead along COG on the WGS84 geodesic. Lead is measured **from the anchor report's time**, so stale reports are respected (§5).
- Warns when it falls back to an earlier report or the anchor is more than 30 min old.
- Reports flagged `sog_implausible` (> 60 kn) are not usable anchors.
- Typed failure `no_usable_sog_cog` if no report qualifies.

## Constant velocity, paper-compatible (`constant_velocity_paper_v1`)
- Uses the **last** report's SOG/COG and extrapolates from `as_of`, treating that report as current. There is no fallback: missing SOG/COG gives the typed failure `anchor_missing_sog_cog`. It warns when that report is more than 30 min old.
- Difference from `scripts/calibrate.py`: the §6.6 reconstruction starts from the *interpolated 10-min grid position* at the anchor time. That position uses the next raw report, which is non-causal, so it is P-paper only. The adapter starts from the last raw report's position, the causal equivalent.
- On product cutoffs, which are sampled at real report times, both start at the same point. The adapter is for comparison against the paper's numbers, not for headline results.

Limitation of both CV modes: no turns, channel geometry or land.

`constant_velocity_paper_v1` is **calibration-only** (`calibration_only=True`). It is left out of product benchmarks, `product_model_ids()` and headline tables, because its typed failures on stationary cutoffs (last report without SOG/COG) would make whole cells non-headline.

## Damped velocity (`damped_velocity_v1`)
- **Model:** v(t) = SOG·e^(−t/τ) along COG from the standard-CV anchor, with lead measured from the anchor time. Distance saturates at SOG·τ. τ → ∞ is CV; τ → 0 is persistence.
- **τ per vessel class:** fitted on the protocol's **train split only** (`models/fit_damped.py`, `make fit PROTOCOL=...`).
  - Fit data: train cutoffs (underway sampler, at most 3,000 per class), causal histories, truth from the truth region.
  - Objective: the mean over 1/2/3/6/12/24 h of the median error.
  - Grid: 0.02 h to 48 h, plus ∞.
  - Classes with fewer than 300 cutoffs use the pooled `default`. Classes whose best τ is the smallest grid value are listed in `at_grid_boundary`.
- **Provenance:** fits are committed (`data/model_params/damped_velocity_v1/<protocol>__<dataset>.json`) with the manifest SHA-256. The model refuses a fit made for a different manifest and is then `not_fitted` / `installed=false`. The benchmark runner refuses uninstalled models up front.
- **Capabilities:** `reads_beyond_own_history=True` (τ comes from other vessels' training data), `uses_store_at_predict=False`.
- **Protocol-leakage tests** (`test_fit_damped.py`): perturbing test-period data (P-known) or test-vessel data (P-unseen) leaves the fit identical, and perturbing train data changes it.
- `vessel_class` is NOAA back-filled, not as-of.
- June 2023 fits:

| Protocol | cargo | tanker | fishing | service | tug_tow | other | passenger | pleasure_sail | default |
|---|---|---|---|---|---|---|---|---|---|
| P-known | 3 | 2 | 0.5 | 0.15 | 0.15 | 0.05 | 0.05 | 0.02 (boundary) | 0.25 |
| P-unseen | 3 | 2 | 0.5 | 0.15 | 0.15 | 0.1 | 0.02 (boundary) | 0.1 | 0.25 |

  All values are τ in hours. Large ships keep moving; harbour craft effectively stop.

## Constant turn rate (`constant_turn_rate_v1`)
- **Turn rate:** the median wrapped ΔCOG/Δt over reports in the 10 min up to the anchor, with COG present and SOG ≥ 2 kn.
- **Cap and decay:** capped at 0.05 °/s, decaying as e^(−t/20 min). Speed is the anchor SOG.
- **Integration:** a local east-north frame in 30 s steps, mapped back with the geodesic direct problem (distance and azimuth from the anchor are exact, as in AEQD).
- **Parameters:** untuned, fixed defaults. No data beyond the target's history is used.
- **Result:** on June 2023 it is slightly worse than CV at every horizon and stratum. Holding the recent turn does not help on average at these horizons. It is kept as a reference, and tuning on train is a candidate for later.


## Historical-route retrieval (`route_retrieval_v1`, §5 R1)
- **Pool:** the model builds the segment index itself, from the store, using only reports at or before `until = pool_until(as_of)`, then applies `eligible()` (see [evaluation-protocol.md](evaluation-protocol.md#retrieval-eligibility-5-r1-62-milestone-3)).
  - Under the default **frozen** pool, `until` is the end of the train period (2023-06-17). One build serves every calibration and test cutoff: about 50–60 s and roughly 9 GB peak for June 2023.
  - The pool is cached per process, keyed by store, dataset, partition files (size and mtime), bound, manifest hash and index config, behind a lock.
  - A golden test checks that this pool equals the prebuilt `make index` output filtered by `eligible()`.
- **Matching:** every 10-min track edge passing within 1 km of the anchor (the latest report; 3 km if none is within 1 km) is a candidate alignment. The anchor is projected onto the edge, which gives a continuous reference time τ. The match distance is
  `D = (offset/500 m)² + (recent shape/500 m)² + (course diff/30°)² [both ≥ 1 kn] + (speed diff/3 kn)² + 1 [different class]`.
  - The recent shape compares the last 30 min of 10-min displacements, from the target's own reports, never interpolated across a gap.
  - The best edge is kept per segment, then the top 10 segments, with at most 3 per vessel.
  - A reference must continue for at least 60 min (or the whole horizon, if shorter) after τ.
  - Lookup is a 0.05° cell grid plus an equirectangular prefilter, used only to discard edges out of reach. Scoring is done in the exact local frame.
- **Continuation:** each reference is translated so its point at τ sits on the anchor, in the azimuthal-equidistant frame centred on the anchor (`geo.geodesic.LocalFrame`), and followed forward in time. There is no rotation.
  - When a reference's segment ends (a coverage gap or the end of its data), its last position is held. `reference_ends_at` records when, and a warning says so for the central path.
- **Central prediction:** the similarity-weighted medoid of the candidates that cover at least half of the best candidate's span. It is always a real path, so the two branches of a junction are never averaged into the bank between them.
- **Output:** `trajectories` holds the K candidates, each with its `similarity = exp(−D/2)` (**not a probability**) and `reference_segment_ids` (provenance). `central_trajectory_id` names the one used for `points`.
- **Failure:** with no eligible segment within 3 km, the forecast fails with `no_references`. It never falls back silently to a baseline.
- **Defaults** were fixed a priori. The only check before the test benchmark was a 600-cutoff diagnostic on the P-known **calibration** split; nothing was tuned on test.
- **Variants:** `pool="rolling"` and `index_days` are diagnostic only (`config.diagnostic_only`). The benchmark runner refuses them, because a rolling index overlaps calibration and test (§5 U1).
- **Back-filled class:** vessel class is NOAA back-filled for both the target and the references (`config.class_source = "backfilled"`).
- **Known limitations:**
  - **Moored and anchored vessels:** a nearby reference that later departed drags the forecast away. Persistence is better for the stationary set (see the benchmark).
  - **Long horizons:** most references end within hours (they leave coverage), and holding their last position makes 24 h forecasts persistence-like.
  - The antimeridian is not handled in the cell lookup (Gulf data only).
  - **P-region:** references must lie wholly beyond the 50 km buffer, so retrieval has no reference near any test-side cutoff and fails (`no_references`) on all of them. Measured: 19,843 of 19,843 June P-region cutoffs (`docs/benchmarks.md`). It does not transfer to new waters.
  - Under P-known with the API's deployment protocol, an `as_of` inside the train period rebuilds the pool for that `as_of`, which takes about 1 min.

## Historical-route retrieval v2 (`route_retrieval_v2`)
- **What it is:** v1 plus a hold for targets stationary at the cutoff (the sampler's rule: 10-min mean SOG < 0.5 kn). Such a target keeps its last position at every target time. That is **persistence**, labelled as such (`config.stationary_policy`, the warning, the description, and `central_trajectory_id = null`). Candidate trajectories are still returned for inspection but do not inform the points.
- **Why it holds unconditionally:** this was decided on the P-known dev window after the failure review found false departures were the stationary loss. Following the references off a mooring, at any release threshold tried (0.5–0.9 of the reference weight moved), was worse than holding. The references carry no departure-timing skill here.
- **No references nearby:** the forecast still fails (`no_references`) rather than holding, so v2 has the same coverage as v1.
- **Options in the code, off in v2:** `speed_match` (follow references at the target's pace; mixed on dev) and a partial hold (`depart_share` < 1).

_Conformal, Timestral and TrAISformer sections are added in Milestones 4–7._


## Timestral zero-shot, positions (`timestral_pos_v1`, §5 T1-pos)
- **Source and pins:** [research/adapters/timestral/README.md](../research/adapters/timestral/README.md). The package is at commit `a534bdb`; the Timestral-28M weights are at revision `49b2640`. Both are optional (`uv sync --extra timestral`, `make timestral-weights`). Without them the model reports `installed=false`, and every request gets the typed error `not_installed`.
- **Representation:** the vessel's own east/north positions, in metres, in the local AEQD frame centred on the last report. That is a two-variate series on a regular grid at the request's step, ending at `as_of` and spanning the request's history.
  - A grid time takes a value only if it falls on a report, or between two of the target's reports at most 30 min apart (linear in the frame). Otherwise it is NaN, which Timestral treats as missing. Gaps are never bridged.
  - A stale `as_of` leaves its own grid point empty.
- **Point forecast:** the median of the per-axis quantiles, mapped back with the frame's exact inverse.
  - `clip_nonnegative=False`; the package default would pin south-west tracks to the anchor.
  - `symmetric_averaging=True`.
  - It warns when the path reaches more than 300 km from the frame centre.
- **No uncertainty served:** Timestral's quantiles are per-axis marginals and are never combined into a 2-D region (AGENTS.md). Calibrated radii would come from conformal (U1), once the model has been benchmarked on a calibration window.
- **Requirements:** at least 2 reports and 6 observed grid steps (1 h at 10 min). Otherwise the typed error `insufficient_context`. Steps 1–60 min; horizons up to 72 h, always one decode block (at most 1,024 steps).
- **`reads_beyond_own_history=True`:** the weights are fitted parameters from an external corpus that is not disclosed. The model card says "mostly synthetic", so public AIS, including NOAA 2023, cannot be ruled out.
  - The caveat is in the model's `config["pretraining"]`, so it travels with every run record.
  - P-unseen is vessel-disjoint only with respect to *our* splits.
  - The risk is low but not zero: the input is relative east/north metres around the last report, and 28M parameters are a generic model.
- **Package pin enforced:** a `timestral` install from a different commit counts as not installed, and the error names the commit found.
- **`optional=True`:** it is never in the default benchmark model set (`product_model_ids()`), so a default run is the same on every machine. Benchmark it by naming it.
- **Tests:**
  - CI uses a labelled linear stand-in backend (`tests/fixtures/fake_timestral.py`) to check the grid, gap handling, call arguments, direction (south-west) and the invariance harness.
  - The stand-in adds a negligible term that depends on every context value, so the invariance harness can see any change to the context.
  - Tests marked `timestral` (`uv run pytest -m timestral`) run the real checkpoint: determinism, direction, no state carried between forecasts on the shared backend, and invariance on the fixture.
- **Runtime:** about 1.7 s for the first call on CPU (including the load). Per-forecast timing is in the benchmark record.
- **Expectation (§5 T1):** it has no route knowledge, so it should behave roughly like a smoothed kinematic extrapolator. That is to be tested, not assumed.


## Timestral zero-shot, velocity (`timestral_vel_v1`, §5 T1-vel)
Same adapter, pins, context grid, flags and caveats as T1-pos. Two things differ:
- **Series:** east/north velocity (m/s) between consecutive context grid points. It is missing wherever either point is missing, so a gap never yields a velocity. The series is one step shorter than the position grid.
- **Positions:** the median velocity forecast is summed step by step from the position at `as_of` in the local AEQD frame, then mapped back with the exact inverse. Distances and bearings from the centre are exact; the path is a sum of straight steps in the plane.
  - With no report **exactly at** `as_of`, there is nothing to integrate from, so it returns the typed error `stale_anchor` rather than guessing the missing interval. (A bracket would need a report after `as_of`.) Benchmark cutoffs are report times, so this never removes a benchmark sample. Through the API, any off-report `as_of` gets `stale_anchor` while the other models forecast. The workbench's cutoff scrubber only offers report times.
