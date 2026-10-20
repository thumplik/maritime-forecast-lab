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

Membership: `manifest.split_of(vessel, t[, lat, lon])` for a point and `manifest.window_split(vessel, start, end[, positions])` for a context-plus-label window. A window whose ends fall in different splits or in a purge gap is excluded. Under P-region the rule uses context positions: test membership is never decided by where a vessel goes after the cutoff, since that would select on the future. Train/calibration windows also require their label positions on the train side.

The P-unseen calibration split is small: 4 days and 164 vessels in the cutoff region. Per-stratum conformal radii (Milestone 4) may need pooled strata or a longer period.

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

## Calibration (§6.6)

`make calibrate` → paper-compatible constant velocity on P-paper vs the published table, ±10% tolerance. Assumptions made in reconstructing P-paper are listed in [coverage-audit.md](coverage-audit.md#calibration).
