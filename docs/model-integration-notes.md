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

_Retrieval, conformal, Timestral and TrAISformer sections are added in Milestones 3–7._
