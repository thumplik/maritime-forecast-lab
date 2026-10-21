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
