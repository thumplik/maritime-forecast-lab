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
| P-region | Not yet defined | — |

Membership: `manifest.split_of(vessel, t)` for a point and `manifest.window_split(vessel, start, end)` for a context-plus-label window. A window whose two ends fall in different splits, or in a purge gap, is excluded.

The P-unseen calibration split is small: 4 days and 164 vessels in the cutoff region. Per-stratum conformal radii (Milestone 4) may need pooled strata or a longer period.

## Metrics (§6.5)

Geodesic displacement error (km, nmi) per horizon; ADE/FDE; along/cross-track; median, p90, mean with paired bootstrap 95% CI resampling vessels. Paired: identical (cutoff, horizon) set for every model in a comparison.

## Calibration (§6.6)

`make calibrate` → paper-compatible constant velocity on P-paper vs the published table, ±10% tolerance. Assumptions made in reconstructing P-paper are listed in [coverage-audit.md](coverage-audit.md#calibration).
