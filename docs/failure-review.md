# Failure review: `route_retrieval_v1` (Milestone 3)

**Purpose:** where and why historical-route retrieval loses to the best baseline, by category (build plan §7 Milestone 3). This guides the next retrieval slice. **It is not an accuracy report;** the headline numbers are in [benchmarks.md](benchmarks.md).

## Source and method

| | P-known | P-unseen |
|---|---|---|
| Benchmark run | `P-known_noaa_us_gulf_2023-06_20261021T152618Z` | `P-unseen_noaa_us_gulf_2023-06_20261021T153929Z` |
| Paired (cutoff, horizon) samples reviewed | 120,612 | 26,557 |
| Review output | `data/runs/reviews/<run id>/`: `report.md` (every stratum, horizon and category), `samples.parquet`, `summary.parquet`, `review.json` (thresholds, run digest, git SHA) | same |

- **Reproduce:** `uv run --project backend python scripts/failure_review.py --protocol P-known` (or `P-unseen`).
- **How it works:** the script re-runs retrieval on the run's cutoffs and refuses to continue unless it reproduces the run's stored predictions (it did, for every sample of both runs). Each paired sample is then flagged with the categories defined in `backend/src/maritime_forecast/evaluation/failure_review.py`. Flags overlap.
- **Thresholds** were fixed before the first review run.
- **The reference** is the run's own best baseline for each cell (`best_baseline_id`, the reference of its skill). A **loss** is a sample where retrieval's error exceeds that baseline's.
  - **Moored vessels at 1–6 h:** the run has no skill reference there, because the best baseline's median error is under 10 m. The review compares with persistence for the *size* of the difference only and scores no losses, since a loss decided by metre-level noise means nothing.
- **† marks categories that select on truth:** `departure`, `false_departure`, `timing_dominated` and `wrong_branch`. Errors within them describe a truth-conditioned subset and are not a model's accuracy.
- **`wrong_branch` is an oracle over the candidate paths.** It is read only against `wrong_branch_control`: the same oracle over the same candidates rotated about the anchor by a random angle (seeded), which keeps their spread and distance but not their direction. Only the excess over the control is evidence (AGENTS.md rule 7, §5 N1).

All numbers below come from the two `report.md` files. They are P-known unless marked, and P-unseen shows the same pattern.

## 1. Moored and anchored vessels: false departures

| Stationary set, P-known | 1 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|
| `false_departure` † share (vessel stayed ≤ 0.5 km; retrieval moved > 1 km) | 11% | 13% | 15% | 14% |
| Their median excess error vs persistence | +4.2 km | +6.8 km | +11.3 km | +12.4 km |
| Their share of stationary losses (scored cells only) | n/a | n/a | 23% | 22% |
| `departure` † share (the vessel really left, > 1 km) | 11% | 27% | 36% | 44% |
| Their median diff vs persistence | 0.00 km | 0.00 km | 0.00 km | 0.00 km |

- **False departures:** a nearby reference that was moored at the matched time and later left drags the forecast away. False departures lose to persistence by construction, so the finding is their share and size, not their loss rate.
- **Real departures:** retrieval ties persistence. It does not predict them; it only adds false ones.
- **Everything else:** on moored vessels overall, retrieval's median error is 0.01 km at 1 h and 2.9 km at 24 h, against 0.00 and 0.23 km for persistence.
  - The review does not separate how much of that excess false departures explain, beyond their share of losses (about a quarter at 12–24 h).
  - Stationary `wrong_branch` † (14–19%) overlaps them; the overlap is not computed.
- P-unseen: false departures are 15–19% of stationary samples, with a median excess of +3.7 km at 1 h and +11.0 km at 24 h.

## 2. Channel junctions: wrong branch, against its control

| Share of samples flagged | 1 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|
| Underway pooled: `wrong_branch` † / control | 30% / 16% | 40% / 19% | 36% / 18% | 26% / 14% |
| Transiting: `wrong_branch` † / control | 23% / 8% | 40% / 11% | 36% / 11% | 21% / 7% |
| P-unseen underway pooled: `wrong_branch` † / control | 31% / 18% | 41% / 22% | 38% / 21% | 29% / 17% |

- The oracle fires about twice as often as on direction-randomized candidates in pooled underway traffic, and about three times as often in transit. So there is a real branch-choice signal beyond chance.
- About half of the pooled flag would appear by chance anyway (the control rate).
- The excess (14–21 points pooled, 14–29 in transit) bounds how many samples a perfect choice among *today's* candidates could fix. It is an upper bound, since the oracle uses truth.
- **Implication:** better branch choice is a real but bounded lever. Candidate signals: longer context (the match uses 30 min of shape today), course history, vessel class or size. Whether any of them helps is measured on the dev window first (see "Test-set use" below).

## 3. Timing: right line, wrong speed

| `timing_dominated` † share (error > 1 km and \|along-track\| > \|cross-track\|), of all samples | 1 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|
| Underway pooled | 37% | 42% | 39% | 39% |
| Transiting | 46% | 48% | 42% | 41% |

