# Implementation log

**Current milestone: 1 — Repository, contracts and data pipeline** (started 2026-10-19). Milestone 0 was completed and reviewed on 2026-10-19.

Append-only. Newest entry last. Use the `/log-progress` skill.

---

## 2026-10-19 — Milestone 0: data spike, coverage audit, calibration

**Milestone:** 0 — Data spike and coverage audit
**Done:**
- Repo scaffold, CLAUDE.md/AGENTS.md, Claude Code skills and `leakage-reviewer` subagent; public repo https://github.com/thumplik/maritime-forecast-lab
- `scripts/fetch_noaa.py`: resumable stream-download, SHA-256, bbox filter to the Gulf, manifest (30/30 June days; 776 MB kept)
- `scripts/canonicalize.py` + `data/canonical.py`: §4 schema (87.4 M Gulf reports)
- `data/segment.py`, `data/cutoffs.py`, `evaluation/truth.py`, `evaluation/windows.py`, `evaluation/bootstrap.py`, `geo/geodesic.py`, `models/baselines.py` with 31 unit tests
- `scripts/coverage_audit.py`, `scripts/calibrate.py`, golden test, spike notebook `research/notebooks/m0_spike.py`
- `docs/coverage-audit.md` written

**Commands run:**
```bash
make fetch            # 2023-06-01..30; 06-10 and 06-13 re-fetched after the malformed-line fix
make canonical
uv run --project backend python scripts/calibrate.py      # calibration_20261020T024511Z
uv run --project backend python scripts/coverage_audit.py # coverage_audit_20261020T024624Z
cd backend && uv run pytest && uv run pytest -m golden
```

**Results:** Calibration PASS: CV 4.03 / 9.65 / 16.16 km vs paper 3.69 / 9.03 / 15.33 km (+9/+7/+5%). All horizons 1–24 h pass the pooled §6.4 rule. Persistence beats CV from 2 h on the pooled underway set; CV wins for cargo and tankers. See `docs/coverage-audit.md`.

**Blockers / open questions (for review):**
- Accept the per-stratum admission rule, damped-velocity baseline and "transiting" stratum proposed in coverage-audit §6?
- Calibration passes with a reconstruction, not the paper's manifests. Try to obtain them before Milestone 7.
- The full-month DuckDB segmentation stalled while spilling to disk. Fixed by segmenting 16 vessel-hash buckets one at a time.

**Next step:** Milestone 1 first slice: Pydantic schemas plus P-unseen/P-paper split manifests with disjointness tests. Waiting for review before starting (AGENTS.md rule 11).

## 2026-10-19 — Build plan v0.3; Milestone 1 slice 1 (schemas and split manifests)

**Milestone:** 0 → 1
**Done:**
- Review accepted the coverage-audit §6 items 1–3. `docs/build-plan.md` is now **v0.3**: per-stratum horizon admission (§6.4), a damped-velocity baseline (§5 B0, Milestone 2), a transiting headline stratum (§6.3), and an updated success condition (§6.5). The survivorship check now compares mean SOG rather than median: with coverage ≥ 50% the evaluable set is the majority, so a median barely moves.
- `evaluation/admission.py` and `data/cutoffs.transiting_expr`. Segmentation v2 adds trailing 30-min SOG mean and min. The audit reports per-stratum admission and transiting-stratum errors.
- Milestone 1 slice 1: Pydantic `Observation` and `SplitManifest` (`schemas/`), protocol rules (`evaluation/splits.py`), `scripts/make_splits.py`, and committed manifests in `data/manifests/splits/`. `calibrate.py` now takes its vessel split from the P-paper rule.
- Tests: 57 unit tests. New ones cover the contract against canonical columns, vessel and period disjointness, purge-gap enforcement, window straddling, bucket tiling, and P-paper equality with the Milestone 0 rule.

**Commands run:**
```bash
uv run --project backend python scripts/coverage_audit.py --rebuild-segments  # coverage_audit_20261020T025237Z
uv run --project backend python scripts/make_splits.py
cd backend && uv run pytest && uv run ruff check src tests ../scripts
```

**Results:** Per-stratum admission: 1–24 h are first-class for all strata except pleasure_sail at ≥ 6 h (coverage 47%) and hsc_wig (too few cutoffs). Transiting stratum: CV beats persistence through 3 h (4.3 vs 18.8 km at 1 h), ties at 6 h, and loses after that. P-unseen in the cutoff region has 1,711 / 164 / 430 train / calibration / test vessels.

**Blockers / open questions:** The P-unseen calibration split is small (4 days, 164 cutoff-region vessels). Revisit when Milestone 4 needs per-stratum conformal radii.

**Next step:** Milestone 1 slice 2: promote ingestion into a versioned package API (generic CSV/Parquet column mapping and validation), and partition processed data by date and region with DuckDB query helpers.

## 2026-10-20 — Leakage review of the evaluator, sampler and splits

**Milestone:** 1
**Done:**
- Ran the `leakage-reviewer` instructions as a general-purpose subagent. The project agent was created mid-session, so it is not registered until a new session starts. Scope: segment, cutoffs, canonical, truth, windows, admission, splits/manifest, baselines, and the audit/calibrate/make_splits scripts.
- **Real issue (fixed):** the coverage audit silently dropped constant-velocity failures (missing COG/SOG produced NaN, nan-aware statistics skipped them, and `cv_failed` was always 0). Persistence and CV were therefore scored on different samples, against AGENTS.md rule 8. I confirmed 8,067 such evaluable rows in `coverage_audit_20261020T025237Z`. Fix: `evaluation/paired.py` (with a test). Failures are recorded per model and excluded for all models. The report adds a `CV failed` column.
- Deterministic dedup tie-break in `canonical.py`. Canonical row counts are unchanged.
- Latent risks are documented in code, not yet fixed:
  - The spike filter uses `lead()` (segment.py docstring), so model context must be segmented on data at or before `as_of`.
  - `evaluation/windows.py` is non-causal and P-paper-only: its last context point is interpolated toward a later report, and windows require full target coverage.
  - Materialized manifest vessel lists are audit-only and carry no time bound (`SplitManifest.vessels` description).
- `make_splits.py` output order is now deterministic. Manifest contents are unchanged apart from timestamps.

**Commands run:**
```bash
uv run --project backend python scripts/canonicalize.py --force
uv run --project backend python scripts/calibrate.py          # calibration_20261020T123430Z: PASS, unchanged 4.03/9.65/16.16 km
uv run --project backend python scripts/coverage_audit.py --rebuild-segments  # coverage_audit_20261020T123532Z
uv run --project backend python scripts/make_splits.py
cd backend && uv run pytest   # 58 passed
```

**Results:** Under paired scoring, underway persistence medians rise slightly (1 h 5.40 → 5.44 km; 24 h 12.93 → 13.05 km) and CV is unchanged. Every conclusion in coverage-audit.md stands. The audit now reports CV failures: 95–123 per underway horizon, 1 per horizon in the transiting stratum.

**Blockers / open questions:** none.

**Next step:** Milestone 1 slice 2: versioned ingestion API (generic CSV/Parquet column mapping and validation) and the partitioned store with DuckDB helpers. Milestone 2 must make context building causal: segment on data at or before `as_of`, and add the leakage invariance test the reviewer's B1 and B2 call for.
