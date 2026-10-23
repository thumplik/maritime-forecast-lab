# Benchmarks

Benchmark runs live in `data/runs/benchmarks/<run_id>/` and are immutable: files are read-only after finalize, and `files.json` holds SHA-256 checksums of every file, `run.json` included. The `files.json` digest below lets anyone check that a run has not been edited. Reproduce with `make benchmark PROTOCOL=...`; the same commit, seed and data give identical results.

## 2026-10-20 — B0 baselines, June 2023, Houston–Galveston cutoffs (commit 293c67a)

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06_20261020T213231Z` | `P-unseen_noaa_us_gulf_2023-06_20261020T222302Z` |
| files.json SHA-256 | `1c8e7b47ba9f04e972ddcfcbc108200a33c7e61f42867982491b9803ac99c962` | `ac7b8dc5464f8f7ec059a53199e026994bd50b2c19d703a16e7e269655e361a3` |
| Cutoffs (underway + stationary) | 22,639 | 4,977 |
| Cells scored / dropped (too few evaluable) | 381 / 11 | 375 / 1 |
| Wall time (as run) | 50 min | 3.5 min |

Setup:
- **Test period:** the manifest's test period (2023-06-23 → 07-01). A cutoff's whole 24 h history and 24 h horizon must lie inside it, so cutoffs fall on 06-24 → 06-30.
- **Sampling:** seed 20230601, §6.3 sampler defaults.
- **Forecasts:** 10-min grid. Truth comes from the U.S. Gulf truth region and is never bridged across gaps longer than 30 min.
- **Scoring:** paired. A sample counts only if truth is evaluable and every model delivered.
- **Uncertainty:** 1,000 vessel-bootstrap resamples, shared across models.

### Reproduction check (2026-10-21, runner speed-up in commit 55a1b1d)

Both runs were repeated after the runner speed-up and came out **identical**: every cell, dropped cell and not-evaluable count, and every row of `cutoffs.parquet` and `records.parquet` (407,502 rows for P-known). Only run id, timestamp, git SHA and timings differ.

| | P-known | P-unseen |
|---|---|---|
| Re-run id | `P-known_noaa_us_gulf_2023-06_20261021T010233Z` | `P-unseen_noaa_us_gulf_2023-06_20261021T010354Z` |
| files.json SHA-256 | `4e10a20ce107a15f58e789d5572f847d3186d4fc7173cae3123b2211010f10bb` | `d57d8f8313e73200db002c4eb7d0ac95a2900ec87609db2dc0e50186c8621fd9` |
| Wall time | **66 s** (was 50 min) | **21 s** (was 3.5 min) |

The slowdown was not the bootstrap, as first assumed. It was an ASOF join over a registered Arrow object; see the implementation log.

### Headline: underway (pooled), median error km [95% CI]

| Horizon | P-known persistence | P-known CV | P-unseen persistence | P-unseen CV |
|---|---|---|---|---|
| 1 h | 5.71 [5.11, 6.31] | **5.12** [4.91, 5.35] | **4.25** [3.45, 5.39] | 5.36 [4.81, 5.92] |
| 2 h | **7.30** [6.47, 8.16] | 12.87 [12.40, 13.35] | **5.18** [3.84, 6.71] | 13.32 [12.12, 14.56] |
| 3 h | **8.09** | 21.86 | **5.58** | 22.16 |
| 6 h | **9.28** | 50.34 | **6.65** | 50.03 |
| 12 h | **11.09** | 110.72 | **8.10** | 108.72 |
| 24 h | **13.21** | 231.74 | **9.47** | 228.57 |

Paired skill against the best baseline:
- At 1 h, P-known persistence is −0.11 [−0.25, +0.02] and P-unseen CV is −0.26 [−0.63, +0.06]. **Statistically tied at 1 h.**
- From 2 h on, CV's skill CI excludes 0 in both protocols. For example, at 2 h it is −0.76 [−1.02, −0.56] (P-known) and −1.57 [−2.55, −0.90] (P-unseen).

### Headline: transiting stratum (cargo / tanker / passenger, underway for 30 min), median km

| Horizon | P-known persistence | P-known CV | Paired skill of the loser |
|---|---|---|---|
| 1 h | 18.99 | **4.22** | persistence −3.50 [−4.04, −3.06] |
| 2 h | 35.26 | **13.20** | persistence −1.67 [−1.88, −1.40] |
| 3 h | 47.85 | **25.17** | persistence −0.90 [−1.08, −0.72] |
| 6 h | **61.98** | 66.36 | CV −0.07 [−0.23, +0.05] (tie) |
| 12 h | **54.30** | 175.65 | CV −2.24 [−2.70, −1.84] |
| 24 h | **53.25** | 410.23 | CV −6.70 [−8.11, −5.87] |

P-unseen shows the same pattern (n = 212–258 per horizon; CV wins 1–3 h, tie at 6 h, persistence wins from 12 h).

### What this says

1. **The bar for any learned model is "best of persistence and CV per stratum and horizon", and it is not low.** In port traffic, persistence wins after 1 h. For ships in transit, CV wins up to 3 h.
2. Neither baseline is good at 6–24 h for transiting ships (50–400 km). That is the gap route retrieval (Milestone 3) and damped velocity are meant to close.
3. P-known and P-unseen agree within CIs for these baselines, which learn nothing. Any later difference between the protocols will come from what a model learns about specific vessels.
4. `constant_velocity_v1` and `constant_velocity_paper_v1` are identical on paired samples, by construction: cutoffs are at report times, and the modes differ only when the last report lacks SOG/COG, where the paper mode fails.
   - The paper mode's 1.2–2.0% failures on stationary cutoffs make every stationary cell non-headline, because headline is a cell property.
   - **Recommendation:** keep the paper mode for §6.6 calibration only, and leave it out of product benchmarks.

### Not evaluable (P-known, all cutoffs, by horizon)

At 1 h: 1,613 truth gaps, 119 cutoffs with no report after, and 274 model failures. At 24 h: 2,066, 706 and 257. Full tables are in each run's `report.md`.

Caveats:
- `vessel_class` (strata, transiting flag) is NOAA back-filled, not as-of.
- Along/cross-track medians cover only samples with a defined true course (`n_decomposed`, about 80–95% of paired samples). Samples where the vessel is stationary at the target have no along/cross split.
- Headline cells tolerate model failure rates up to 1%. The rate is reported per model.

## 2026-10-21 — B0 with damped velocity and constant turn rate (commit a3022ac)

Paper-mode CV is no longer in product benchmarks (`calibration_only`; it stays in `make calibrate`). Models: `persistence_v1`, `constant_velocity_v1`, `damped_velocity_v1` (τ per class, fitted on each protocol's **train** split, see `docs/model-integration-notes.md`), and `constant_turn_rate_v1`. Setup is otherwise as above: same test period, sampler, seed and cutoffs (22,639 / 4,977).

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06_20261021T021006Z` | `P-unseen_noaa_us_gulf_2023-06_20261021T021141Z` |
| files.json SHA-256 | `ad2f13675f6428a4784a8c2675c8c5c20ada09059339d6fafd09aefa9433cdf0` | `a2055ead489f9e5f5e6a2562fd0b94d723e392ae2a856b75c48aee2f1c0fcbd0` |
| Cells scored / dropped / headline | 508 / 11 / 444 | 500 / 1 / 232 |
| Wall time | 95 s | 27 s |

These runs were made after the slice-3 leakage-review fixes and a refit from a clean tree. They match the pre-fix runs cell for cell.

### Headline: underway (pooled), median error km

