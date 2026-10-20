# Model integration notes

One section per model: source, pinned version, adaptation differences from the original, known limitations.

## Persistence (`persistence_v1`)
Last observed position held constant. No adaptation.

## Constant velocity (`constant_velocity_v1`, `constant_velocity_paper_v1`)
- Standard: geodesic direct problem from the last valid report using its SOG and COG.
- Paper-compatible: velocity from the last two points of the regularized 10-min context grid (see coverage-audit.md, calibration assumptions), extrapolated on the geodesic.
- Limitation: ignores turns, channel geometry and land.

_Retrieval, conformal, Timestral and TrAISformer sections are added in Milestones 3–7._
