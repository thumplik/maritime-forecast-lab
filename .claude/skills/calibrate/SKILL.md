---
name: calibrate
description: Run the §6.6 evaluator calibration (paper-compatible constant velocity on the P-paper protocol vs the calibration paper's golden numbers) and interpret the result.
---

1. Confirm canonical data exists: `ls data/processed/canonical/`. If not, run `make canonical` (and `make fetch` before it if raw data is missing).
2. Run `make calibrate`. It writes `data/runs/calibration_<timestamp>/` with `summary.json`.
3. Compare to the golden table in `docs/build-plan.md` §6.6 (CV: 3.69 / 9.03 / 15.33 km at 1 / 2 / 3 h). Tolerance: ±10% per horizon.
4. If outside tolerance, it is a **blocker**. Investigate in this order and record each check in the implementation log:
   - error statistic (mean vs median) and units;
   - window construction: 10-min grid, 18 context + 18 target steps, 30-min gap split, 3 h spacing of test windows;
   - filters: Class A/B, coordinate box, stationary-jitter removal, minimum speed;
   - CV anchor: last report's SOG/COG vs last-two-points velocity;
   - vessel-disjoint split proportions and seed.
5. Record the result with `/log-progress`, and update the calibration section of `docs/coverage-audit.md`.