| Horizon | Persistence | CV | Damped | CTR | Damped skill vs best baseline |
|---|---|---|---|---|---|
| **P-known** (n ≈ 7,400–8,000, 1,381–1,533 vessels) |
| 1 h | 5.66 | 5.12 | **4.28** [3.97, 4.54] | 5.86 | best (persistence −0.32 [−0.39, −0.26]) |
| 2 h | 7.25 | 12.87 | **6.87** | 14.21 | best (persistence −0.05 [−0.09, −0.01]) |
| 3 h | **8.04** | 21.84 | 8.20 | 23.45 | −0.02 [−0.06, +0.01] (tie) |
| 6 h | **9.22** | 50.24 | 10.04 | 52.15 | −0.09 [−0.14, −0.05] |
| 12 h | **11.05** | 110.60 | 12.06 | 114.08 | −0.09 [−0.15, −0.05] |
| 24 h | **13.14** | 231.42 | 14.11 | 234.30 | −0.07 [−0.11, −0.04] |
| **P-unseen** (n ≈ 1,650–1,760, 291–327 vessels) |
| 1 h | 4.22 | 5.33 | **3.49** [3.06, 4.10] | 6.01 | best (persistence −0.21 [−0.32, −0.10]) |
| 2 h | **5.17** | 13.16 | 5.21 | 14.58 | −0.01 [−0.12, +0.06] (tie) |
| 3 h | **5.57** | 22.04 | 5.80 | 23.67 | −0.04 [−0.16, +0.05] (tie) |
| 6 h | **6.62** | 49.87 | 7.06 | 51.75 | −0.07 [−0.21, −0.00] |
| 12 h | **8.08** | 108.38 | 8.68 | 111.13 | −0.07 [−0.17, −0.00] |
| 24 h | **9.43** | 228.43 | 9.98 | 229.91 | −0.06 [−0.19, +0.00] |

### Headline: transiting stratum, median error km

| Horizon | Persistence | CV | Damped | CTR | Best | Damped skill vs best |
|---|---|---|---|---|---|---|
| **P-known** (n = 1,069–1,394, 379–451 vessels) |
| 1 h | 18.99 | **4.22** | 5.35 | 5.27 | CV | −0.27 [−0.39, −0.17] |
| 2 h | 35.23 | **13.22** | 15.39 | 15.07 | CV | −0.16 [−0.24, −0.07] |
| 3 h | 47.82 | **25.21** | 25.33 | 27.99 | CV | −0.00 [−0.10, +0.08] (tie) |
| 6 h | 61.95 | 66.41 | **37.33** | 69.94 | damped | persistence −0.66, CV −0.78 |
| 12 h | 54.30 | 175.65 | **35.76** | 179.61 | damped | persistence −0.52 [−0.61, −0.40] |
| 24 h | 53.20 | 409.86 | **37.06** | 413.58 | damped | persistence −0.44 [−0.53, −0.28] |
| **P-unseen** (n = 212–258, 72–89 vessels) |
| 1 h | 18.55 | **5.05** | 5.39 | 6.20 | CV | −0.07 [−0.28, +0.07] (tie) |
| 2 h | 34.11 | **14.25** | 15.43 | 16.32 | CV | −0.08 [−0.34, +0.12] (tie) |
| 3 h | 47.99 | **24.63** | 24.83 | 28.06 | CV | −0.01 [−0.33, +0.18] (tie) |
| 6 h | 57.97 | 64.12 | **33.48** | 66.38 | damped | persistence −0.73 [−0.94, −0.41] |
| 12 h | 54.85 | 173.41 | **32.72** | 180.13 | damped | persistence −0.68 [−0.85, −0.39] |
| 24 h | 52.76 | 409.16 | **32.25** | 409.86 | damped | persistence −0.64 [−0.87, −0.22] |

Stationary cells are all headline now that paper-mode CV (and its 1–2% failures) is gone. Persistence is best there (median 0.23–0.27 km at 24 h; damped 0.33–0.34; CV 0.9–1.1). Skill is null where the best baseline's median is under 10 m (1–6 h).

### What this says

1. **Damped velocity is the new bar for ships in transit beyond 3 h.** It cuts the 6–24 h transiting error to about 33–37 km, from 53–62 km for persistence, with CIs that exclude zero in both protocols. Up to 3 h, CV is still best or tied.
2. **In pooled port traffic, damped velocity only wins at 1 h** (and at 2 h under P-known). From 3 h onward persistence is slightly better (skill −0.02 to −0.09). τ is one value per class and is chosen mostly by the long-horizon errors, so it under-damps short harbour moves.
3. **Constant turn rate is slightly worse than CV everywhere.** Holding a turn rate adds error. It stays as a registered baseline, but it is not in the "best baseline" set.
4. The best-baseline reference for any later model is now **per stratum and horizon, from {persistence, CV, damped}**. For transiting ships it is CV up to 3 h and damped velocity from 6 h.

Caveats:
- `vessel_class` (strata and the per-class τ) is NOAA back-filled, not as-of.
- τ was fitted on underway train cutoffs only, but it is applied to stationary cutoffs too. Stationary vessels have SOG ≈ 0, so the effect is small.
- The fit objective is the mean of median errors over 1–24 h, so the 12–24 h errors dominate it.
- Fitted τ is refused for any `as_of` before the end of the train period (`fit_overlaps_as_of`) and for any other manifest.

## 2026-10-21 — Feasibility flags against the navigable-water mask

Same models, cutoffs, seed and test period as the previous section. (Two earlier runs of this slice, `…033802Z` / `…034013Z`, used a non-cumulative truth floor; the leakage review caught it and they were deleted.) **Every error metric, record and cutoff is identical to `…021006Z` / `…021141Z`** (checked cell by cell). What is new is feasibility per cell, a GeoJSON export, and horizon positions in `records.parquet`.

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06_20261021T034634Z` | `P-unseen_noaa_us_gulf_2023-06_20261021T034836Z` |
| files.json SHA-256 | `7ee914bff5198d0a3b86f36dcd46ff0db133de938c0cab6e2718fb5a2c839cf5` | `dc300c21a8089dd95b4ae3d713f1d396f2c0ddb717e57b0cb457dbddae1a3740` |
| Mask | `osm_land_us_gulf@e8224df7b334+traffic[P-known:train,>=2v,dilate1]` | `…+traffic[P-unseen:train,>=2v,dilate1]` |
| Wall time | 120 s (95 s before feasibility) | 33 s |

The mask is OSM land minus cells used by at least 2 vessels in that protocol's **train** split (see `docs/evaluation-protocol.md` § Feasibility). Its own error is the `truth` column below: the share of paired samples whose TRUE positions, at any evaluable step up to the horizon, it puts on land. This is cumulative, like the model rates. Under P-known it grows from 1.4% at 1 h to 8.1% at 24 h for pooled underway vessels, and from 0.8% to 3.7% for transiting ones. Under P-unseen it reaches 8.6% and 4.2% at 24 h. A model land rate near that floor means nothing.

### Share of paired forecasts with a point on infeasible land, anywhere up to the horizon

| Horizon | truth | Persistence | CV | Damped | CTR |
|---|---|---|---|---|---|
| **P-known, underway pooled** |
| 1 h | 1.4% | 0.3% | 61.3% | 26.0% | 64.1% |
| 6 h | 3.6% | 0.3% | 80.7% | 28.9% | 81.0% |
| 24 h | 8.1% | 0.3% | 88.1% | 29.2% | 88.0% |
| **P-known, transiting** |
| 1 h | 0.8% | 0.0% | 29.5% | 21.3% | 30.1% |
| 6 h | 1.8% | 0.0% | 58.1% | 33.2% | 58.6% |
| 24 h | 3.7% | 0.0% | 83.1% | 37.6% | 83.0% |
| **P-unseen, transiting** |
| 1 h | 0.8% | 0.0% | 34.5% | 27.1% | 34.9% |
| 6 h | 2.8% | 0.0% | 62.8% | 36.8% | 64.4% |
| 24 h | 4.2% | 0.0% | 85.8% | 41.0% | 86.3% |

Path crossings (the move between grid points passes over land) run 1–4 points higher than land points. Implausible speed (> 60 kn) is 0.0% everywhere. Implausible acceleration (> 2 kn/min) is 1.1–1.3% for persistence (an instant stop from about 20 kn), 0.2% for damped velocity, and 0.0% for CV and CTR. Full tables are in each run's `report.md`.

### What this says

1. **Straight-line extrapolation leaves the water within an hour in port.** Most pooled underway cutoffs are in the Houston Ship Channel and its branches, and CV or CTR puts 61–64% of them on land by 1 h. Even for ships in transit, 30–35% are on land by 1 h, and 83–86% by 24 h.
2. **Damped velocity's lower error is partly that it stops before reaching land** (26–41% on land). It still crosses land often. Its long-horizon win is not route knowledge.
3. **Persistence is feasible by construction** (0.0–0.3%, below the mask's own floor). A stationary point is tested once, while the truth floor counts any of many steps. It is also the best baseline in pooled port traffic from 3 h onward (previous section).
4. This is the measured gap a route-aware model (Milestone 3 retrieval) has to close: keep CV's short-horizon accuracy without leaving the channel.

GeoJSON of the paired transiting stratum is written by `make benchmark` to `data/runs/exports/<run_id>/transiting.geojson` (7.9 MB / 1.5 MB). Use `scripts/export_geojson.py` for other filters.

## 2026-10-21 — R1 `route_retrieval_v1` vs B0, June 2023 (Milestone 3)

Same setup as above (test period, seed 20230601, §6.3 sampler, 10-min grid, paired, 1,000 vessel-bootstrap resamples). Retrieval uses the frozen pool: segments known complete by the end of the train period (2023-06-17), and under P-unseen, train-assigned vessels other than the target. Code: the commit that adds `models/route_retrieval.py` (runs made on a working tree based on `9477ff0`, which the run records as its git SHA).

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06_20261021T152618Z` | `P-unseen_noaa_us_gulf_2023-06_20261021T153929Z` |
| files.json SHA-256 | `7c1ec7c0f28335a0fdf14a9dffcd9fcbe2199043ed1bbde6b6b07a0d421d841b` | `66322275b3ea3043361dafd26958bc0a4f57a10f75ebc4eddaa633718534c14c` |
| Cutoffs (underway + stationary) | 22,639 | 4,977 |
| Wall time | 767 s (pool build ~60 s, ~25 ms per retrieval forecast) | 252 s |
| Retrieval failures (`no_references`) | ≤ 0.1% per cell | 0.3–0.6% per cell |

