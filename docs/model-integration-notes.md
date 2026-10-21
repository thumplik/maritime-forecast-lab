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

_Conformal, Timestral and TrAISformer sections are added in Milestones 4–7._