- Retrieval still beats the best baseline on the median of these samples up to 12 h (pooled −1.1 km at 1 h, −4.4 km at 6 h).
- But much of its remaining error lies **along** the line it follows, as in the README example, where at 6 h the forecast is where the ship was at 3 h.
- References move at their own historical speed. Rescaling each reference's time by the ratio of the target's speed to the reference's speed at the match (bounded) is the obvious experiment.

## 4. References running out

| `references_exhausted` share | 1–3 h | 6 h | 12 h | 24 h |
|---|---|---|---|---|
| Underway pooled, P-known | 0% | 1% | 3% | 65% |
| Transiting, P-known | 0% | 1% | 6% | 61% |

- By 24 h most central references have ended, and the forecast holds the last referenced position. *Why* they end (leaving receiver coverage, mooring, a feed gap) is not measured by this review.
- It does not yet decide the losses:
  - in transit, held samples still beat damped velocity at 24 h (33.3 vs 38.7 km median);
  - pooled, they tie (median diff −0.15 km).
- It does cap long-horizon skill. Chaining a second retrieval at the reference's end is the candidate fix.

## 5. By vessel class (P-known, from the run's class strata; skill vs the run's best baseline, 95% CI)

| Underway class | 1 h | 6 h | 24 h |
|---|---|---|---|
| tanker | +0.64 [0.60, 0.68] | +0.68 [0.58, 0.74] | +0.23 [−0.04, 0.38] |
| cargo | +0.49 [0.38, 0.61] | +0.68 [0.60, 0.75] | +0.36 [0.04, 0.57] |
| passenger | +0.37 [0.07, 0.60] | +0.52 [0.22, 0.77] | +0.06 [−0.42, 0.42] |
| service | +0.70 [0.60, 0.76] | +0.41 [0.22, 0.63] | +0.06 [−0.17, 0.30] |
| tug_tow | +0.56 [0.49, 0.62] | +0.19 [0.08, 0.27] | **−0.12 [−0.24, −0.02]** |
| other | +0.40 [0.17, 0.61] | +0.01 [−0.35, 0.32] | **−0.63 [−1.17, −0.14]** |
| fishing | **−1.45 [−2.53, −0.36]** | −0.10 [−1.17, 0.41] | −0.17 [−1.08, 0.15] |

- **Fishing vessels** don't follow lanes. Retrieval loses to constant velocity (the run's best baseline for fishing at 1 h) even at 1 h.
- **Tugs and "other"** do short, irregular harbour jobs, and lose at 24 h.
- **Lane-following ships** (tanker, cargo, passenger) are where retrieval is strong.

## 6. Smaller categories

- **`sparse_history`** (fewer than 3 gap-free 10-min context steps, or a last report older than the baselines' 30-min stale limit): 4–7% of samples. Retrieval's median diff vs the best baseline on these is close to the overall one, so nothing special is needed yet.
- **`new_geography`** (nothing within 1 km; matched within 3 km): ≤ 0.3% of samples, plus outright failures (≤ 0.1% P-known, ≤ 0.6% P-unseen). It is too rare to characterize: 22 pooled underway samples at 1 h (median diff +0.8 km) and 4 in transit (+11.7 km).
- **Weather-driven turns:** not assessable. The dataset has no archived weather or wind.

## Test-set use

This review read **test-period results** (Jun 23–30). For retrieval, that June test set is therefore no longer untouched (§6.5).

**Decision (2026-10-21, user):** fetch July 2023 as a fresh test period, and develop on a dev window. The new June+July manifests (`noaa_us_gulf_2023-06-07`, [evaluation-protocol.md](evaluation-protocol.md#june--july-2023-noaa_us_gulf_2023-06-07-manifests-splits-v2)) define the periods:

| Split | Period |
|---|---|
| train | Jun 1–17, unchanged |
| dev | Jun 18–22 |
| calibration (for Milestone 4) | Jun 23–30 (under P-known, the old June test) |
| test | **Jul 2–30 (period ends Jul 31 00:00), untouched** |

Rules:
1. Any retrieval version developed after this review is designed on the dev window. Its held-out result is the July test.
2. Benchmarks of such a version record `informed_by` = the two review run ids above. Any June-test result for it is labelled a second look, never held-out.
3. Gating or hybrids (for example, a fallback for fishing vessels) are adopted only if the dev window shows the same pattern.
4. These rules are enforced in code (confirmation, clean tree, an access ledger, API refusals); see [evaluation-protocol.md](evaluation-protocol.md#june--july-2023-noaa_us_gulf_2023-06-07-manifests-splits-v2).
5. **Under P-unseen, July is fresh in time but not in vessels:** its test vessels are the ones this review examined. Any P-unseen July result says so.

## What to try next, in order

Each change is developed on the dev window and benchmarked as a new model version next to `route_retrieval_v1`, under the rules above, with July as the held-out test:

1. **Stationary handling:** for a target stationary at the cutoff, prefer staying unless most candidates leave. Weight candidates by whether they stayed moored over the horizon. This removes false departures without claiming to predict real ones.
2. **Speed-matched continuation:** time-warp each reference by the target's speed relative to the reference's, within bounds.
3. **Branch choice:** longer context and course-history terms. Measure whether the `wrong_branch` excess over its control falls.
4. **Class gating** for fishing vessels, as a labelled hybrid, only if the dev window agrees.
5. **Reference chaining** for horizons beyond the references' coverage.