All baseline numbers match the earlier runs on the cells they share. Retrieval's few failures remove those samples from every model's paired set.

**Paired skill of retrieval against the best baseline per cell** (1 − retrieval / best-baseline median error; 95% vessel-bootstrap CI):

| Stratum | Protocol | 1 h | 2 h | 3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|---|---|---|
| Transiting | P-known | +0.62 [0.57, 0.66] | +0.74 [0.71, 0.77] | +0.77 [0.73, 0.79] | +0.66 [0.59, 0.70] | +0.42 [0.33, 0.48] | +0.31 [0.20, 0.43] |
| Transiting | P-unseen | +0.69 [0.62, 0.75] | +0.71 [0.61, 0.78] | +0.71 [0.60, 0.80] | +0.50 [0.37, 0.66] | +0.30 [0.16, 0.44] | −0.03 [−0.53, 0.31] |
| Underway (pooled) | P-known | +0.64 [0.61, 0.67] | +0.62 [0.58, 0.65] | +0.55 [0.50, 0.59] | +0.36 [0.29, 0.42] | +0.17 [0.07, 0.25] | −0.01 [−0.09, 0.07] |
| Underway (pooled) | P-unseen | +0.54 [0.47, 0.63] | +0.45 [0.26, 0.58] | +0.30 [0.09, 0.45] | +0.01 [−0.28, 0.21] | −0.11 [−0.39, 0.08] | −0.38 [−0.63, −0.10] |
| Stationary | P-known | n/a | n/a | n/a | n/a | −21.5 [−28.1, −17.0] | −11.7 [−16.8, −7.8] |
| Stationary | P-unseen | n/a | n/a | n/a | n/a | −37.9 [−75.3, −21.4] | −15.9 [−37.0, −7.0] |

- **Stationary:** n/a means the best baseline's median error is under 10 m, so skill is undefined. Retrieval's median is still worse at every horizon: 0.01–0.31 km at 1–6 h and 2.9 km at 24 h (P-known), against ≤ 0.23 km for persistence.
- **Medians, transiting, P-known (km):**
  - Retrieval: 1.62 / 3.45 / 5.88 / 12.80 / 20.83 / 25.61.
  - Best baseline: CV 4.22 / 13.22 / 25.21, then damped 37.33 / 35.76 / 37.06.
- **Feasibility, underway pooled, P-known:**
  - Retrieval puts a point on land in 8.7% → 29.5% of paths (1 → 24 h). Damped is at 26–29% and CV at 61–88%.
  - Retrieval's path-crossing rate is 28% → 66%: chords between 10-min points cut channel bends.

## 2026-10-21 — Dev-window reference runs, June+July dataset (B0 + `route_retrieval_v1`)

