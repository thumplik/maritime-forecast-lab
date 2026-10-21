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


_Retrieval, conformal, Timestral and TrAISformer sections are added in Milestones 3–7._
