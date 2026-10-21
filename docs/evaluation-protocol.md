# Evaluation protocol

Normative source: build plan §6. This file records the concrete, implemented choices and is updated as the evaluator is built (Milestone 2).

## Replay (§6.1)

1. Sampler picks `(vessel_id, as_of)` at a real observation time.
2. Context = that vessel's observations with `observed_at <= as_of` (and nothing else unless the protocol permits it).
3. Target grid = `as_of + k·step` for k = 1..H.
4. Truth at each target time = linear interpolation between the two real observations that bracket it, **only if** both are within the truth region, belong to the same segment, and the bracketing gap ≤ the gap threshold (30 min). Otherwise `not_evaluable` with a reason.
5. Store per-sample, per-horizon records; never drop a failure.

`not_evaluable` reasons: `no_truth_after_cutoff`, `truth_gap`, `left_truth_region`, `segment_ended`, `insufficient_history`, `model_failed`.

## Protocols (§6.2)

Each protocol is a manifest: a rule in `maritime_forecast/evaluation/splits.py` (model: `schemas/manifest.py`), materialized per dataset by `scripts/make_splits.py` into `data/manifests/splits/<protocol>__<dataset>.json`, which is committed.

| Id | Rule | June 2023 (`noaa_us_gulf_2023-06`) |
|---|---|---|
| P-known | Chronological: train [06-01, 06-17), calibration [06-18, 06-22), test [06-23, 07-01). The 24 h purge gaps between periods are unused | Vessels may appear in every split |
| P-unseen | Same periods **and** vessels assigned by `sha256("p-unseen-v1" + MMSI) mod 1000`: train [0, 700), calibration [700, 800), test [800, 1000). A sample counts only where the time split and the vessel split agree | 1,711 / 164 / 430 vessels seen in the cutoff region for train / calibration / test |
| P-paper | Vessel-only: `sha256(MMSI) mod 1000` < 200 → test. Preprocessing parameters are recorded in the manifest. Calibration only | 666 test vessels seen in the cutoff region |
| P-region | Geographic cut plus the same periods. Test side = Houston–Galveston box (`config/regions.yaml`). A 50 km buffer band around it is excluded. Train side = beyond the buffer. The test side pairs only with the test period, the train side with the train/calibration periods. Membership is decided by the window's *context* positions (all on one side, none in the buffer). Test labels may go anywhere and are scored against truth in the truth region. Train/calibration labels must stay on the train side | test 2,066 vessels / 6.83 M reports; train 6,994 / 31.38 M; calibration 4,610 / 7.87 M |

**P-region benchmarks** (`runner.select_cutoffs`, `region_window_ok`):
- The test split samples cutoffs in the cutoff region (the test side). Other splits, such as the damped-velocity fit, sample anywhere in the truth region on the train side.
- The sampler runs on candidates whose cutoff report is on the required side and whose window is in the split's period. Windows are then dropped if any context report leaves that side, or, outside test, if any label report enters the buffer or the test side.
  - The label window runs through the last target plus the 30 min truth bracket.
  - The run records the effective sampling region and the number of sampled cutoffs dropped (`sampler.sampling_region`, `sampler.region_dropped`).
- Test membership therefore depends only on context positions.
- The navigable mask has no P-region fit, so feasibility is not scored under P-region.

Membership: `manifest.split_of(vessel, t[, lat, lon])` for a point and `manifest.window_split(vessel, start, end[, positions])` for a context-plus-label window. A window whose ends fall in different splits or in a purge gap is excluded. Under P-region the rule uses context positions: test membership is never decided by where a vessel goes after the cutoff, since that would select on the future. Train/calibration windows also require their label positions on the train side.

The P-unseen calibration split is small: 4 days and 164 vessels in the cutoff region. Per-stratum conformal radii (Milestone 4) may need pooled strata or a longer period.

### June + July 2023 (`noaa_us_gulf_2023-06-07`, manifests `splits-v2`)

