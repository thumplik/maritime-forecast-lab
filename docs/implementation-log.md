# Implementation log

**Current milestone: 0 — Data spike and coverage audit** (started 2026-10-19; acceptance criteria met 2026-10-19, awaiting review before Milestone 1)

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