Dataset `noaa_us_gulf_2023-06-07` (June + July 2023), manifests `splits-v2` ([evaluation-protocol.md](evaluation-protocol.md#june--july-2023-noaa_us_gulf_2023-06-07-manifests-splits-v2)), **split = dev** (Jun 18–22). These are **development references** for retrieval v2, not held-out results, and are not used in the README.
- The June days of this dataset are identical to `noaa_us_gulf_2023-06`: 87,377,629 rows, zero differences outside `source`.
- The train-split damped-velocity τ and the navigable masks are identical to the June-only ones (same train period).
- **July (the test split) has not been benchmarked.** It stays unseen until a v2 design is frozen on dev.

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_dev_20261021T184406Z` | `P-unseen_noaa_us_gulf_2023-06-07_dev_20261021T184833Z` |
| files.json SHA-256 | `d386f4b31be35237b8d3aade4f8583e947159993d0320257e90aad5359fe5f2f` | `a74fe230823b92a85d54b03e677f83f7d29ebf6950e8b629e2abfca945662140` |
| Cutoffs | 7,372 | 366 (78 dev vessels in the cutoff region: too small for a transiting stratum) |
| Wall time | 265 s | 66 s |

These replace the first dev runs, `P-known_noaa_us_gulf_2023-06-07_20261021T182911Z` and `P-unseen_noaa_us_gulf_2023-06-07_20261021T183325Z`. Those were made before the test period's end moved to Jul 31 00:00 (which changes the manifest hash) and before dev truth was capped at the dev period's end.
- The same cutoffs and identical predictions.
- 30 P-known records (5 cutoffs × 6 models, 24 h targets just before Jun 22 00:00) are now `no_report_after`.

**Retrieval skill vs best baseline on dev, P-known** (same pattern as the June test):

| Stratum | 1 h | 3 h | 6 h | 24 h |
|---|---|---|---|---|
| Transiting | +0.61 [0.49, 0.67] | +0.74 [0.69, 0.79] | +0.70 [0.61, 0.77] | −0.08 [−0.32, 0.19] |
| Underway pooled | +0.60 [0.54, 0.64] | +0.47 [0.40, 0.55] | +0.30 [0.19, 0.41] | −0.04 [−0.17, 0.09] |
| Stationary | n/a (< 10 m) | n/a | −19.3 [−25.4, −14.7] | −15.3 [−23.7, −9.6] |

P-unseen dev is too small to decide anything (pooled underway CIs span zero at every horizon). It is a sanity check only; v2 is developed on P-known dev.

## 2026-10-21 — P-region (region-disjoint), June 2023 test

Protocol P-region on `noaa_us_gulf_2023-06`:
- **Split:** test (Jun 23–30). The test side is the Houston–Galveston box and the train side is the Gulf beyond a 50 km buffer ([evaluation-protocol.md](evaluation-protocol.md)).
- **Selection:** test cutoffs have their whole 24 h context inside the box.
- **Damped τ:** fitted on the train side (`data/model_params/damped_velocity_v1/P-region__noaa_us_gulf_2023-06.json`).
- **Not scored:** feasibility (there is no P-region navigable mask).

| | All models | Baselines only |
|---|---|---|
| Run id | `P-region_noaa_us_gulf_2023-06_20261021T195800Z` | `P-region_noaa_us_gulf_2023-06_20261021T200112Z` |
| files.json SHA-256 | `c49f75c85239e71f74f9ecd48d06b8557dfbf5c98b25d6c4e5298f57036bc4de` | `b6afbca0babe734617dc2ac45e3dbaf02f84f92b82235db3f8635f61d3e7b65b` |
| Cutoffs (after the side check; 2,796 sampled cutoffs dropped) | 19,843 | 19,843 |
| Cells | 0 (no paired samples) | 504 |

- **Code and git SHA:** the code is commit `3fccd4d`. The runs record `3fccd4d-dirty` because only docs were uncommitted.
- **Superseded first runs:** `…_20261021T194547Z` and `…_20261021T194901Z` came before the leakage review widened the train-side label check by the truth bracket, which changes the P-region τ fit (tug_tow 0.10 → 0.05 h).
- **Not a held-out result:** these use the June test days (Jun 23–30), which the failure review already read, so they are not held out. The held-out P-region test would be July on `noaa_us_gulf_2023-06-07`, which is gated like every July run.

**Retrieval does not transfer to new waters.** `route_retrieval_v1` returned `no_references` on all 19,843 cutoffs.
- Under P-region every reference must lie wholly beyond the buffer, so nothing is within 3 km of a Houston cutoff.
- Paired scoring then leaves no sample for any model, so the all-model run has no cells. Its records hold the failures.
- This is retrieval's expected limit, like TrAISformer's absolute-position bins (§5 N1). Any transfer needs a different model (kinematics, or route knowledge from another source).

**Baselines on the test side** (median km, paired):

| | Persistence | CV | Damped |
|---|---|---|---|
| Underway pooled, 1 h / 6 h / 24 h | 4.20 / **6.52** / **8.78** | 5.60 / 50.9 / 227 | **3.58** / 6.98 / 9.74 |
| Transiting, 1 h / 6 h / 24 h | 18.7 / 68.0 / **34.0** | **4.90** / 66.8 / 403 | 4.93 / **47.5** / 39.2 |

The baselines need no geography, so they transfer.

## 2026-10-21 — `route_retrieval_v2` frozen on the dev window (development record, not held out)

`route_retrieval_v2` (2.0.0) = v1 plus a hold for targets stationary at the cutoff. **On the stationary set it is persistence by construction**: `config.stationary_policy = "hold_last_position"`, and the description and warnings say so. It is not retrieval skill. On underway targets it is identical to v1.

| | P-known, dev (Jun 18–22), `noaa_us_gulf_2023-06-07` |
|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_dev_20261021T204207Z` (git `672a766`, clean) |
| files.json SHA-256 | `f8dadbbeb160e52a73adba102d4c35d06f0010aaf0668486080b11705fedcec5` |
| Models | persistence, CV, damped, v1, v2 (paired) |

| Median km (skill of v2) | 1 h | 6 h | 24 h |
|---|---|---|---|
| Stationary: v1 / v2 / persistence | 0.01 / 0.00 / 0.00 (n/a) | 0.21 / 0.01 / 0.01 (+0.00) | 3.60 / 0.22 / 0.22 (+0.00) |
| Underway pooled: v1 = v2 | 1.61 (+0.60) | 6.31 (+0.30) | 13.32 (−0.04) |
| Transiting: v1 = v2 | 1.83 (+0.61) | 12.53 (+0.70) | 52.94 (−0.08) |

- **v2 is persistence on moored vessels:** v2's predictions equal persistence's on all 27,354 stationary records.
- **How the design was chosen** (dev only; scratch diagnostics on dev cutoffs):
  - Releasing the hold where 50–90% of the reference weight had moved was worse than always holding, in median and mean, at every horizon.
  - Speed-matched continuation was mixed (transiting medians 1.83 → 1.47 km at 1 h, but 8.25 → 8.76 km pooled at 12 h) and is not in v2.
- **Not v2.0.0 as frozen**, though they carry its id:
  - `…_dev_20261021T201719Z` is a pre-freeze sweep point with `depart_share` 0.5, from a dirty tree.
  - `…_dev_20261021T203429Z` ran before the labelling fixes.
- **July is still unscored** (no test-access ledger).

## 2026-10-21 — July 2023 hold-out: B0, `route_retrieval_v1`, `route_retrieval_v2` (held-out headline)

Dataset `noaa_us_gulf_2023-06-07`, manifests `splits-v2`, **split = test (Jul 2–30), scored once**, from the clean commit `2757bd3`.
- **Ledger:** both runs are recorded in `data/manifests/test_access/noaa_us_gulf_2023-06-07.json`, written before scoring.
- **`informed_by`:** the June runs `P-known_…152618Z` and `P-unseen_…153929Z`, their failure reviews, and the dev run `…_dev_20261021T204207Z`.
- **Models:** persistence, CV, CTR, damped, v1, v2 (paired). The damped τ is the train-split fit (identical to the June fit).
- **Refused attempt:** a first P-unseen attempt was refused before scoring by the clean-tree guard (an uncommitted edit to the figure script). Nothing was scored or logged for it.

| | P-known | P-unseen |
|---|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_20261021T213109Z` | `P-unseen_noaa_us_gulf_2023-06-07_20261021T225730Z` |
| files.json SHA-256 | `b09eba0c9d9f3c22a575c102141187cf5a85c77ca0184295f4309f9d7b0e3925` | `d610bfd0d6ca5ebfdab049ebfb0acc7d40d0d3dcd2453d4cdd4e1c8373b89b0f` |
| Cutoffs | 106,503 | 23,166 |
| Cells | 828 | 756 |
| Wall time | 5,152 s | 1,002 s |

**Skill of v2 against the best baseline per cell** [95% CI]. v1 is identical on underway cells.

| Stratum | Protocol | 1 h | 3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|---|---|
| Transiting | P-known | +0.60 [0.57, 0.63] | +0.76 [0.74, 0.77] | +0.65 [0.61, 0.68] | +0.44 [0.39, 0.48] | +0.30 [0.19, 0.37] |
| Transiting | P-unseen | +0.57 [0.50, 0.63] | +0.70 [0.67, 0.75] | +0.60 [0.51, 0.67] | +0.44 [0.32, 0.52] | +0.36 [0.13, 0.47] |
| Underway pooled | P-known | +0.53 [0.50, 0.56] | +0.44 [0.40, 0.48] | +0.19 [0.13, 0.24] | −0.02 [−0.10, 0.04] | −0.31 [−0.39, −0.23] |
| Underway pooled | P-unseen | +0.45 [0.37, 0.51] | +0.29 [0.15, 0.40] | +0.06 [−0.08, 0.20] | −0.17 [−0.36, −0.02] | −0.44 [−0.68, −0.24] |
| Stationary | both | n/a (< 10 m) | n/a | +0.00 (= persistence) | +0.00 | +0.00 |

- **Stationary:** v2 = persistence by construction. v1's stationary skill at 6 / 12 / 24 h is −26.0 / −21.0 / −11.5 under P-known.
- **Transiting medians, P-known (km):**
  - v2: 1.71 / 5.97 / 13.36 / 20.19 / 25.40.
  - Best baseline: CV 4.28 / 24.54, then damped 38.15 / 35.95 / 36.06.
- **Feasibility, underway pooled, P-known, 1 → 24 h:** a point on land for v2 8.2% → 29.6%, damped 24.2% → 26.9%, CV 58.4% → 83.7%. The mask's own error is 0.5% → 1.6%.
- **P-unseen:** its July test vessels are the same vessels as in June (same hash buckets), so it is fresh in time, not in vessels.

## 2026-10-21 — Conformal displacement radii (Milestone 4 slice 1), coverage on the July hold-out

**Method** (`evaluation/conformal.py`, `scripts/conformal.py`):
- **Radii:** split-conformal, per model × horizon × level × stratum (motion:class, falling back to motion:ALL and then ALL below 30 calibration errors).
- **Fitted on the calibration split** (Jun 23–30) of `noaa_us_gulf_2023-06-07`. It is disjoint from the retrieval index period (train) and from the test period.
- **Coverage** is read from the stored records of the two July hold-out runs. Nothing is re-scored, and each analysis is recorded in the test-access ledger.
- **Fixed before any test coverage was seen:** the fallback rule, the levels (80/90/95%) and the verdict (calibrated = |coverage − level| ≤ 0.03 on cells with n ≥ 200).
- **Caveat:** strata use NOAA back-filled vessel class.

| | P-known | P-unseen |
|---|---|---|
| Calibration run | `P-known_noaa_us_gulf_2023-06-07_calibration_20261022T005126Z` (22,639 cutoffs) | `P-unseen_noaa_us_gulf_2023-06-07_calibration_20261022T011557Z` (1,084 cutoffs) |
| files.json SHA-256 | `41f550a48e957e50bff3017d110a0bcf4e09a34d2fda69833c6c1dad8874f25f` | `322af87a5c19ef7c70fa68a5980a96134f3504bf7e893ba61cb8ae2d0bc468c0` |
| Radii (calibration_set_id = the calibration run) | `data/model_params/conformal/P-known__noaa_us_gulf_2023-06-07.json` (code `822563c`) | `…/P-unseen__noaa_us_gulf_2023-06-07.json` |
| Coverage analysis | `data/runs/conformal/P-known_…_20261021T213109Z/20261022T012930Z/` | `…/P-unseen_…_20261021T225730Z/20261022T012934Z/` |
| Motion-level cells (underway:ALL, stationary:ALL) within ±3% at 90%, all models | **69 / 72** | **50 / 72** |

**P-known, 90% radii, July coverage** [vessel 95% CI], median radius, mean disc area:

| | v2, 1 h | v2, 6 h | v2, 24 h | damped, 1 h | damped, 24 h |
|---|---|---|---|---|---|
| Transiting | 0.896 [0.884, 0.908], 12.7 km, 529 km² | 0.886, 91 km | 0.884, 241 km, 182,520 km² | 0.901, 12.4 km, 724 km² | 0.881, 145 km, 155,028 km² |
| Underway pooled | 0.881 [0.875, 0.888], 7.6 km | 0.875 | 0.900 | 0.911 | 0.908 |
| Stationary | 0.890 (= persistence) | 0.892 | 0.894 | 0.884 | 0.893 |

- **The 3 P-known misses:** `route_retrieval_v1` on stationary vessels at 1–3 h (0.858–0.861). Its false departures make the errors heavy-tailed and non-exchangeable.
- **Drift through July:** v2's underway 90% coverage drifts down. At 1 h it is 0.901 → 0.888 → 0.868 → 0.875 over July weeks 0–3; at 6 h, 0.896 → 0.859 at week 2. Retrieval's frozen pool gets older, as predicted (`evaluation-protocol.md`). The kinematic baselines are steady near 0.91.
- **Area:** v2's radii are smaller than damped velocity's for transiting ships up to 12 h, at equal coverage (529 vs 724 km² at 1 h; 98,923 vs 130,365 km² at 12 h). They are larger at 24 h.

**P-unseen: not calibrated for underway traffic.**
- The calibration bucket is 5% of vessels (78 underway, 87 stationary vessels).
- Underway 90% coverage is 0.79–0.89. For example, v2 underway pooled is 0.832–0.847 at 1–6 h, and persistence in transit is 0.79–0.83.
- Stationary over-covers at 12–24 h (0.93–0.94).
- 21–41% of radii fall back to pooled strata.
- The radii are deliberately **not** refitted on the P-known calibration run, which contains P-unseen test vessels.

## 2026-10-22 — Conformal H3 Mondrian bins (Milestone 4 slice 2): pre-registered, **not adopted**

**Design** (pre-registered in the implementation log before the code existed; clarifications committed in `62f3089` before any H3 result):
- **Bins:** motion state × H3 cell (resolution 5) of the cutoff report's position, ahead of the slice-1 chain.
- **Own radius:** at least 30 calibration errors per bin.
- **Adoption, in each protocol:**
  - at 90%, at least as many motion:ALL cells within ±3 points as slice 1;
  - and v2's mean underway:ALL area smaller at 4 or more of the 6 horizons.
- **Both protocols** must pass.

| | P-known | P-unseen |
|---|---|---|
| Radii | `data/model_params/conformal/P-known__noaa_us_gulf_2023-06-07__h3r5.json` | `…/P-unseen__noaa_us_gulf_2023-06-07__h3r5.json` |
| Analysis | `data/runs/conformal/P-known_…_20261021T213109Z/20261022T020659Z/` | `…/P-unseen_…_20261021T225730Z/20261022T020706Z/` |
| Cells within tolerance at 90%: slice 1 → H3 | 69/72 → **61/72 (fails)** | 50/72 → 55/72 (passes) |
| v2 underway mean area at 90%, smaller at | 6/6 horizons (e.g. 418 → 375 km² at 1 h; 74,897 → 68,682 km² at 24 h) | 6/6 horizons |
| Rule | fails | passes |

**Verdict: not adopted.** The API keeps serving the slice-1 radii.
- Every P-known cell that flipped is retrieval (v1/v2). They sat at 0.872–0.877 under slice 1, just inside tolerance, because retrieval's frozen pool ages through July. H3's tighter radii take them to 0.867–0.869.
- The kinematic baselines are unaffected.
- **Crashed first attempt:** it wrote coverage files and then crashed before its ledger entry. Those outputs were set aside unread (`_crashed_*`) and recorded in the ledger (`86a9cad`), then the analysis was re-run.

## 2026-10-22 — `timestral_pos_v1` (T1-pos, zero-shot) on the dev window (development record, not held out)

Dataset `noaa_us_gulf_2023-06-07`, P-known, **split = dev (Jun 18–22)**, clean commit `123b4f4`. This is the identical model set as the dev reference run `…_dev_20261021T204207Z`, plus Timestral. It is **not** a held-out result and is not for the README (AGENTS.md rule 13). Timestral has not been scored on July.

| | |
|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_dev_20261022T173857Z` |
| files.json SHA-256 | `eb474894d017f18a9a8164691a3b1ba7e5ae85ebd4581903541a3a967511fadc` |
| Cutoffs / cells | 7,372 / 744 (14 dropped cells, as in the reference run) |
| Wall time | 554 s; Timestral 121 s of model time (about 16 ms per 24 h forecast on CPU) |
| Timestral checkpoint | `mistralai/Timestral-28M@49b2640`, package `a534bdb`; pretraining corpus undisclosed (see model-integration-notes) |

**Pairing.** Timestral failed on 68 cutoffs, all `insufficient_context` (fewer than 6 observed 10-min steps). Those samples leave the paired set for every model. On all 124 comparable cells, each paired count drops by exactly Timestral's failures, and the evaluable counts are identical to the reference run. Every headline cell stays headline.

**Median error, km, and skill against the cell's best baseline** [paired 95% CI]

| Stratum | Model | 1 h | 2 h | 3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|---|---|---|
| Underway pooled | Timestral T1-pos | 2.54 (+37% [30, 42]) | 4.57 (+28% [22, 35]) | 6.25 (+14% [6, 22]) | 11.29 (−23% [−36, −10]) | 15.25 (−35% [−48, −20]) | 17.32 (−32% [−47, −21]) |
| | retrieval v2 | 1.61 (+60%) | 2.58 (+59%) | 3.80 (+48%) | 6.32 (+31%) | 8.25 (+27%) | 13.40 (−2% [−17, 8]) |
| | best baseline | damped 4.01 | damped 6.33 | persistence 7.27 | persistence 9.16 | persistence 11.29 | persistence 13.10 |
| Transiting | Timestral T1-pos | 5.59 (−21% [−44, −1]) | 14.28 (−14% [−30, 5]) | 23.72 (−1% [−14, 11]) | 56.81 (−37% [−54, −14]) | 118.91 (−157%) | 180.90 (−266%) |
| | retrieval v2 | 1.83 (+60%) | 3.91 (+69%) | 6.02 (+74%) | 12.45 (+70%) | 23.90 (+48%) | 54.09 (−9% [−32, 18]) |
| | best baseline | CV 4.62 | CV 12.56 | CV 23.42 | damped 41.58 | damped 46.35 | damped 49.45 |
| Stationary | Timestral T1-pos | 0.01 | 0.02 | 0.03 | 0.08 (−650%) | 0.23 (−360%) | 1.25 (−468%) |
| | persistence (best) | 0.00 | 0.00 | 0.01 | 0.01 | 0.05 | 0.22 |

(Stationary 1–3 h has no skill reference: the best baseline's median is under 10 m.)

### What this says (dev only)
- **Short-horizon port moves:** zero-shot Timestral on raw positions beats the best kinematic baseline on pooled underway traffic at 1–3 h. That traffic is mostly tugs and harbour moves, where speed changes and the track bends.
  - From 6 h on it loses to persistence and damped velocity, by 23–35%.
- **Transiting ships:** it never beats constant velocity at 1–3 h, and from 6 h on it is far worse than damped velocity. On lanes it neither holds speed like CV nor saturates like damped velocity.
- **Moored ships:** it adds drift to them (worse than persistence at every horizon).
- **Against retrieval:** retrieval v2 beats Timestral in every stratum and at every horizon up to 12 h.
- This matches the plan's expectation for a model with no route knowledge (§5 T1): a smoothed extrapolator of the recent track, useful only where recent curvature and deceleration carry information.
- **Next:**
  - T1-vel (velocity channels, integrated geodesically);
  - longer histories (48/72 h);
  - T1-vel+route (retrieval displacement as a known-future covariate), the variant where route knowledge can enter.

## 2026-10-22 — `timestral_vel_v1` (T1-vel) vs T1-pos on the dev window (development record, not held out)

Dataset `noaa_us_gulf_2023-06-07`, P-known, **split = dev**, 24 h history, clean commit `727a1f1`. Models: persistence, CV, damped velocity, retrieval v2, T1-pos, T1-vel, all paired. Not held out; not for the README.

| | |
|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_dev_20261022T175118Z` |
| files.json SHA-256 | `e05c14df71bb3a899ecd0827dbb73181ebd3ad448e85c8a6e90418d57c2eb8de` |
| Cutoffs / cells / wall time | 7,372 / 744 / 510 s (T1-pos 120 s, T1-vel 133 s of model time) |
| Failures (removed from every model's paired set) | T1-vel 82 and T1-pos 68 `insufficient_context`; retrieval v2 3 `no_references`; no `stale_anchor` (cutoffs are report times) |

**Median km and skill against the best baseline** [paired 95% CI]

| Stratum | Model | 1 h | 2 h | 3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|---|---|---|
| Underway pooled | T1-pos | 2.54 (+37% [30, 42]) | 4.57 (+28% [22, 35]) | 6.25 (+14% [6, 22]) | 11.30 (−23%) | 15.30 (−35%) | 17.32 (−32%) |
| | T1-vel | 2.89 (+28% [19, 35]) | 5.75 (+9% [0, 20]) | 8.37 (−15% [−27, −1]) | 17.73 (−93%) | 34.58 (−206%) | 56.43 (−330%) |
| Transiting | T1-pos | 5.59 (−21% [−44, −1]) | 14.28 (−14% [−30, 5]) | 23.72 (−1%) | 56.81 (−37%) | 118.91 (−157%) | 180.90 (−266%) |
| | T1-vel | 4.28 (+7% [−9, 18]) | 12.56 (+0% [−12, 11]) | 23.79 (−2% [−9, 8]) | 60.33 (−45%) | 146.55 (−216%) | 329.26 (−566%) |
| Stationary | T1-pos | 0.01 | 0.02 | 0.03 | 0.08 (−652%) | 0.23 (−360%) | 1.25 (−468%) |
| | T1-vel | 0.01 | 0.02 | 0.03 | 0.09 (−820%) | 0.33 (−543%) | 1.85 (−738%) |

Retrieval v2 on the same set is unchanged from the T1-pos record above (for example, 1.61 / 3.80 / 6.32 km pooled at 1 / 3 / 6 h).

### What this says (dev only)
- **T1-vel is worse than T1-pos on pooled underway traffic at every horizon.** Integrating forecast velocities compounds their error, and from 3 h on it loses to the best baseline.
- **On transiting ships, T1-vel matches constant velocity at 1–3 h** (CIs straddle 0). It extrapolates the current velocity, which is what CV does, and it is better than T1-pos at 1 h. From 6 h on it is far worse than damped velocity.
- **Neither representation beats retrieval anywhere up to 12 h, and neither should be a product model as is.** The variant left in the plan where route knowledge can enter is T1-vel+route.

### History length: not measured, because this dev window cannot support it
A cutoff is kept only if its whole window (`as_of − history`, `as_of + 24 h`) lies inside the dev period (Jun 18–22). So:
- 24 h history draws cutoffs from 2 days;
- 48 h from 1 day;
- 72 h from none.

Separate runs would therefore compare different cutoff populations, not history lengths. An interrupted 48 h run left an empty directory, which was removed; no 48 or 72 h result exists. A sound comparison needs one of:
- (a) a sampler purge window set independently of the model's history, so all lengths share one cutoff set (a protocol change, for review);
- (b) one run with per-model context lengths on a shared paired set;
- (c) a longer dev window.

## 2026-10-22 — Timestral T1-pos history length (24 / 48 / 72 h context) on one paired set (development record, not held out)

**Design:** one run, so every context length is scored on the identical cutoffs. The run gives every model 72 h of history; the T1-pos variants trim their own context to 24 h (`timestral_pos_c24_v1`), 48 h (`timestral_pos_c48_v1`) or keep all 72 h (`timestral_pos_v1`). With 72 h of history the dev window (Jun 18–22) only fits horizons up to 6 h, so the cutoffs come from Jun 21 and the horizons are 1/2/3/6 h. No protocol change.

| | |
|---|---|
| Run id | `P-known_noaa_us_gulf_2023-06-07_dev_20261022T182637Z` (commit `554b61d`) |
| files.json SHA-256 | `7160638b9ebcaefd202dbe86bf4e64a6fa5cf1acefb6981ae645d3c8b038c62c` |
| Cutoffs / cells / wall time | 3,400 / 532 / 309 s |

**Median km and skill against the best baseline** [paired 95% CI]

| Stratum | Model | 1 h | 2 h | 3 h | 6 h |
|---|---|---|---|---|---|
| Underway pooled (n ≈ 1,040–1,150 from ~700 vessels) | T1-pos, 24 h context | 2.54 (+33%) | 4.22 (+27%) | 6.21 (+4% [−10, 20]) | 10.52 (−14% [−31, 2]) |
| | T1-pos, 48 h | 2.45 (+36%) | 4.19 (+28%) | 5.61 (+14% [0, 28]) | 9.71 (−6% [−21, 10]) |
| | T1-pos, 72 h | 2.40 (+37%) | 4.19 (+28%) | 5.43 (+16% [1, 31]) | 9.07 (+1% [−12, 16]) |
| | retrieval v2 | 1.33 (+65%) | 2.34 (+60%) | 3.05 (+53%) | 6.02 (+35%) |
| Transiting (n ≈ 170–190) | T1-pos 24 / 48 / 72 h | 4.94 / 4.90 / 4.86 | 12.77 / 11.87 / 12.44 | 23.03 / 22.35 / 20.67 | 45.98 / 48.38 / 46.37 |
| | (all three: skill CIs straddle 0) | | | | |
| Stationary | T1-pos 24 / 48 / 72 h | 0.01 / 0.02 / 0.03 | 0.02 / 0.03 / 0.04 | 0.03 / 0.04 / 0.06 | 0.10 / 0.13 / 0.18 |

**Longer context, directly** (paired skill of 72 h or 48 h over 24 h, 500 vessel-bootstrap resamples, from `records.parquet`):

| Stratum | Horizon | 48 h vs 24 h | 72 h vs 24 h |
|---|---|---|---|
| Underway pooled | 1 h | +4% [−3, 10] | +6% [−1, 13] |
| | 3 h | +10% [1, 18] | +13% [3, 21] |
| | 6 h | +8% [0, 15] | +14% [5, 23] |
| Transiting | 1 h | +1% [−18, 23] | +2% [−20, 23] |
| | 3 h | +3% [−19, 17] | +10% [−14, 21] |
| | 6 h | −5% [−22, 4] | −1% [−19, 12] |

### What this says (dev, one day of cutoffs, horizons ≤ 6 h)
- **Moving vessels:** longer context helps T1-pos at 3–6 h on pooled underway traffic, by about 13–14% from 24 h to 72 h. At 6 h, 72 h of context brings it level with the best baseline.
- **Transiting ships:** no effect can be detected; the sample is small.
- **Moored ships:** longer context makes them drift more.
- **Retrieval v2 stays far ahead at every horizon.** Context length doesn't change the conclusion that zero-shot Timestral on raw positions is not a product model.
- **Limits:** one day of cutoffs (Jun 21), horizons up to 6 h only, and the population differs from the 24 h dev runs above. These numbers compare context lengths with each other, not with those runs.

## 2026-10-22 — Hex prediction sets (H1, retrieval ensemble) vs the disc baseline, dev window (Milestone 7 slice 1; development record)

Dev run `P-known_noaa_us_gulf_2023-06-07_dev_20261022T175118Z` (24 h history), underway cutoffs only (2,743). Script `scripts/hexsets_dev.py` at commit `10e0da1`. Outputs are in `data/runs/hexsets/<run>/20261022T193234Z/` (90%) and the sensitivity run beside it (93%). Not held out, not for the README. Retrieval v2's design was frozen on this same window, so absolute numbers are optimistic for both methods; only the comparison counts.

**Design**
- **Split:** chronological at the median cutoff time (2023-06-19 23:57).
  - Calibration rows need their label before the split (purge); evaluation rows have `as_of` after it.
  - A vessel-hashed third of the calibration rows chooses H1's bandwidth from {0.5, 1, 2, 4, 8, 16} km by smallest set area. The other two thirds fit the disc radius and the density threshold.
- **H1:** retrieval v2 re-run with K = 50; candidates that cover the target time, weighted by similarity. A Gaussian kernel density, mixed (5%) with a broad Gaussian of scale disc radius / 2 around the run's scored v2 point.
- **Disc:** the conformal disc around that same scored v2 point.
- **Scoring:** both are scored as H3 cells (res 7 up to 3 h, res 6 beyond). Covered means the truth's cell is in the set.
- **No failures and no dropped samples:** retrieval re-run failures 0.
- **24 h is not calibratable:** no calibration label lands before the split.

**Underway pooled, evaluation half (n ≈ 1,170–1,310, vessel-bootstrap 95% CIs)**

| Horizon | Disc @ 90% nominal: coverage / mean km² | H1 @ 90%: coverage / km² (H1/disc area) | H1 @ 93%: coverage / km² |
|---|---|---|---|
| 1 h | 0.907 [0.890, 0.922] / 252 | 0.878 [0.856, 0.896] / 46 (0.18) | 0.903 [0.885, 0.922] / 65 |
| 2 h | 0.898 [0.881, 0.915] / 815 | 0.882 [0.862, 0.899] / 151 (0.19) | 0.909 [0.891, 0.927] / 222 |
| 3 h | 0.918 [0.902, 0.934] / 2,052 | 0.888 [0.867, 0.908] / 300 (0.15) | 0.925 [0.909, 0.941] / 613 |
| 6 h | 0.917 [0.900, 0.933] / 7,775 | 0.885 [0.865, 0.905] / 1,159 (0.15) | 0.917 [0.898, 0.935] / 2,216 |
| 12 h | 0.869 [0.848, 0.890] / 16,258 | 0.859 [0.834, 0.881] / 3,924 (0.24) | 0.883 [0.862, 0.903] / 4,724 |

**Transiting, evaluation half (n ≈ 210–230)**

| Horizon | Disc @ 90% | H1 @ 90% (area ratio) | H1 @ 93% (area ratio against disc @ 93%) |
|---|---|---|---|
| 1 h | 0.879 / 295 | 0.871 / 155 (0.53) | 0.918 / 284 (0.58) |
| 2 h | 0.905 / 2,005 | 0.849 / 424 (0.21) | 0.871 / 810 (0.27) |
| 3 h | 0.888 / 4,073 | 0.841 / 606 (0.15) | 0.893 / 1,478 (0.21) |
| 6 h | 0.887 / 17,753 | 0.859 / 9,963 (0.56) | 0.892 / 18,241 (0.79) |
| 12 h | 0.839 / 56,760 | 0.794 / 16,300 (0.29) | 0.954 / 111,998 (1.12) |

### What this says (dev)
- **On pooled underway traffic, H1's sets are about 3.5–4 times smaller than the disc at matched real coverage.** At 93% nominal, H1 reaches 0.90–0.93 real coverage, and its areas are 26–29% of the disc's at 90% nominal over 1–12 h. This is the v0.4 hypothesis: the probability mass follows the lanes, a disc does not.
- **H1 under-covers its nominal level by about 2–3 points; the disc by about 0–1.** Likely causes are the chronological shift between the halves, and the density score being more sensitive to which routes were in use. For the July comparison the level correction must be fixed in advance, not tuned on test.
- **Transiting is unstable:** bandwidth selection from 32–83 selection samples swings between 0.5 and 16 km, and at 12 h H1 is no smaller. The fix to try is choosing the bandwidth on pooled underway traffic (or as a fraction of the candidate spread) rather than per stratum.
- **24 h** needs a longer calibration window: the calibration split (Jun 23–30) does that for the hold-out protocol.


## 2026-10-22 — Hex prediction sets (H1) vs disc on the July hold-out, P-known (held-out; pre-registered; **adopted**)

- **Test run:** `P-known_noaa_us_gulf_2023-06-07_20261021T213109Z` (underway cutoffs: 40,950).
- **Calibration run:** `P-known_noaa_us_gulf_2023-06-07_calibration_20261022T005126Z`.
- **Code and design:** commit `e62bbdc`, frozen design `data/model_params/hexsets/h1_design.json` (sha `e0d6a707…`).
- **Analysis:** `data/runs/hexsets/P-known_noaa_us_gulf_2023-06-07_20261021T213109Z/20261022T211506Z/` (report.md, summary.json, samples.parquet).
- **Ledger:** `hexsets_h1_started` then `hexsets_h1`. `informed_by` lists the earlier `conformal_coverage*` analyses of this run: the disc arm was not blind.
- **Pre-registration:** implementation log, 2026-10-22, written before the look; the procedural fixes were also made before it.
- Retrieval re-run failures: 0. Zero-candidate (broad-only) sets per cell are in the report; most at 24 h (2,129 of 36,893 pooled).

| Stratum | h | n (vessels) | Disc coverage [CI] | H1 coverage [CI] | Disc km² | H1 km² | H1/disc area [CI] |
|---|---|---|---|---|---|---|---|
| underway:ALL | 1 h | 39,700 (2,768) | 0.843 [0.833, 0.853] ✗ | 0.837 [0.821, 0.849] ✗ | 252 | 67 | 0.26 [0.26, 0.27] |
| underway:ALL | 2 h | 39,198 (2,716) | 0.847 [0.837, 0.858] ✗ | 0.840 [0.828, 0.852] ✗ | 914 | 233 | 0.25 [0.25, 0.26] |
| underway:ALL | 3 h | 38,828 (2,687) | 0.844 [0.834, 0.854] ✗ | 0.840 [0.827, 0.853] ✗ | 1,807 | 569 | 0.32 [0.31, 0.32] |
| underway:ALL | 6 h | 37,908 (2,605) | 0.854 [0.845, 0.863] ✗ | 0.851 [0.841, 0.863] ✗ | 6,597 | 1,782 | 0.27 [0.27, 0.28] |
| underway:ALL | 12 h | 36,704 (2,528) | 0.865 [0.856, 0.874] ✗ | 0.864 [0.854, 0.872] ✗ | 19,054 | 6,268 | 0.33 [0.32, 0.34] |
| underway:ALL | 24 h | 36,893 (2,535) | 0.876 [0.868, 0.883] | 0.873 [0.864, 0.881] | 40,132 | 18,485 | 0.46 [0.45, 0.47] |
| underway:TRANSITING | 1 h | 6,006 (1,004) | 0.895 [0.880, 0.907] | 0.889 [0.870, 0.903] | 478 | 241 | 0.51 [0.50, 0.51] |
| underway:TRANSITING | 2 h | 5,937 (1,000) | 0.895 [0.883, 0.907] | 0.899 [0.885, 0.912] | 2,099 | 928 | 0.44 [0.44, 0.44] |
| underway:TRANSITING | 3 h | 5,864 (998) | 0.897 [0.885, 0.907] | 0.902 [0.889, 0.914] | 5,103 | 1,886 | 0.37 [0.36, 0.38] |
| underway:TRANSITING | 6 h | 5,645 (992) | 0.895 [0.885, 0.904] | 0.898 [0.887, 0.909] | 20,936 | 7,793 | 0.37 [0.36, 0.39] |
| underway:TRANSITING | 12 h | 5,033 (969) | 0.894 [0.885, 0.902] | 0.899 [0.888, 0.911] | 82,750 | 28,694 | 0.35 [0.33, 0.36] |
| underway:TRANSITING | 24 h | 4,590 (942) | 0.895 [0.884, 0.907] | 0.894 [0.882, 0.904] | 168,463 | 95,960 | 0.57 [0.55, 0.59] |

(✗ = outside ±0.03 of 0.90.)

**Pre-registered verdict:** H1 within tolerance on 7 of 12 cells (disc: 7); H1 smaller on 6 of 6 pooled horizons → **adopted**. The served parameters (`data/model_params/hexsets/P-known__noaa_us_gulf_2023-06-07.json`) carry this verdict.

### What this says
- **At equal coverage, H1's hex sets are 2–4 times smaller than the conformal disc** around the same retrieval forecast, in both strata and at every horizon. The two methods' coverages differ by at most 0.6 points in any cell.
- **Transiting ships are calibrated at every horizon (0.889–0.902).**
- **Pooled underway traffic is under-covered by both methods at 1–12 h (0.84–0.87).** This is the July drift seen for U1, the frozen retrieval pool ageing through July. H1 improves area, not coverage. A coverage fix (a rolling calibration window, or recalibration per week) is separate work, with its own protocol.


## 2026-10-22 — Nested 90/95/98% hex bands on the July hold-out, P-known (held-out; pre-registered band check)

- **Served parameters:** `data/model_params/hexsets/P-known__noaa_us_gulf_2023-06-07.json` at commit `bda5f1c`: the 90% thresholds are unchanged, and the 95/98% thresholds come from the same calibration window.
- **Analysis:** `data/runs/hexsets/P-known_noaa_us_gulf_2023-06-07_20261021T213109Z/levels_20261022T225158Z/`.
- **Ledger:** `hexsets_h1_levels_started` / `hexsets_h1_levels`. `informed_by` lists every earlier analysis of this run, including the H1 look, so the 90% coverage was already known.
- **Pre-registered tolerance:** ±0.03 at 90%, ±0.02 at 95%, ±0.01 at 98% (n ≥ 200). Bands revised from 50/80 to 95/98 at the user's request before any band was checked.

| Stratum | h | 90%: coverage / km² | 95%: coverage / km² | 98%: coverage / km² |
|---|---|---|---|---|
| Transiting | 1 h | 0.889 / 241 | 0.957 / 948 | 0.981 / 2,176 |
| Transiting | 2 h | 0.899 / 928 | 0.955 / 3,494 | 0.983 / 8,272 |
| Transiting | 3 h | 0.902 / 1,886 | 0.960 / 7,388 | 0.983 / 16,906 |
| Transiting | 6 h | 0.898 / 7,793 | 0.955 / 23,936 | 0.980 / 49,401 |
| Transiting | 12 h | 0.899 / 28,694 | 0.952 / 80,543 | 0.983 / 178,681 |
| Transiting | 24 h | 0.894 / 95,960 | 0.945 / 241,314 | 0.973 / 412,920 |
| Underway, all | 1 h | 0.837 ✗ / 67 | 0.898 ✗ / 287 | 0.961 ✗ / 1,017 |
| Underway, all | 2 h | 0.840 ✗ / 233 | 0.896 ✗ / 928 | 0.956 ✗ / 3,172 |
| Underway, all | 3 h | 0.840 ✗ / 569 | 0.892 ✗ / 1,544 | 0.949 ✗ / 5,407 |
| Underway, all | 6 h | 0.851 ✗ / 1,782 | 0.903 ✗ / 5,948 | 0.958 ✗ / 19,041 |
| Underway, all | 12 h | 0.864 ✗ / 6,268 | 0.917 ✗ / 19,510 | 0.969 ✗ / 61,454 |
| Underway, all | 24 h | 0.873 / 18,485 | 0.933 / 55,683 | 0.974 / 146,974 |

Cells within tolerance: 7/12 at each level (all 6 transiting cells, plus pooled 24 h). CIs are in the report.

### What this says
- **For transiting ships, the bands mean what they say at every level and horizon.** 90 / 95 / 98% of July positions fell in the 90 / 95 / 98% bands, within the stated tolerance.
- **For all underway traffic pooled, every band under-covers at 1–12 h by about the same 4–6 points** (0.84–0.86 at 90%, 0.89–0.92 at 95%, 0.95–0.97 at 98%). This is the July drift already seen for the 90% sets and the discs. It needs a coverage fix, not wider bands.
- **The 95% and 98% bands are largely round:** past the 90% band the density is mostly the broad safety component, not the retrieved routes. They are calibrated, but carry little route information. A traffic-density prior in place of the round component is the way to give the outer bands lane shape (§5 H2).
- **Area grows steeply toward 98%:** for transiting ships at 6 h, 7,800 → 23,900 → 49,400 km².


## 2026-10-22 — H1 sensitivity to K (retrieved tracks per forecast), dev window (development record)

The question was whether K = 50 is too few, or overfits. The same dev design (spread rule, 90%, calibrated on the first half, scored on the second) was run at K = 100, 200 and 400. Outputs: `data/runs/hexsets/<dev run>/20261022T231430Z`, `…T232053Z`, `…T233122Z`; the K = 50 run is `…T200752Z`.

| Underway pooled, H1 coverage / km² | K = 50 | K = 100 | K = 200 | K = 400 |
|---|---|---|---|---|
| 1 h | 0.880 / 46 | 0.879 / 45 | 0.881 / 47 | 0.878 / 47 |
| 3 h | 0.886 / 405 | 0.891 / 411 | 0.874 / 308 | 0.876 / 322 |
| 6 h | 0.880 / 1,433 | 0.881 / 1,394 | 0.883 / 1,450 | 0.881 / 1,446 |
| 12 h | 0.862 / 3,986 | 0.857 / 3,672 | 0.868 / 4,112 | 0.872 / 4,302 |
| Transiting 12 h | 0.794 / 18,836 | 0.817 / 20,274 | 0.821 / 20,957 | 0.821 / 21,175 |

**What this says:**
- K barely matters between 50 and 400: coverage moves within ±2 points and area within about ±10%, in both strata and at every horizon.
- Similarity weights (exp(−D/2)) give the extra, less similar tracks almost no mass, so the density hardly changes.
- There is no overfitting signature. The only fitted quantities are the spread factor c per horizon (on dev) and the conformal thresholds (on a separate window), and July coverage for transiting ships was nominal.
- More tracks do not give the outer 95/98% bands lane shape. That needs a traffic-density prior in place of the round broad component.


## 2026-10-23 — Beyond Houston: four other Gulf ports, dev window (development record)

The question was whether the approach only works at Houston. Cutoffs were started at four other Gulf ports (new regions in `config/regions.yaml`). The retrieval pool and truth region are Gulf-wide as before. P-known, dev window (Jun 18–22), commit `b4040d8`. Not held out, not for the README.

| Port | Run id | Cutoffs |
|---|---|---|
| New Orleans / lower Mississippi | `P-known_noaa_us_gulf_2023-06-07_dev_new_orleans_mississippi_20261022T233151Z` | 8,431 |
| Corpus Christi | `…_dev_corpus_christi_20261022T233550Z` | 1,262 |
| Mobile–Pascagoula | `…_dev_mobile_pascagoula_20261022T233705Z` | 1,302 |
| Tampa Bay | `…_dev_tampa_bay_20261022T233813Z` | 1,139 |

**Point forecasts (retrieval v2, skill against the best baseline):**
- At every port, retrieval beats the baselines at 1–3 h: +32% to +55% pooled and +53% to +79% transiting at 1 h.
- Pooled underway goes negative from 6–24 h, as at Houston.
- New Orleans transiting stays at +65% at 6 h.
- The three smaller ports have only 20–51 transiting samples per horizon.

**Hex sets with Houston's calibrated parameters applied unchanged** (`scripts/hexsets_holdout.py --dry-run <port run>`; fitted on the Houston calibration window):

| Port | H1 coverage (pooled, 1–24 h) | H1 area against the disc | Note |
|---|---|---|---|
| New Orleans | 0.893–0.902 (12/12 cells within tolerance) | 0.24–0.52 | transfers cleanly |
| Corpus Christi | 0.928–0.958 | 0.26–0.40 | over-covers: too cautious |
| Mobile–Pascagoula | 0.838–0.869 at 1–6 h; 0.90 at 12–24 h | 0.26–0.39 | under-covers at short horizons |
| Tampa Bay | 0.78–0.92 (n = 99–159; CIs about ±0.07) | 0.28–0.52 | too few samples to judge; the disc misses there too |

**Hex sets calibrated locally** (each port's own first half; `scripts/hexsets_dev.py`):
- New Orleans: 0.865–0.898 pooled, at 19–46% of the disc's area.
- Corpus Christi and Mobile: 0.78–0.95, on 180–210 samples per cell.
- Tampa: every cell skipped (too few samples).

### What this says
- **Not Houston-specific.** The method needs local traffic history, not Houston tuning: point skill and the 2–4× area advantage over discs hold at all four ports.
- **Where it cannot work:** waters with no history. The P-region test above found no references at all.
- **Calibration transfers only partly.** It held at New Orleans, a large port with similar traffic. Corpus Christi over-covers, and Mobile under-covers at short horizons. Each port, or region, needs its own calibration window.
- **Data is the limit.** The smaller ports need more than five dev days to calibrate per cell (about ≥ 200 samples). The calibration split (Jun 23–30) per port, or more months of AIS, would give that.