Added 2026-10-21, after the Milestone 3 failure review read the June test results. From then on, the June test period is no longer an untouched held-out set for retrieval (see [failure-review.md](failure-review.md#test-set-use)).

| Split | Period | Use |
|---|---|---|
| train | [06-01, 06-17) | Unchanged: fitted parameters, navigable mask, and the frozen retrieval index period |
| dev | [06-18, 06-22) | Model development and tuning (the old calibration period; only one 600-cutoff retrieval diagnostic ever looked at it) |
| calibration | [06-23, 07-01) | Milestone 4 conformal radii. Under P-known this is the old June test, which informed the failure review, so it is never reported as held-out |
| test | [07-02, 07-31) | **Fresh and untouched.** The held-out set for any model developed after the review. It ends Jul 31 00:00 because the data end on Jul 31 |

- **Purges:** 24 h between all periods (06-17, 06-22, 07-01). Benchmarks of dev or calibration cap truth at their period's end.
- **P-unseen:** the same salt, so the train and test vessels are exactly those of the June manifests. The old calibration bucket [700, 800) is split into dev [700, 750) and calibration [750, 800).
  - Its calibration split (Jun 23–30 × bucket [750, 800)) is **not** the old June test: those vessel-days were excluded under v1 and never read. The old June test vessels have no split for those days.
  - Its July test vessels **are** the vessels the June failure review examined (same buckets). July is fresh in time, not in vessels, and this is stated next to any P-unseen July result.
  - Dev and calibration are 5% of vessels each (78 and about 105 in the cutoff region). Decisions are taken on P-known dev; P-unseen dev is a sanity check and is reported with its sample counts.
- **Reading benchmarks:**
  - `make benchmark PROTOCOL=… DS=noaa_us_gulf_2023-06-07 SPLIT=dev` is for development.
  - `SPLIT=test` is the July held-out result. Runs on the June-only dataset stay valid as the record of what the June test showed, but are not compared as held-out against later models.
- **Rule:** a model version is benchmarked on the July test only once its design is frozen on dev. This is **enforced** (`evaluation/holdout.py`):
  - `run_benchmark.py --confirm-test` and a clean committed tree are required.
  - Each run is appended, before scoring, to the committed ledger `data/manifests/test_access/noaa_us_gulf_2023-06-07.json`, with run id, git SHA, model versions and config hashes, and `--informed-by`.
  - The same model version and config cannot be scored on July twice without `--test-rerun-reason`.
  - The API refuses benchmarks on this dataset and forecast evaluation whose targets reach into July. `failure_review.py` refuses July runs unless `--allow-holdout` is given, after the results are reported.
  - Non-test runs carry the split in their run id (`…_dev_…`) and report header.
- **Conformal (Milestone 4):** radii fitted on the calibration split are judged by their **empirical coverage on July**, per stratum and per July week. Calibration and July are not exchangeable for retrieval: the frozen pool ends Jun 17, so calibration cutoffs are 6–14 days past it and July cutoffs are 15–44 days past it.

## Leakage invariance (§10, Milestone 2 done-when)

`evaluation/invariance.py` runs every registered model on each cutoff, both on scoped data and on copies where every report after `as_of`, **for every vessel**, has been perturbed. The perturbations are:

- `delete_future`;
- `shift_future` (positions, SOG and COG altered);
- spikes at `as_of` + 1 µs, + 1 ms and + 1 s (boundary probes);
- `flood_future`.

Forecasts must be identical (ignoring `runtime_ms`) and deterministic across repeat calls and fresh instances. Each check runs at horizon/step grids (1 h, 1 min), (6 h, 10 min) and (24 h, 60 min).

Safeguards in the harness itself:
- Models are built by `factory(store, dataset_id)` with the scoped store. While models run, opening the real archive or the store under test is blocked (`parquet_store.BLOCKED_ROOTS`).
- Perturbed runs execute first, each on a fresh instance.
- Self-checks: a perturbation may not alter rows at or before `as_of`, and it must change the target's future. Otherwise the case is reported as `vacuous`, never counted as a pass. The report counts how many comparisons were against `ok` forecasts.
- Models that declare `reads_beyond_own_history` get the whole dataset as scope.

Controls in the unit tests:
- a model that peeks at the store past `as_of` is caught;
- a model that opens the archive by path is blocked;
- a perturbation that edits the past is rejected.

**Known limitations:**
- A *process-global* cache keyed without the data version would hide a leak. It needs process isolation; a strict `xfail` test documents this, and the new-forecaster checklist forbids such caches.
- The harness does not detect protocol leakage (P-unseen exclusion, index period, parameters fitted on test). Those need their own tests when the models that could commit them arrive.

Runs:
- Unit: 7 synthetic cutoffs.
- Golden (`make calibrate`): 6 real NOAA cutoffs (underway, stationary, transiting) × 3 grids × 3 models = 378 checks. No violations, no vacuous cases, at least 80% `ok` comparisons, 2 min 42 s.

## Runner (Milestone 2)

`evaluation/runner.py` (`make benchmark PROTOCOL=...`) runs these steps:

1. **Cutoff candidates** in the cutoff region, from causal trailing features only. No spike filter, because it looks at the next report.
2. **Split filter:** keep a candidate only if the manifest puts its whole `[as_of − history, as_of + max horizon]` window in the test split, with no other period inside it.
3. **Sampling:** the seeded §6.3 sampler (underway and stationary sets), plus the transiting flag.
4. **Prediction:** batched causal histories, proven equal to `ParquetStore.history()`.
5. **Truth:** at every grid step, from the truth region. For along/cross-track, the true course is the bracket bearing; when a report falls exactly on the target, it is that report's COG if the vessel is moving.

Paired scoring and failures:
- A sample counts only if truth is evaluable and every model delivered.
- Failures are counted per model. `not_evaluable` is reported by horizon, including `model_failed`.
- (stratum, horizon) cells with fewer than 20 evaluable samples are listed in `dropped_cells`, never dropped silently.

Each cell reports, per model: median and mean error with per-model vessel-bootstrap CIs from shared resamples, p90, |along|/|cross| medians with `n_decomposed`, ADE, failure rate, admission, and a cross-run-comparable `sample_set_sha256`.

Skill against the best baseline uses a **paired** bootstrap CI. It is computed only if every registered baseline is in the run, and it is null if the best baseline's error is under 10 m.

**Headline** is a property of the cell. It requires all of:
- the horizon is admitted for that stratum;
- every model's failure rate is at most 1%;
- the paired set has at least 100 samples from at least 20 vessels.

Runs go to the immutable run store; failed runs are recorded too. Results are in [benchmarks.md](benchmarks.md).

## Metrics (§6.5)

Geodesic displacement error (km, nmi) per horizon; ADE/FDE; along/cross-track; median, p90, mean with paired bootstrap 95% CI resampling vessels. Paired: identical (cutoff, horizon) set for every model in a comparison.

## Feasibility (§6.5)

Forecasts are never constrained to water. Their paths are flagged afterwards (`evaluation/feasibility.py`), and per cell the runner reports the share of **paired** samples flagged anywhere up to the horizon. Flags of a model that failed are null, never "feasible".

| Flag | Rule |
|---|---|
| land point | A forecast grid point is infeasible in the navigable mask |
| land crossing | The move from the previous point passes through an infeasible cell. The anchor report is the previous point for step 1. The move is sampled every half cell along a straight lon/lat line, a documented approximation over one grid step |
| implausible speed | Implied speed of a move > 60 kn (the canonical `sog_implausible` bound) |
| implausible acceleration | Implied speed changes by > 2 kn/min from the previous move. For step 1 the comparison is with the anchor's reported SOG |

**Navigable mask** (`geo/navigable_mask.py`, `make fetch-land`, `make mask PROTOCOL=...`):

- **Land** is OSM land polygons (`land-polygons-split-4326`, ODbL) rasterized at cell centres on a 0.001° grid (≈ 100 m) over the truth region. The raster matches exact point-in-polygon tests on 99.94% of random points; the rest are boundary cells. Natural Earth is never used.
- **Water is not only the sea.** OSM land is everything that is not sea, so ship channels, rivers and lakes behind the coastline count as land. On their own, the polygons put 74% of stationary and 50% of underway real reports in Houston–Galveston on land.
- **Traffic cells** are therefore removed from land: cells where at least `min_vessels` distinct vessels reported during the protocol's **train** split. Under P-unseen only train-assigned vessels count. Quality-flagged reports are excluded, and the cells are then dilated by `dilate_cells`. Fit only on protocol-permitted data (§6.2). The sidecar in `data/manifests/masks/` records the manifest SHA-256, the split, the periods and the mask checksum.
- **Parameters were chosen on each protocol's calibration split**, never test, with `scripts/tune_navigable_mask.py`. For each candidate, the mask is fitted on train and scored on 500 k sampled calibration reports in Houston–Galveston (seed 1). Under P-unseen these are calibration-assigned vessels only. The table shows the share of real reports called infeasible, and the share of OSM land still flagged:

  | min_vessels | dilate | P-known stationary | P-known underway | P-unseen stationary | P-unseen underway | land still flagged |
  |---|---|---|---|---|---|---|
  | — (OSM only) | — | 73.9% | 50.7% | 75.3% | 52.1% | 100% |
  | 1 | 1 | 0.03% | 0.02% | 0.22% | 0.17% | 99.1–99.2% |
  | **2** | **1** | **0.56%** | **0.10%** | **0.58%** | **0.75%** | **99.4%** |
  | 2 | 2 | 0.43% | 0.09% | 0.22% | 0.73% | 99.1% |
  | 3 | 1 | 1.27% | 0.29% | 3.53% | 0.90% | 99.5% |

  Underway error is higher under P-unseen because unseen vessels use places train traffic never visited.
  2 vessels rather than 1 keeps one stray transponder (for example, one carried on a truck) from opening a road.
- **The mask's own error is reported in every cell** as `truth_on_land_rate`. It is computed the same way as the model rates: the share of paired samples whose true positions, at any evaluable step up to the horizon, the mask puts on land. A model's land rate near that floor carries no information.
- The runner refuses a mask fitted for another manifest, or on the split being scored, before a run is created. With no mask, land flags are null and the run carries a warning.
- The API's `POST /forecasts/evaluation` uses the deployment protocol's (P-known) mask. When `as_of` falls inside that train split, land flags are **null**, with a warning: the mask's traffic could include this vessel's own track after `as_of` (its truth). Speed and acceleration flags are still reported.
- The runner refuses any mask not fitted on the train split.

## Retrieval eligibility (§5 R1, §6.2; Milestone 3)

`data/segment_index.py` (`make index`) indexes every completed track segment of a dataset once: 240,798 segments and 16.0 M ten-minute track points for June 2023, built in about 2 min. What a forecast may use is decided per cutoff by `eligible(index, as_of, target, manifest, pool)`.

| Rule | Why |
|---|---|
| `known_at = end + 2 × gap ≤ as_of` | One gap to know the segment ended. One more for a spike report inside that gap: whether it is dropped depends on its next report. Segmentation runs with `causal_ends=True`, so the spike test never looks across a gap |
| Frozen pool (default): also `known_at ≤ end of the train period` | The retrieval index period is the training period, disjoint from calibration and test (§5 U1). No test sample's truth can be another's reference |
| Rolling pool (`pool="rolling"`): any segment known complete before `as_of` | Causal; what a deployed system has. Reported only as a labelled variant |
| P-known: every vessel, including the target's own earlier voyages | §6.2 "known vessel" |
| Vessel-disjoint (P-unseen): train-assigned vessels only, never the target | §6.2 "unseen vessel". With the frozen pool this is exactly the manifest's training data |
| P-region: only segments lying wholly on the train side (bounding box clear of the test region grown by the buffer), from any vessel | Region-disjoint transfer: no reference from the test waters or the buffer band. Segments that pass through the buffer are dropped whole, conservatively. Interpolated track points stay inside the reports' box. The diagnostic rolling pool also admits train-side segments from the test period, which the manifest assigns to no split; the frozen default does not |

Tracks are interpolated only between reports of one segment, whose internal gaps are at most 30 min. Vessel class is the segment's most frequent non-null value, with ties going to the smallest name: the same rule as `history_vessel_class`. It is back-filled by NOAA and flagged by `static_fields_provenance`.

**Tests:**
- **Equivalence:** the eligible segments from the full archive equal those from an index built only from reports at or before the cutoff. This is checked on the synthetic fixture plus adversarial tracks, and on real data for both protocols and both pools.
- **Teeth:** the equivalence breaks with the naive `end + gap` rule, and it breaks without `causal_ends`.
- **Pool:** a real test-period cutoff only sees segments known by the train end, and under P-unseen only train vessels, never the target.
- **Forecaster** (`route_retrieval_v1`):
  - Every reference used by 150 real test cutoffs per protocol was known by the train end; under P-unseen every reference came from a train vessel other than the target (golden).
  - Perturbing or deleting other vessels' test-period reports *before* the cutoff leaves a frozen-pool forecast byte-identical (unit). The rolling pool changes, which shows the test has teeth.
  - The invariance harness (post-cutoff perturbations) passes on synthetic and real cutoffs.
  - The benchmark runner refuses rolling or truncated pools (`diagnostic_only`).

## Calibration (§6.6)

`make calibrate` → paper-compatible constant velocity on P-paper vs the published table, ±10% tolerance. Assumptions made in reconstructing P-paper are listed in [coverage-audit.md](coverage-audit.md#calibration).
