# Coverage audit — Milestone 0

**Runs:** `coverage_audit_20261020T024624Z` and `calibration_20261020T024511Z`, both at commit `725d401`.
**Dataset:** `noaa_us_gulf_2023-06`: NOAA MarineCadastre, 2023-06-01 to 2023-06-30, filtered to the U.S. Gulf truth region (manifest `data/manifests/noaa_us_gulf_2023-06-01_2023-06-30.json`).
**Reproduce:** `make fetch && make canonical && make audit && make calibrate`. The full per-class tables are in each run's `report.md`. This page summarises them.

## 1. What the data looks like

| | Cutoff region (Houston–Galveston) | Truth region (U.S. Gulf) |
|---|---|---|
| Reports | 25.6 M | 87.4 M |
| Vessels | 3,171 | 9,832 |

- **Reporting cadence** in the cutoff region: median 70 s, p90 180 s, p99 545 s. Only 0.2% of intervals are longer than 30 min. Pleasure craft report least often (p90 659 s, 1.5% of intervals > 30 min). Tankers and cargo ships cluster at about 180 s.
- **Mostly stationary:** 70.5% of reports have a trailing 10-min mean SOG below 0.5 kn, 4.4% are slow (0.5–2 kn) and 25.1% are underway. Uniform sampling would be dominated by stationary vessels, which confirms §6.3.
- **Tugs and towing dominate:** 906 vessels and 60% of reports. They make up 58% of underway cutoffs. Tankers are next with 12%.
- **Feed outages:** 13 hours in June had less than 25% of the median Gulf-wide report volume. The worst was 2023-06-11 13:00 UTC at 1.5%. Other outages hit 06-03, 06-06, 06-10, 06-15, 06-19, 06-21, 06-24 and 06-25. These are system-wide gaps, not vessel behaviour.

## 2. Ground-truth coverage (underway cutoffs)

Sampler: §6.3 defaults (`docs/cutoff-sampling.md`), seed 20230601. It produced 41,985 underway cutoffs (2,821 vessels) and 66,481 stationary cutoffs. Cutoffs are limited to `as_of ≤ dataset end − 24 h`, so "missing truth" never means "the dataset ended".

| Horizon | 1 h | 2 h | 3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|---|---|
| Valid truth, Gulf truth region | 93.0% | 91.2% | 90.8% | 88.9% | 86.0% | 86.6% |
| Valid truth, cutoff box only | 89.8% | 86.7% | 85.3% | 81.2% | 76.0% | 75.5% |

The pooled numbers hide a lot of spread between classes. Gulf / box-only:

| Class | 6 h | 12 h | 24 h |
|---|---|---|---|
| tug_tow (n=23,882) | 94.0 / 89.2% | 93.0 / 86.1% | 94.3 / 84.3% |
| tanker (n=4,746) | 91.9 / 73.7% | 84.7 / 64.0% | 76.2 / 62.9% |
| cargo (n=1,626) | 88.9 / 67.6% | 82.0 / 55.1% | 77.4 / 54.4% |
| fishing (n=1,334) | 73.1 / 52.7% | 70.0 / 43.6% | 66.0 / 37.1% |
| pleasure_sail (n=1,476) | 47.0 / 45.0% | 38.0 / 35.4% | 39.2 / 36.9% |

- The larger truth region does its job (§0 item 4). For cargo at 24 h it raises coverage from 54% to 77%.
- **Survivorship (pooled):** the median cutoff SOG of evaluable samples stays at 5.4–5.5 kn at every horizon. No class share moves by more than 5 percentage points. Long-horizon evaluable sets are therefore *not* dominated by slow vessels, but only because tugs, which stay in coverage, are most of the population.

## 3. Horizon admission (§6.4)

Rule as implemented: at least 50% valid truth, evaluable median SOG no more than 25% below the underway population's median, and no class share shifting by more than 15 pp.

| Horizon | Pooled status |
|---|---|
| 1, 2, 3, 6 h | **first-class** |
| 12, 24 h | **first-class** under the pooled rule. **Recommended: first-class per stratum only** (see §6) |
| 48, 72 h | not assessed: needs data beyond terrestrial coverage (Milestone 8) |

The pooled rule passes every horizon because tugs dominate it. For the classes where long horizons are interesting (cargo, tanker), coverage at 12–24 h is 76–85%, which still clears 50%. Pleasure craft fail at ≥ 6 h (47%) and fishing is marginal. Recommendation: apply the rule **per vessel-class stratum**. A horizon is first-class for a stratum only if it passes in that stratum.

## 4. Baseline errors (underway; median km, 95% vessel-bootstrap CI)

| Horizon | Persistence | Constant velocity | CV mean |
|---|---|---|---|
| 1 h | 5.40 [4.99, 5.82] | 5.24 [5.07, 5.41] | 6.72 |
| 2 h | 6.81 [6.24, 7.45] | 12.83 [12.49, 13.16] | 15.25 |
| 3 h | 7.52 [6.84, 8.27] | 21.47 [20.95, 22.06] | 24.44 |
| 6 h | 9.00 [8.18, 9.94] | 49.21 [48.19, 50.29] | 53.74 |
| 12 h | 10.75 [9.83, 12.05] | 109.0 [107.0, 111.1] | 115.9 |
| 24 h | 12.93 [11.52, 14.45] | 230.2 [225.8, 235.0] | 246.9 |

**Persistence beats constant velocity from 2 h onwards on the pooled underway set.** Most underway cutoffs in the port are short harbour moves: tugs, ferries and service boats that soon stop or turn around. CV wins only for vessels that keep going:

| Class, median km | 1 h persistence / CV | 3 h persistence / CV | 6 h persistence / CV |
|---|---|---|---|
| cargo | 19.0 / **3.9** | 53.1 / **21.7** | 86.0 / **57.2** |
| tanker | 17.7 / **4.4** | 46.0 / **23.9** | 62.8 / 61.8 (tie) |
| tug_tow | **3.4** / 5.3 | **4.5** / 20.5 | **5.7** / 46.1 |
| passenger | **3.6** / 10.1 | **4.0** / 40.7 | **4.1** / 92.4 |

Along-track and cross-track CV errors are about equal at every horizon (for example 24.9 / 24.5 km median at 6 h). CV errs on route about as much as on speed.

Stationary set: both baselines have a median error near 0 km. CV's *mean* error grows to 6.4 km at 6 h and 29 km at 24 h, because some vessels depart. Departure timing is the whole problem for this set.

## 5. Calibration (§6.6)

**Result: PASS.** On the pre-declared primary variant, paper-compatible constant velocity is within ±10% of the published numbers at every horizon:

| | 1 h | 2 h | 3 h |
|---|---|---|---|
| Paper (CV, deterministic) | 3.69 km | 9.03 km | 15.33 km |
| Ours, primary (mean, 95% CI) | 4.03 [3.45, 4.82] | 9.65 [8.51, 11.15] | 16.16 [14.36, 18.41] |
| Relative | +9% | +7% | +5% |

The paper's CV numbers fall inside our CIs at all three horizons. The test split has 3,777 windows from 431 vessels.

### Reconstruction assumptions

The paper's manifests were not obtained. These choices were declared in `scripts/calibrate.py` before the numbers were seen:

1. **Region:** the Houston–Galveston box. Data are filtered to the box before segmentation, so vessels leaving the box end their segment.
2. **Class A/B filter:** `TransceiverClass ∈ {A, B}`.
3. **Stationary jitter removal:** drop reports with SOG < 0.5 kn (or missing SOG) *before* segmentation.
4. **Segmentation:** a new segment after a gap of more than 30 min, plus single-point spike removal (> 50 kn implied both ways).
5. **Grid:** 10-min, clock-aligned, linear interpolation inside segments only.
6. **Windows:** 18 context + 18 target steps, stride 18 (3 h spacing), full windows only.
7. **Vessel-disjoint split:** 20% of MMSIs as test, by SHA-256 hash.
8. **CV anchor:** SOG and COG of the latest raw report at or before the last context step, extrapolated on the WGS84 geodesic.
9. **Statistic:** mean displacement error at steps 6, 12 and 18.

### Sensitivity (all variants are in the run report)

- **Jitter removal is decisive.** Without it, the windows are mostly moored vessels and CV error comes out about 63% too low (1.42 km at 1 h).
- Using last-two-grid-points velocity instead of the anchor report gives +1 / −2 / −3%, even closer to the paper.
- Using all vessels instead of the 20% test split gives +6 / +4 / +3%.
- The **median** is about 40% below the paper at every horizon, so the paper reports means.

## 6. Surprises and recommended plan changes

1. **Persistence is the baseline to beat in port traffic.** §6.5's success condition ("outperform CV *and* persistence") is much harder against persistence than CV beyond 1 h. Proposal: add a **damped-velocity** baseline (speed decays toward 0 with a time constant fit on training data) to Milestone 2. It is cheap and likely beats both baselines on the pooled set, so it raises the bar for retrieval honestly.
2. **Apply the §6.4 admission rule per stratum** (vessel class × motion state). The pooled rule is satisfied by tugs alone.
3. **Add a "transiting" stratum.** Use cargo, tanker and passenger-ship cutoffs whose last 30 min are underway, and report it as a headline stratum alongside the pooled numbers. This is where route retrieval should matter, and pooled metrics hide it.
4. **Distinguish feed outages from vessel gaps.** Add a `feed_outage` not-evaluable reason when the bracketing gap overlaps an hour below 25% of median system volume.
5. **Calibration criterion:** keep the ±10% point check, and also record whether the paper's value lies inside our vessel-bootstrap CI. At 1 h the point estimate passes with little margin (+9%) and the CI is wide.
6. **Source data quirks:** NOAA daily CSVs contain malformed lines (360 on 06-10, 4 on 06-13). `fetch_noaa.py` now skips and counts them per day (filter v2). The first 28 days were fetched with v1, which would have failed on any malformed line, so they had none.
7. The truth region's northern edge (31°N) includes the lower Mississippi up to Baton Rouge. This is harmless for Houston cutoffs but should be noted if the cutoff region ever moves east.

## 7. Smallest next slice (Milestone 1)

Add Pydantic models for the canonical observation and the split manifest. Generate a **P-unseen** manifest (chronological + MMSI-hash vessel-disjoint) and a **P-paper** manifest from the calibration reconstruction above. Add tests that prove the manifests are disjoint on their stated axes.
