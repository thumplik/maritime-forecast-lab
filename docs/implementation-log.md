# Implementation log

**Current milestone: 5 — Workbench UI** (in progress: slices 1–3, forecast comparison, benchmark dashboard and failure gallery, done 2026-10-22). Milestone 4 complete 2026-10-22. Milestone 3 complete 2026-10-21; retrieval v2 frozen on dev and scored once on the July hold-out (entries below). Milestone 2 completed 2026-10-21 (milestone check below). Milestone 1 completed 2026-10-20. Milestone 0 was completed and reviewed on 2026-10-19.

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

## 2026-10-20 — Milestone 1 slice 2: mapped ingestion and partitioned store

**Milestone:** 1
**Done:**
- `data/mapping.py`: versioned `SourceMapping` covering columns, timestamp parsing (timezone / format / epoch; explicit offsets win), SOG units, sentinels, value maps, vessel class from AIS code or text, and static-field provenance. Presets are `config/sources/noaa_marinecadastre.yaml` (verified) and `dma_denmark.yaml` (unverified, written from the published layout).
- `data/canonical.py` (canonical-v2) is now mapping-driven. `staged_sql`, `DROP_REASON_SQL` and `canonical_sql` are shared. `noaa_to_canonical_sql` is a thin wrapper. A new `bad_transceiver_class` flag nulls non-A/B values.
- `data/ingest.py`: files → store, with a balancing row-accounting `IngestReport`, and a `DatasetManifest` (`schemas/dataset.py`) holding inputs with SHA-256, the per-input report and rows per day. Ingest refuses to mix mappings in one dataset.
- `storage/parquet_store.py`: daily UTC partitions with date pruning; `scan` (half-open window, bbox, vessels, columns), `track`, causal `history(as_of, hours)`, paginated `vessels`, and atomic per-day `write` with an `expect_days` guard. Dataset ids, columns and timestamps are validated, and values are bound as parameters.
- `scripts/canonicalize.py` rewritten on the ingest API (one day per call, `expect_days`). New `scripts/import_ais.py` and `make import`.
- Tests: 87 unit tests (29 new: ingest and store, including injection and path-traversal guards).

**Commands run:**
```bash
cd backend && uv run pytest                           # 87 passed
uv run --project backend python scripts/canonicalize.py   # full June re-ingest via the new API (2 min 53 s)
python fingerprint.py data/processed/canonical/noaa_us_gulf_2023-06   # order-independent content hash
```

**Results:** The re-ingested June dataset is **identical** to the pre-refactor data: 87,377,629 rows, same content hash. Ingest report: 87,379,611 rows in, 0 dropped (the raw data was already bbox-filtered), 1,982 duplicates; flags bad_mmsi 43,541, sog_sentinel 99,146, sog_implausible 475. Store on real data: a 24 h causal history query takes 0.02 s, and a one-day bbox vessel summary takes 2.1 s.

**Blockers / open questions:** The DMA mapping is unverified. Verify it against one real DMA day before any Danish benchmark (the paper also reports Danish numbers). _(Resolved 2026-10-20, see the next entry.)_

**Next step:** Milestone 1 slice 3: `GET /health`, `/datasets`, `/vessels`, `/vessels/{id}/track` in FastAPI over `ParquetStore` and dataset manifests, with typed errors and input limits.

## 2026-10-20 — DMA mapping verified against a real day

**Milestone:** 1 (before slice 3)
**Done:**
- DMA 2023 is published only as monthly zips (June 2023 is 21.7 GB). New `scripts/fetch_dma.py` reads the zip directory and one day's member over HTTP ranges (zipfile checks the CRC). It handles daily zips for later years too and writes `data/manifests/dma_fetch.json`. Fetched `aisdk-2023-06-01.csv`: 2.05 GB, 11.8 M rows, 610 MB transferred.
- Profiled every column domain the mapping depends on, then ingested as `dma_2023-06`.
- The v1 mapping had three defects:
  - It kept 525 k non-vessel rows (base stations, AtoN, SAR aircraft, beacons).
  - It did not map ETA.
  - It missed three ship-type labels.
- Fixed in `dma_denmark` **v2**, which is now `verified: true`. Added a generic `drop_values` mapping option (counted as `excluded_by_mapping`). `DROP_REASON_SQL` became `drop_reason_sql(mapping)`.
- Tests: 88. New: the DMA preset on a synthetic file in DMA layout.

**Commands run:**
```bash
curl -r ... 2023/aisdk-2023-06.zip   # first manual member extraction; CRC 0x0e8cd484 verified
uv run --project backend python scripts/import_ais.py --mapping dma_denmark --dataset-id dma_2023-06 data/raw/dma/2023-06-01/aisdk-2023-06-01.csv
uv run --project backend python scripts/fetch_dma.py 2023-06-01   # reproduced the same SHA-256 b41f6ab4…
cd backend && uv run pytest   # 88 passed
```

**Results:** Every row is accounted for. Implied-speed/SOG ratio is 1.002 (Class A) and 0.995 (Class B), so units, axes and timing are consistent. Positions fall in Danish waters. Median cadence is 10 s for Class A, versus NOAA's about 60 s down-sampled floor. The full table is in `docs/data-contracts.md` under "DMA verification".

**Blockers / open questions:** That DMA timestamps are UTC is documented by DMA but cannot be proven by the internal checks.

**Next step:** Milestone 1 slice 3: FastAPI `GET /health`, `/datasets`, `/vessels`, `/vessels/{id}/track` over `ParquetStore` and dataset manifests.

## 2026-10-20 — Milestone 1 slice 3: read API

**Milestone:** 1
**Done:**
- `maritime_forecast.api`: FastAPI app factory (`create_app(ApiSettings)`), typed error envelope (`errors.py`), and response models (`models.py`, the OpenAPI source for the future TypeScript client). Endpoints: `GET /health`, `/datasets`, `/vessels`, `/vessels/{id}/track`. Limits and conventions are in `docs/architecture.md` under "Read API". `make api`.
- `data.segment.gap_segments` and `segments_and_gaps`: pure display segmentation. A gap of more than 30 min starts a new segment, and the track response lists every gap.
- Store fixes found while doing this:
  - **Performance:** a DuckDB relation built with bound params and then `.aggregate()`d re-materializes. The month-wide vessel list took 869 s; as one bound statement it takes 0.37 s. `scan()` now returns a Polars DataFrame, and `vessels()` returns `(page, total)`.
  - **Timezone bug:** DuckDB cursors do not inherit `SET TimeZone`, so times came back in local time (−04:00). `ParquetStore` now sets UTC on every connection. Audit and calibration runs use `connect()` directly, so earlier results are unaffected.
  - `store.write` ignores an incoming `date` column (hive auto-detection).
- Tests: 111. New: 24 API tests covering datasets/admission, paging, bbox, half-open windows, offset conversion, gap segments, every error code, input not echoed, OpenAPI paths; plus 4 display-segmentation tests.

**Commands run:**
```bash
cd backend && uv add fastapi uvicorn && uv run pytest     # 111 passed
python -m uvicorn maritime_forecast.api.app:app --port 8765   # smoke test with curl against NOAA + DMA
```

**Results (real data):**

| Request | Time |
|---|---|
| `/datasets` (both datasets; NOAA carries admission from `coverage_audit_20261020T123532Z`) | 14 ms |
| Month-wide Houston bbox vessel list (3,171) | 2.0 s |
| Whole-Gulf page of 1,000 | 0.30 s |
| Month track of vessel 367138690 (32,488 points, 18 segments) | 0.21 s |
| DMA day track | 16 ms |

A 404 on 2023-06-10 for that vessel was confirmed against raw counts: it has no reports on 06-10 or 06-11.

**Blockers / open questions:** none.

**Next step:** Milestone 1 slice 4:

- Generate TypeScript types from the OpenAPI schema.
- Build a minimal Next.js + MapLibre frontend that shows a real vessel's track with gaps visibly distinct.
- Add `make dev` to start both services.
- Add the CI smoke workflow.

## 2026-10-20 — Milestone 1 slice 4: web explorer, make dev, CI

**Milestone:** 1
**Done:**
- Installed Node 24.21.0 LTS in `~/.local/node`, checked against nodejs.org SHASUMS256. There is no Homebrew on this machine.
- `frontend/`: Next 16.3.8, React 19.3.0, MapLibre GL 6.12.0, TypeScript 5.9.3 and Vitest 5.0.3, all pinned exactly and all at least two weeks old. TypeScript 6 conflicts with openapi-typescript's peer range, so it stays on 5.9.
- Typed client generated from OpenAPI (`scripts/export_openapi.py` + openapi-typescript).
- Explorer page features:
  - dataset and UTC day/span pickers;
  - a paged vessel list with an MMSI filter and an "only vessels in the current map view" bbox;
  - a track summary with provenance warnings;
  - a legend.
- `lib/track.ts` is pure and tested (9 Vitest tests, including the 200 k-point API maximum).
- `make dev` (`scripts/dev.sh`, bash 3.2-safe), plus `make setup` / `web-test` / `gen-api`. `.python-version` is now 3.12 to match the env.
- `.github/workflows/ci.yml`: backend (ruff, pytest, OpenAPI drift) and frontend (TS drift, typecheck, vitest, build).

**Issues found during browser verification (fixed):**
- MapLibre 6 loads its web worker as a separate ES module that the bundler doesn't emit, so the map stayed blank. Fix: `scripts/copy-maplibre-worker.mjs` copies it to `public/maplibre/` and `setWorkerUrl` points there.
- Gap labels with `line-center` placement silently vanished on short gaps. They are now midpoint symbols with overlap allowed.
- Bounds used `Math.min(...xs)`, which overflows on large tracks. It is now a loop.
- Changing the map-view bbox now resets the vessel paging.

**Commands run:**
```bash
cd frontend && npm install && npm run gen:api && npm run typecheck && npm test && npm run build
./scripts/dev.sh   # then browser verification on real NOAA + DMA data
cd backend && uv sync --locked && uv run pytest   # 111 passed
```

**Results:** Verified in the browser on real data. Vessel 212314000 (cargo, NOAA, 2023-06-01 + 3 days) runs Houston Ship Channel → Galveston → Gulf → Sabine Pass → Port Arthur/Beaumont: 1,879 reports, 4 segments, 3 gaps (longest 53 min), with each gap amber and dashed and labelled. The DMA dataset lists 5,826 vessels. The map-view filter gave 1,396 vessels for the Houston view over 3 days.

**Blockers / open questions:** The Claude preview runner cannot execute from `/Volumes/Code` (macOS permission), so the dev servers were started from the shell. This doesn't affect `make dev`.

**Next step:** Close out Milestone 1:
- a synthetic fixture covering moving, turning, stopping, gaps and an antimeridian crossing, for unit tests only;
- a P-region manifest;
- confirm the "done when" criteria with `/milestone-check`.

## 2026-10-20 — Milestone 1 close-out: fixture, P-region, gaps, milestone check

**Milestone:** 1 → complete
**Done:**
- **Synthetic fixture** (`backend/tests/fixtures/`): generator plus committed CSV with six vessels (moving, turning, stopping/moored, gaps, antimeridian, dirty), MMSIs `999…`, unit tests only. It runs through every stage: ingest, store, segmentation, sampler, truth, P-paper grid, baselines, API.
- **Bugs the fixture found (fixed):**
  - **Truth interpolation across the antimeridian** returned about 0° longitude (the far side of the globe), and the P-paper grid had the same flaw. Fixed with `geo.geodesic.lon_lerp_sql`.
  - The spike-filter distance did not wrap longitude, so a lone report across the dateline was dropped as a "spike".
  - The web map would draw a dateline crossing as a line around the world, and its bounds would span the globe. It now unwraps longitudes.
  - Gulf data never crosses 180°, so no reported number changes. Calibration was re-run (`calibration_20261020T162749Z`): identical 4.03 / 9.65 / 16.16 km, PASS.
- **P-region** (`RegionSplit` in the manifest):
  - Test side is the Houston–Galveston box with a 50 km excluded buffer; train side is the rest of the Gulf. Combined with the periods.
  - Windows that straddle the cut are dropped. `sql_case()` is verified against `side()` on 3,000 random points.
  - Materialized: test 2,066 vessels / 6.83 M reports; train 6,994 / 31.38 M; calibration 4,610 / 7.87 M. No train or calibration report lies in the cutoff region.
- **Milestone check found two unmet tasks, now closed:**
  1. Pydantic schemas for forecasts and benchmark runs (`schemas/forecast.py`, `schemas/benchmark.py`). They encode: no truth in forecast responses, `similarity` not probability, conformal-only uncertainty, no fake points on failure, paired cells only. They are published to OpenAPI and TypeScript.
  2. Motion-state label (segment-v3) and causal stationary-jitter handling (`data/dejitter.py`).
- Tests: 150 backend + 2 golden, 10 frontend.

**Milestone 1 checklist (build plan §7):**

| Task / criterion | Evidence | Status |
|---|---|---|
| Monorepo, Makefile, lint, tests, `.env.example`, CI smoke | `Makefile`; ruff clean; `make test` 150 passed; `make web-test` 10 passed; `.env.example` and `frontend/.env.example`; `.github/workflows/ci.yml` green (run 39447357584) | met |
| `AGENTS.md` per §12 | `AGENTS.md` | met |
| Pydantic schemas: observations, forecasts, benchmark runs; generated TS types | `schemas/observation.py`, `forecast.py`, `benchmark.py`; `frontend/lib/api/schema.d.ts`; CI drift check | met |
| Versioned NOAA and generic CSV/Parquet ingestion with column mapping | `data/mapping.py`, `data/ingest.py`, `config/sources/*` (NOAA verified; DMA verified on 2023-06-01) | met |
| Validation, deduplication | `IngestReport` row accounting; `test_canonical`, `test_ingest`, fixture dirty rows | met |
| Jitter handling | `data/dejitter.py` (causal), motion_state label | met |
| Gap-aware segmentation | `data/segment.py`; fixture gap tests | met |
| Partitioned Parquet with DuckDB queries | `storage/parquet_store.py`; `test_store` | met |
| Cutoff sampler (§6.3) | `data/cutoffs.py`; `test_cutoffs` | met |
| Split manifests P-known, P-unseen, P-region, P-paper | `data/manifests/splits/*.json`; `test_splits` (17 + P-region) | met |
| Synthetic fixture (moving, turning, stopping, gaps, antimeridian), labelled synthetic | `tests/fixtures/`; `test_synthetic_fixture` | met |
| `GET /health`, `/datasets`, `/vessels`, `/vessels/{id}/track` | `api/app.py`; `test_api` | met |
| Minimal frontend: real track, gaps distinct | browser check 2026-10-20 (vessel 212314000) | met |
| **Done when:** one command starts both services | `make dev`, verified 2026-10-20 | met |
| **Done when:** real NOAA data loads and replays without fabricated segments | Re-ingested data has an identical content hash. API `n_points` equals the raw count (real: 579 for 367138690 on 06-12; fixture test). Gaps are their own layer | met |
| **Done when:** tests cover bad values, timestamp cutoffs, manifest disjointness | `test_canonical`/`test_ingest`/fixture dirty rows; `history()` causality, half-open windows, dejitter causality; `test_splits` | met |

**Leakage review of this change** (the `leakage-reviewer` subagent, now registered). It found no temporal leakage. It did find these, all fixed before commit:

1. **P-region selected test samples on their future.** `window_split` required every position, including post-cutoff labels, to be inside the box. A tanker leaving Houston after its cutoff would therefore be silently dropped from the test set, biasing it toward harbour traffic. Membership is now decided by context positions only. Train and calibration labels must still stay on the train side. Regression test added.
2. **`BenchmarkRun` pairing only compared counts.** Cells now also carry `sample_set_sha256`, and `n_vessels`, admission and hash must agree. Undeclared models are rejected. A succeeded run must cover every model × horizon × stratum. `not_evaluable` is now by horizon.
3. **`ModelForecast`, `HorizonScore` and `ForecastEvaluation` holes closed:**
   - an "ok" forecast with no prediction;
   - a failed forecast that still ships trajectories or uncertainty;
   - negative radii, unordered grids, and trajectories off the grid;
   - an evaluable score with no error, or a not-evaluable score with no reason;
   - per-horizon silent drops;
   - responses with input after `as_of` or targets at or before it.
4. **Failure rate and headline eligibility:** cells now report `failure_rate`, and only cells with zero failures on an admitted horizon can be `headline`.
5. **Dejitter is causal only on causally segmented input** (the spike filter uses the next report). This is now documented, with a test of the prescribed path that has a spike right after the cutoff.

Nits fixed:
- the latitude buffer pad now uses the shortest meridian degree;
- `sql_case` treats NULL coordinates as excluded;
- `test_region` is a typed box;
- P-region forbids vessel assignment;
- materialization asserts day-aligned periods;
- stable sort in dejitter;
- the float-safe step-grid check.

The departure-lag bias of dejitter is documented (not leakage).

**Commands run:**
```bash
cd backend && uv run python tests/fixtures/synthetic.py && uv run pytest   # 150 passed after review fixes
uv run --project backend python scripts/make_splits.py                     # P-region materialized
uv run --project backend python scripts/calibrate.py && (cd backend && uv run pytest -m golden)  # PASS, 2 passed
make lint && make web-test && make gen-api
```

**Results:** All Milestone 1 tasks and acceptance criteria are met (table above).

**Blockers / open questions:** none.

**Next step:** Smallest first slice of Milestone 2, waiting for your go-ahead (AGENTS.md rule 11):
- the `TrajectoryForecaster` protocol and registry, with persistence and both constant-velocity modes as adapters;
- the leakage-invariance test harness (perturb or delete post-cutoff rows; every adapter's output must be unchanged), built on `ParquetStore.history()`.

## 2026-10-20 — Milestone 2 slice 1: forecaster contract, B0 adapters, leakage-invariance harness

**Milestone:** 2 — Baselines and the evaluator
**Done:**
- `models/base.py`: `TrajectoryForecaster` protocol.
  - `AISHistory` raises `LeakageError` on any row after `as_of`; `history_from_store()` builds it over `ParquetStore.history()`.
  - Also defines `ForecastHorizon` and `ModelCapabilities` (`reads_beyond_own_history` is required).
  - `BaseForecaster` checks requests against capabilities and turns unsupported cases, exceptions and non-finite outputs into typed `ModelForecast` failures.
- Adapters:
  - `persistence_v1`;
  - `constant_velocity_v1` (standard: anchor on the latest usable SOG/COG report, lead from its own time, skips `sog_implausible`, stale and fallback warnings);
  - `constant_velocity_paper_v1` (last report extrapolated from `as_of`, no fallback; the causal counterpart of the §6.6 reconstruction, documented).
- `models/registry.py`: factories have the shape `(store, dataset_id)`.
- `evaluation/invariance.py`: the leakage-invariance harness (see `docs/evaluation-protocol.md`).
- Tests:
  - 169 unit tests plus 1 strict xfail. The xfail documents that a process-global cache would hide a leak.
  - 3 golden tests.
  - Controls: the peeking model is caught, the archive bypass is blocked, a perturbation that edits the past is rejected, and vacuous cases are reported.
  - `history()` boundary is tested to the microsecond.

**Leakage review** (`leakage-reviewer`): no live leak in the models or `history()`. Eight harness and contract weaknesses were fixed before commit:

1. Store-bypassing models could skip the harness (fix: factories take the store; the real archive is blocked during checks).
2. Checks could be vacuous (fix: must-change self-check, `vacuous` and `ok_comparisons` reporting).
3. Boundary probed only at +1 s (fix: +1 µs and +1 ms spikes, plus a µs boundary test).
4. One horizon/step and a narrow scope (fix: 3 grids; whole-dataset scope for models that read beyond their own history).
5. Run order (fix: perturbed first, fresh instances). A process-global cache remains a documented limitation.
6. Exceptions were not typed (fix: typed `exception` / `non_finite_output` failures).
7. The paper adapter vs `calibrate.py` difference was undocumented (fix: documented).
8. `reads_beyond_own_history` defaulted to False (fix: now required).

Nits fixed: paper-mode stale warning; standard mode skips `sog_implausible` anchors.

**Commands run:**
```bash
cd backend && uv run pytest                 # 169 passed, 1 xfailed
uv run pytest -m golden                     # 3 passed (calibration, real-data invariance) in 2 min 42 s
```

**Results:** Real-data invariance: 6 NOAA cutoffs × 3 grids × 3 models = 378 checks. No violations, no vacuous cases.

**Blockers / open questions:** none.

**Next step:** Milestone 2 slice 2:
- the evaluator runner (paired scoring over the sampler, P-known/P-unseen manifests and `truth_sql`, with `not_evaluable` reasons by horizon, vessel-bootstrap CIs and stratification);
- a `BenchmarkRun` record written to an immutable run store;
- a first unattended baseline benchmark on June 2023 (P-known and P-unseen).

Then damped velocity (τ fit on train only) and constant turn rate.

## 2026-10-20 — Milestone 2 slice 2: evaluator runner and first baseline benchmarks

**Milestone:** 2
**Done:**
- `evaluation/runner.py`: the unattended paired benchmark (see `docs/evaluation-protocol.md` § Runner).
- `data/cutoffs.cutoff_candidates_sql` uses causal features. `models/base.histories_for_cutoffs` batches histories and is equivalence-tested against `ParquetStore.history()`.
- `evaluation/bootstrap`:
  - multinomial-weight vessel bootstrap;
  - `paired_vessel_bootstrap` with shared resamples;
  - the weighted median now matches `np.median`. An earlier lower-median version gave a CI that excluded its own estimate; a test caught it.
- `storage/run_store.py`: write-once, read-only after finalize, SHA-256 of every file including `run.json`, failed runs recorded.
- `scripts/run_benchmark.py`, `make benchmark`.
- Schema changes:
  - `CellResult` gains `n_evaluable`, `n_decomposed`, `best_baseline_id`, and a skill `Stat` with CI.
  - `BenchmarkRun.dropped_cells` added.
  - The headline failure tolerance is 1%.
- Tests: 186 + 1 xfail.

**Leakage review** found no leakage, but eight integrity problems, fixed before any result was kept (the smoke run and an interrupted first full run were discarded):

1. Headline was per-model, although the paired set was shaped by every model's failures. It is now per cell.
2. Small cells were dropped silently. They are now recorded in `dropped_cells`, decided on the evaluable count.
3. Along/cross medians covered an unreported subset. `n_decomposed` is now reported, and the exact-hit course comes from the hit report's COG.
4. Skill was labelled against a damped velocity that does not exist yet. It is now computed only when every registered baseline is present, records `best_baseline_id`, and has a paired CI.
5. The "paired CI" wording was wrong. It now says "per-model CIs from shared resamples"; the paired contrasts are the skill CIs.
6. `run.json` was not checksummed, and failed runs left orphan directories. Both fixed.
7. Sample hashes used run-local ids. They now use `vessel|as_of|horizon`.
8. Models capped below the longest horizon would have failed everywhere. They are now refused before the run is created.

Nits fixed: a window straddling another period is rejected; truth is filtered to the truth region.

**Commands run:**
```bash
cd backend && uv run pytest                     # 186 passed, 1 xfailed
make benchmark PROTOCOL=P-known                 # 22,639 cutoffs, 50 min (bootstrap-bound)
make benchmark PROTOCOL=P-unseen                # 4,977 cutoffs, 3.5 min
```

**Results:** run ids, `files.json` digests and tables are in `docs/benchmarks.md`.
- Pooled underway: persistence and CV are tied at 1 h, and persistence wins from 2 h (paired CIs exclude 0).
- Transiting: CV wins 1–3 h, they tie at 6 h, and persistence wins at 12–24 h.
- P-known and P-unseen agree.

**Blockers / open questions:**
- The P-known run took 50 min. _(Resolved 2026-10-21: the cause was not the bootstrap; see the next entry.)_
- Recommendation: drop `constant_velocity_paper_v1` from product benchmarks; its stationary failures make every stationary cell non-headline.

**Next step:** Milestone 2 slice 3:
- damped velocity (τ fit per class on the TRAIN split only, with a protocol-leakage test);
- constant turn rate;
- `GET /models`, `POST /forecasts` and `/benchmarks` endpoints;
- feasibility flags (navigable-water mask);
- the GeoJSON export.

## 2026-10-21 — Benchmark runner speed-up (50 min → 66 s), results reproduced

**Milestone:** 2
**Done:**
- Profiled before optimizing. The planned fix (vectorize the bootstrap) was based on a wrong guess: the bootstrap takes 0.14 s per cell, about 30 s per run.
- Stage timing on a 4,000-cutoff run: `truth_grid` 387 s, `select_cutoffs` 33 s, predictions 12 s, bootstrap 1.8 s.
- **Fix 1:** the ASOF join in `truth_grid` read the targets from a registered Arrow object. The same query against a materialized temp table is about 1,800× faster (382 s → 0.21 s for 576 k targets). The targets are now materialized; results are unchanged by construction. `coverage_audit.py` already materialized its targets.
- **Fix 2:** cutoff selection called `window_split` in Python for 6.8 M candidate rows. `SplitManifest.window_splits` does the same in polars. A test checks it row for row against the scalar rule on 4,000 random windows for P-known, P-unseen, P-paper and a manifest with an interior period.
- Gotcha for CLAUDE.md: never run ASOF joins (or other heavy joins) against a registered Arrow or polars object; materialize first.

**Commands run:**
```bash
cProfile + per-stage timing on a 4,000-cutoff P-known run
cd backend && uv run pytest                       # 187 passed, 1 xfailed
make benchmark PROTOCOL=P-known                   # 66 s (was 50 min)
make benchmark PROTOCOL=P-unseen                  # 21 s (was 3.5 min)
RunStore.load() on old and new runs, then compared run.json, cutoffs and records
```

**Results:** Both re-runs are **identical** to the kept runs: run.json fields (except id, time, SHA, timings), all 381 / 375 cells, dropped cells, not-evaluable counts, `cutoffs.parquet`, and `records.parquet` (407,502 rows for P-known). New digests are in `docs/benchmarks.md`.

**Blockers / open questions:** none.

**Next step:** Milestone 2 slice 3:
- damped velocity (τ per class fit on the train split only, with a protocol-leakage test);
- constant turn rate;
- `GET /models` and `POST /forecasts`;
- drop paper-mode CV from product benchmarks.

## 2026-10-21 — Milestone 2 slice 3: damped velocity, constant turn rate, /models and /forecasts

**Milestone:** 2
**Done:**
- **`damped_velocity_v1`**: v(t) = SOG·e^(−t/τ), with τ per vessel class.
  - `models/fit_damped.py` and `make fit PROTOCOL=...` fit τ on the protocol's **train split only**, from a grid of 0.02 h to ∞. Truth is capped at the end of the train period (`truth_until`), so the purge gap cannot leak calibration or test data into the fit.
  - The fit records `manifest_sha256` and `data_until`. The model refuses a fit made for another manifest, and refuses any `as_of` before `data_until` (`fit_overlaps_as_of`).
  - The runner refuses to benchmark on the split a model was fitted on.
  - Fits for P-known and P-unseen are committed in `data/model_params/damped_velocity_v1/`. The τ table is in `docs/model-integration-notes.md`.
- **`constant_turn_rate_v1`**: median turn rate over the last 10 min, capped at 0.05°/s and decaying with a 20 min time constant, integrated in ENU and then projected geodesically. Reports flagged `sog_implausible` are excluded.
- **API**:
  - `GET /models` lists capabilities and parameters in use.
  - `POST /forecasts` serves causal forecasts. Its cache key covers the request, model configs and versions, the data version (manifest, inputs and partition file stats) and the code SHA. It uses the P-known fit.
  - OpenAPI and TS types were regenerated; the Makefile `gen-api` target was fixed.
- **Paper-mode CV** is `calibration_only` and excluded from `product_model_ids()`. All stationary cells are now headline.
- **Capability flags**:
  - `reads_beyond_own_history` and `uses_store_at_predict` are now required, and the second implies the first.
  - The invariance harness's whole-dataset scope keys on `uses_store_at_predict`. Damped velocity does not query the store, so its golden run takes minutes instead of hours.
- **Leakage review (leakage-reviewer subagent).** All findings were fixed in `bee9b53`:
  - The API applied the P-known τ to cutoffs inside its own train period. These are now refused.
  - The harness could pass a model that never returns `ok`. Each model now needs an ok-fraction of at least 0.8, and uninstalled models are refused.
  - Fit truth depended on the purge gap. It is now capped at the end of the train period.
  - A benchmark on the fit's own split was possible. It is now refused.
  - The fit tests had no teeth. They were rewritten on a thinned fixture, and verified to fail with the truth cap removed. P-unseen non-vacuity uses TURNING as the test vessel.
  - The cache key now includes the code SHA. CTR now ignores flagged reports.
- Refit τ from a clean tree (`a3022ac`): the τ values are identical, and the fit file now records `data_until`.
- Incident: a killed golden run left 18 GB of DuckDB spill in `backend/.tmp`, which hung `git add`. The spill was deleted and `.tmp/` gitignored (noted in CLAUDE.md).

**Commands run:**
```bash
make fit PROTOCOL=P-known && make fit PROTOCOL=P-unseen
make benchmark PROTOCOL=P-known     # 95 s, 22,639 cutoffs
make benchmark PROTOCOL=P-unseen    # 27 s, 4,977 cutoffs
cd backend && uv run pytest         # 214 passed, 3 deselected, 1 xfailed
cd backend && uv run pytest -m golden   # 3 passed (137 s)
make lint
```

**Results:** see `docs/benchmarks.md` (2026-10-21 section). The post-review runs match the pre-review runs cell for cell.
- Transiting stratum: damped velocity is best from 6 h, at 33–37 km against 53–62 km for persistence, with CIs excluding zero in both protocols. CV is still best up to 3 h.
- Pooled underway: damped velocity wins at 1 h. From 3 h onward persistence is slightly better.
- CTR is slightly worse than CV everywhere.

**Blockers / open questions:** none.
- τ is one value per class, tuned mostly on 12–24 h errors. A per-horizon or per-motion-state τ is possible, but it would be a different baseline; not pursued.

**Next step:** remaining Milestone 2 items:
- `POST /benchmarks` and `GET /benchmarks/{id}[/summary]`;
- the forecast evaluation endpoint;
- navigable-water mask and feasibility flags;
- GeoJSON export from the CLI;
- then the milestone check.

## 2026-10-21 — Milestone 2 slice 4: forecast evaluation and benchmark endpoints

**Milestone:** 2 — baselines and the evaluator
**Done:**
- `POST /forecasts/evaluation` (`api/app.py`, `evaluation/forecast_eval.py`). It takes the same `ForecastRequest` as `POST /forecasts` and scores the forecast that endpoint serves (same `forecast_id` and cache, shared `_forecast` helper).
  - Truth is read only after the forecast exists, with the benchmark's rule. `runner.truth_grid` is generalized to `truth_on_grid`.
  - Every target time is reported for every model: the truth reason where there is no truth, `model_failed` where truth exists but a model gave nothing.
  - `paired` marks the target times every model scored; `ade_m` averages only those.
- `ForecastEvaluation` contract (`schemas/forecast.py`) gains `target_times`, `truth_reasons`, `paired`, per-model `status` and `ade_m`. Its validators reject silent drops, a mis-stated `paired`, and truth/reason mismatches.
- `POST /benchmarks` → 202 + `Location`. It runs the runner's `benchmark_context` + `preflight` synchronously (both extracted from `run_benchmark`), so anything the CLI refuses is a 422 `benchmark_refused` before a process starts.
  - The run is `run_benchmark` in a spawned daemon process (`api/jobs.py`), one at a time (409 `benchmark_busy`, guarded by a lock).
- `GET /benchmarks/{id}`: `running` / `incomplete` / `succeeded` / `failed`, with the full record (checksums verified on every read; 409 `run_tampered`).
- `GET /benchmarks/{id}/summary?stratum&headline_only`: one row per paired cell, every model side by side. It includes median + CI, p90, `n_failed`, failure rate, skill + paired CI, admission, headline, filtered dropped cells and `max_cutoffs_per_set`. A cell whose shared fields differ between models is refused (500 `run_inconsistent`).
- The runner labels smoke runs (`max_cutoffs_per_set`) in `run.warnings`. `RunStore.path()` added.
- `export_openapi.py` no longer publishes contracts separately (all are served). TS types regenerated; new types exported from `frontend/lib/api/client.ts`. `docs/architecture.md` updated.
- Tests: `test_api.py` (evaluation: exact persistence error, gaps, model failures, mid-gap `as_of`, no truth in forecasts), `test_api_benchmarks.py` (status, summary pivot, tampering, refusals, a real spawned background run, smoke label), `test_contracts.py`.

**Leakage review (leakage-reviewer subagent):** no leakage. Fixed:
1. Truth scan started at the cutoff, so an API `as_of` between reports lost the report bracketing its first targets (and that report's spike test lost its predecessor). It now starts one bracket gap + 10 min earlier. The new regression test was verified to fail without the fix. Benchmark results are unchanged (below).
2. `evaluate_forecast` asserts the target grid is `as_of + k·step` (the grid truth is read on).
3. A warning when the vessel has no report in the truth region before any target (e.g. a non-Gulf dataset).
4. Smoke runs are labelled in the run and the summary.
5. The summary carries dropped cells, `n_evaluable` and per-model `n_failed`.
6. The summary checks cell fields are identical across models.
7. A lock around the busy check in `BenchmarkJobs.start`.

Not changed:
- `preflight` accepts an explicit `calibration_only` model, as the CLI does.
- Truth is filtered to the region after segmentation, so a vessel that leaves the region for < 30 min is bracketed across its absence. This is pre-existing and affects both paths.

**Commands run:**
```bash
cd backend && uv run pytest -q          # 233 passed, 3 deselected, 1 xfailed
make lint && make gen-api && make web-test
make benchmark PROTOCOL=P-known         # 95 s, after the truth-window fix
make benchmark PROTOCOL=P-unseen        # 28 s
```
Real-data smoke through the app (scratch script, not committed):
- `/forecasts/evaluation` on a transiting cutoff: 6 h, 3 models, 0.14–0.18 s, 36/36 paired.
- `POST /benchmarks` P-unseen with `max_cutoffs_per_set=200`: succeeded in the background in 12 s.
- The smoke run was deleted.

**Results:** the post-fix re-runs are identical to the kept runs `P-known_noaa_us_gulf_2023-06_20261021T021006Z` and `P-unseen_noaa_us_gulf_2023-06_20261021T021141Z`: every cell (508 / 500), records, cutoffs, dropped cells and not-evaluable counts. They were duplicates, so they were deleted; `docs/benchmarks.md` is unchanged.

**Blockers / open questions:** none.
- Processes use `spawn`: an ad-hoc script that drives the app needs a `__main__` guard (documented in `api/jobs.py`).
- The truth region is the default `us_gulf` for every dataset. A per-dataset truth region is needed before evaluating imported non-Gulf data.

**Next step:** remaining Milestone 2 items:
- navigable-water mask and feasibility flags;
- GeoJSON export from the benchmark CLI;
- then `/milestone-check`.

## 2026-10-21 — Milestone 2 slice 5: navigable-water mask, feasibility flags, GeoJSON export

**Milestone:** 2 — baselines and the evaluator
**Done:**
- `scripts/fetch_land_polygons.py` (`make fetch-land`) streams OSM `land-polygons-split-4326` (ODbL, 885 MiB) with resume. It keeps the 19,175 polygons near `us_gulf` and deletes the zip. The manifest is `data/manifests/osm_land_us_gulf.json` (SHA-256 `e8224df7b334…`).
- **Finding:** OSM land polygons count channels, rivers and lakes behind the coastline as land. On their own they put 74% of stationary and 51% of underway real Houston reports on land. The Houston Ship Channel at Baytown is "land"; Galveston Bay is water.
- `geo/navigable_mask.py`:
  - The land raster is 0.001° (≈ 100 m), cached, and matches exact point-in-polygon on 99.94% of random points.
  - Traffic cells (≥ 2 distinct vessels in the protocol's train split; P-unseen: train-assigned vessels only), dilated 1 cell, are removed from land.
  - Point and segment tests, a bit-packed save, and a checksummed sidecar in `data/manifests/masks/` (now un-ignored).
  - Parameters were chosen on each protocol's calibration split with `scripts/tune_navigable_mask.py`. The table is in `docs/evaluation-protocol.md` § Feasibility.
- `evaluation/feasibility.py`: per-step flags for land point, land crossing (half-cell sampling along lon/lat lines), implied speed > 60 kn, and acceleration > 2 kn/min.
- Runner:
  - `preflight` refuses a mask fitted for another manifest, or not on train.
  - Flags are cumulative over the grid. Per-cell `FeasibilityStats` cover the paired samples and include a cumulative `truth_on_land_rate` (the mask's own error).
  - Records gain horizon `pred_*`/`truth_*` positions and `feas_*` columns, null where a model failed.
  - The run records its mask, warns when there is none, and has a feasibility table in `report.md`.
- `POST /forecasts/evaluation` returns per-model `PathFeasibility` against the P-known mask. Land flags are null when `as_of` is inside that train split. The benchmark summary carries feasibility.
- `evaluation/export.py`, `scripts/export_geojson.py`: GeoJSON per run under `data/runs/exports/` with a sidecar.
  - Predictions are LineStrings from the anchor through the horizon points, labelled as chords. Truth is a MultiPoint, never a line.
  - `make benchmark` exports the paired transiting stratum.
- Dependencies: `shapely`, `pyshp`. Makefile: `fetch-land`, `mask`.
- Tests: 246 (+13) — rasterization, mask logic, thin-land segments, P-unseen and time filters on traffic, runner integration, refusal, GeoJSON, API null-in-window.

**Leakage review (leakage-reviewer subagent):** no leakage, and no error metric depends on the mask. Fixed:
1. The truth floor was single-point while model rates are cumulative; it is now cumulative. Two runs made before this fix were deleted.
2. API land flags could reflect the target's own post-`as_of` track (inside the P-known train window); they are now null there. A warning was added when there is no anchor.
3. The tuning measurement is committed and was re-checked on P-unseen's calibration split (calibration vessels).
4. GeoJSON `paired_only` now nulls unpaired errors.
5. Only train-split masks are accepted.

**Commands run:**
```bash
uv add shapely pyshp
uv run --project backend python scripts/fetch_land_polygons.py --keep-zip   # zip deleted afterwards
make mask PROTOCOL=P-known && make mask PROTOCOL=P-unseen
uv run --project backend python scripts/tune_navigable_mask.py --protocol P-known   # and P-unseen
make benchmark PROTOCOL=P-known     # 120 s
make benchmark PROTOCOL=P-unseen    # 33 s
make test && make lint && make gen-api && make web-test   # 246 passed
```

**Results:** runs `P-known_noaa_us_gulf_2023-06_20261021T034634Z` and `P-unseen_noaa_us_gulf_2023-06_20261021T034836Z` (digests in `docs/benchmarks.md`).
- Every error metric, record and cutoff is identical to the previous kept runs.
- Pooled underway (P-known), share on infeasible land by 1 h → 24 h: CV 61% → 88%, damped 26% → 29%, persistence 0.3%; truth floor 1.4% → 8.1%.
- Transiting: CV 30–35% at 1 h.
- Implausible speed is 0% everywhere.

**Blockers / open questions:** none.
- The truth floor grows to about 8% at 24 h in pooled port traffic (docks, cells no train vessel visited). A denser source (NOAA ENC, or OSM inland water polygons via a regional extract) would lower it. Not needed to read the current, much larger model rates.
- P-region masks are not built (P-region benchmarks are not built either).

**Next step:** `/milestone-check` for Milestone 2. The open "done when" items to verify: calibration passes, and every model passes the invariance test with the current code.

## 2026-10-21 — Milestone 2 check: complete

**Milestone:** 2 — baselines and the evaluator
**Done:** verified every §7 Milestone 2 task and "done when" criterion against current `main` (`1907912`).

| Criterion | Evidence | Status |
|---|---|---|
| Persistence; CV (standard and paper-compatible); constant turn rate; damped velocity with τ per class fitted on train | `models/registry.py` (5 models). Fits in `data/model_params/damped_velocity_v1/` (P-known, P-unseen, train split). `test_baselines.py`, `test_fit_damped.py` | met |
| Evaluator: paired scoring, `not_evaluable`, ADE/FDE (horizon error), along/cross-track, vessel-bootstrap CIs, stratification | `evaluation/runner.py`, `bootstrap.py`; `BenchmarkRun` validators; `test_runner.py`, `test_bootstrap.py` | met |
| Feasibility flags against the navigable-water mask; OSM polygons plus a training-only AIS density mask | `geo/navigable_mask.py`, `evaluation/feasibility.py`, `data/manifests/masks/*.json`, `test_feasibility.py`, `docs/evaluation-protocol.md` § Feasibility | met |
| `make calibrate` and the golden test | `make calibrate` → `calibration_20261021T103221Z` **PASS**: primary CV 4.03 / 9.65 / 16.16 km vs 3.69 / 9.03 / 15.33 (+9% / +7% / +5%, tolerance ±10%); `pytest -m golden` 3 passed in 334 s | met |
| `POST /forecasts` with a deterministic cache; `POST /benchmarks`, `GET /benchmarks/{id}`, `/summary` | `api/app.py`, `api/jobs.py`; `test_api.py`, `test_api_benchmarks.py` (including a real spawned run) | met |
| Unattended CLI benchmarks exporting JSON and GeoJSON | `make benchmark` → `run.json` + `report.md` + `exports/<run_id>/transiting.geojson`; `scripts/export_geojson.py` | met |
| Leakage invariance: every model unchanged when post-cutoff data is altered or deleted | `test_invariance.py` (synthetic), `tests/golden/test_invariance_real.py`: all 5 registered models on real cutoffs, ok-fraction ≥ 0.8, no vacuous cases. **Passed on current code** | met |
| **Done when:** unattended benchmarks of the baselines on P-known and P-unseen, persisted | `P-known_noaa_us_gulf_2023-06_20261021T034634Z`, `P-unseen_noaa_us_gulf_2023-06_20261021T034836Z` (digests in `docs/benchmarks.md`) | met |
| **Done when:** passes calibration | above | met |
| **Done when:** every model passes the invariance test | above | met |

**Commands run:**
```bash
make test        # 246 passed, 3 deselected, 1 xfailed
make lint        # clean
make calibrate   # PASS; golden: 3 passed (333.6 s)
```

**Results:** calibration run `calibration_20261021T103221Z`, numbers above. They are identical to the earlier calibration runs.

**Blockers / open questions:** none blocking.
- The calibration margin at 1 h is +9% against a ±10% tolerance (unchanged since Milestone 0; explained in `docs/coverage-audit.md`).
- Not in Milestone 2 scope and deferred: P-region benchmarks and masks; a per-dataset truth region for non-Gulf imports.

**Next step (smallest first slice of Milestone 3):** the protocol-aware segment index (§5 R1, §6.2), with leakage tests and no forecasting yet:
- index completed segments that end strictly before the cutoff;
- P-unseen excludes test-assigned vessels; P-known allows the target's own earlier voyages;
- tests prove no post-cutoff or test-vessel segment is ever eligible.

Then a plain kNN `route_retrieval_v1` (alignment plus a median continuation, similarity labelled as such), behind the forecaster contract and the invariance harness.

## 2026-10-21 — Milestone 3 slice 1: protocol-aware retrieval segment index

**Milestone:** 3 — historical-route retrieval
**Done:**
- `data/segment_index.py`, `scripts/build_segment_index.py` (`make index`): one row per completed gap-segment (≥ 3 reports, ≥ 10 min).
  - Each row has its endpoints, bbox, mean SOG, a deterministic class with back-filled provenance, and a 10-min track interpolated only inside the segment.
  - The sidecar is `data/manifests/segment_index/<dataset>.json` (now un-ignored).
- `eligible(index, as_of, target, manifest, pool)` (rules in `docs/evaluation-protocol.md` § Retrieval eligibility):
  - `known_at = end + 2 × gap ≤ as_of`.
  - **Frozen pool by default:** `known_at ≤ train end`, so the retrieval index period is the train period as §5 U1 requires. Rolling is a labelled variant.
  - P-known allows the target's own voyages. P-unseen uses train-assigned vessels only, never the target. P-region is refused.
- `segmented_sql(..., causal_ends=True)`: the spike test never looks across a gap. Off by default, so truth and benchmarks are unchanged.
- Tests:
  - 11 unit tests: full-archive vs truncated equivalence on the fixture plus adversarial tracks; teeth for the naive `end + gap` rule and for `causal_ends=False`; the known_at boundary; tracks never bridging; the frozen pool; P-unseen and target exclusion; determinism and provenance; P-region refused.
  - 4 golden tests: real-data equivalence for both protocols and pools; a test-period cutoff with real test- and train-assigned targets.

**Leakage review (leakage-reviewer subagent):** no temporal leak; `end + 2 × gap` is sufficient for every input traced. Fixed:
1. The rolling pool conflicted with §5 U1 (calibration must be disjoint from the index period). The frozen pool is now the default, which follows the spec, so no spec change was needed.
2. `any_value(vessel_class)` was nondeterministic (a real vessel has mixed classes). It now uses the mode with a tie-break, the same rule as the models.
3. Class provenance (back-filled) is now carried.
4. The `segment.py` docstring now states the 2 × gap bound.
5. Tests strengthened: test-period cutoffs, real targets, explicit `causal_ends` teeth, the target's ongoing segment excluded.

Also found while testing: a parallel `avg()` made `sog_mean` differ at 1e-14 between identical builds. It is now an ordered `list_avg`.

**Commands run:**
```bash
make index                                   # 240,798 segments, 15,964,045 track points, 252 MB, 102–138 s, ~11.5 GB peak RSS
cd backend && uv run pytest -m golden tests/golden/test_segment_index_real.py   # 4 passed
make test && make lint                       # 257 passed
```

**Results:**
- At a late-June test cutoff: 208 k eligible segments (8,739 vessels) under the rolling P-known pool, and 143 k under rolling P-unseen.
- The frozen pool keeps only segments known by Jun 17.
- Segments by class: tug_tow 65 k, pleasure_sail 54 k, other 36 k, fishing 22 k, passenger 18 k, tanker 17 k. 30% have a mean SOG of at least 2 kn.

**Blockers / open questions:** none.
- Peak memory for the full build is about 11.5 GB of 24 GB. Chunking by vessel would lower it if datasets grow.

**Next step:** `route_retrieval_v1` (§5 R1) on the frozen index:
- plain kNN over local-frame recent track shape, position, course, speed and class;
- align the references at the current position and continue them; top-K candidates plus a medoid central path;
- `similarity` labels and reference segment ids as provenance;
- registered behind the forecaster contract (`reads_beyond_own_history=True`), passing the invariance harness;
- then a paired benchmark against B0 under P-known and P-unseen.

## 2026-10-21 — Milestone 3 slice 2: `route_retrieval_v1` and its first benchmark

**Milestone:** 3 — historical-route retrieval
**Done:**
- `models/route_retrieval.py`, registered as `route_retrieval_v1` (§5 R1); design in `docs/model-integration-notes.md`.
  - **Pool:** the model builds its own pool from the store, using data up to `pool_until(as_of)` only. The default frozen pool ends at train end, the target-independent `eligible()` is applied when the pool is built, and the P-unseen target exclusion per query. The pool is cached per process, keyed by data version, behind a lock.
  - **Matching:** kNN over 10-min track edges within 1 km of the anchor (3 km as a fallback), with continuous alignment. Scored on offset, the last 30 min of track shape, course, speed and class. Top 10, at most 3 per vessel.
  - **Continuation:** references are translated in an anchor-centred AEQD frame (new `geo.geodesic.LocalFrame`) and followed forward. A reference holds its last position after its segment ends (`reference_ends_at`).
  - **Output:** the central path is the similarity-weighted medoid. `trajectories` carry `similarity` (not probability) and `reference_segment_ids`; `central_trajectory_id` names the medoid.
  - **Failure:** no reference nearby is a typed `no_references` failure, with no silent fallback.
- **Contract additions:**
  - `BaseForecaster._predict` may return extra forecast fields.
  - Schema: `CandidateTrajectory.reference_ends_at`, `ModelForecast.central_trajectory_id`.
  - `build_segment_index(start=)`; `check_invariance(wide_days=)`.
  - The runner's preflight refuses `diagnostic_only` model configs (rolling pool, `index_days`).
- **Tests:**
  - 13 unit tests (`test_route_retrieval.py`) on a synthetic lane fixture:
    - branch following, provenance and similarity, held references;
    - P-unseen exclusion, frozen-pool bounds;
    - frozen forecast byte-identical when other vessels' test-period data **before** the cutoff are deleted or shifted (rolling differs: teeth);
    - deterministic pool build, typed failures, the cache data-version key, synthetic invariance, `LocalFrame` round trips.
  - 3 golden tests (`test_route_retrieval_real.py`):
    - every reference used by 150 real test cutoffs per protocol has `known_at ≤ train end`; under P-unseen every reference is from a train vessel other than the target;
    - the model's pool equals the prebuilt index filtered by `eligible()`.
  - The real-data invariance golden test now includes retrieval.
- **Docs and rules:**
  - Docs: `model-integration-notes.md`, `evaluation-protocol.md`, `benchmarks.md`, README (results, figures, roadmap), `CLAUDE.md` status.
  - `AGENTS.md` rule 13 (user request): update the README results, figures and roadmap after every improvement that produces new kept benchmark results, from immutable runs only, losses included.

**Leakage review (leakage-reviewer subagent):** no temporal or protocol leak. Fixed:
1. Rolling or truncated pools could have been benchmarked as headline. They are now `diagnostic_only` and refused by preflight.
2. The production frozen pool was not tested against test-period data before the cutoff. A unit test now covers it, with teeth.
3. API thread safety: a lock around pool builds.
4. Determinism without the cache: a test.
5. The prefilter reduced to one edge per segment on an approximate metric. Pass 1 now keeps 4 per segment and pass 2 scores exactly.
6. `reference_ends_at` when a reference covered no target time.
7. `class_source: "backfilled"` in the config.
8. An explicit manifest must match `ctx.protocol`.
9. A misleading test comment.

Not done:
- A structured "held steps" share per cell (only `reference_ends_at` per trajectory and a warning).
- A labelled variant that falls back to a baseline instead of failing.

**Defaults** were fixed a priori. One 600-cutoff diagnostic ran on the P-known **calibration** split (scratch script, not a kept run). Its medians for underway and transiting were below the baselines, and for stationary they were above. Nothing was tuned on test.

**Commands run:**
```bash
cd backend && uv run pytest tests/unit -q                       # 270 passed, 1 xfailed
uv run pytest -m golden tests/golden/test_route_retrieval_real.py tests/golden/test_invariance_real.py   # 4 passed, 694 s
make lint && make gen-api
make benchmark PROTOCOL=P-known     # 767 s
make benchmark PROTOCOL=P-unseen    # 252 s
uv run --project backend python scripts/make_readme_figures.py   # RUN -> new P-known run
```

**Results:** runs `P-known_noaa_us_gulf_2023-06_20261021T152618Z` and `P-unseen_noaa_us_gulf_2023-06_20261021T153929Z` (digests and the full skill table are in `docs/benchmarks.md`).
- **Transiting:** retrieval beats the best baseline at every horizon under P-known (skill +0.62 at 1 h, +0.77 at 3 h, +0.31 at 24 h; all CIs exclude 0). Under P-unseen it wins at 1–12 h and ties at 24 h.
- **Pooled underway:**
  - P-known: wins to 12 h, ties at 24 h.
  - P-unseen: wins to 3 h, ties at 6–12 h, **loses at 24 h** (−0.38 [−0.63, −0.10]).
- **Stationary:** worse than persistence at every horizon.
- **Failures:** ≤ 0.1% (P-known), ≤ 0.6% (P-unseen); all are `no_references`.
- **Cost:** pool build about 60 s and about 9 GB peak; about 25 ms per forecast.
- Note: the runs record git `9477ff0`, but the working tree held this uncommitted change. The commit that adds `models/route_retrieval.py` contains exactly the code that ran.

**Blockers / open questions:** none blocking.
- An API `as_of` inside the train period rebuilds the pool per `as_of` (about 1 min).
- P-region retrieval is still refused.

**Next step:** the Milestone 3 failure review (§7) on these runs, by category:
- channel junctions (wrong branch);
- anchoring and departures (the stationary loss);
- references running out (held positions, long horizons);
- missing observations;
- new geography (`no_references`).

That needs a "held steps" share per cell. Then decide on a stationary/departure gate (e.g. persistence below a speed threshold, as a labelled variant). P-region is still to be built.

## 2026-10-21 — Milestone 3 slice 3: failure review of `route_retrieval_v1`

**Milestone:** 3 — historical-route retrieval
**Done:**
- `evaluation/failure_review.py` and `scripts/failure_review.py`. The script re-runs retrieval on a kept run's cutoffs and refuses to continue unless it reproduces the run's predictions exactly. It then flags every paired sample with fixed categories and compares it with the run's own best baseline for the cell (`best_baseline_id`). No losses are scored where the run has no skill reference (< 10 m).
  - Categories: `references_exhausted`, `branch_disagreement`, `wrong_branch` and its `wrong_branch_control`, `departure`, `false_departure`, `sparse_history`, `new_geography` and `timing_dominated`.
  - `wrong_branch` is an oracle over candidates, never an accuracy. Its control applies the same oracle to the same candidates rotated by a random angle.
  - Categories that select on truth are marked †.
  - Output goes to `data/runs/reviews/<run_id>/` with a `review.json` sidecar (thresholds, run digest, git SHA).
- `route_retrieval.py`: `WARN_RADIUS_FALLBACK` constant; `context_xy` is now public. Forecasts are unchanged (the reviews reproduce the runs).
- 4 unit tests (`test_failure_review.py`, hand-built synthetic records).
- `docs/failure-review.md` (findings and the ordered next experiments); `architecture.md`; `docs/README.md` index; README roadmap; `CLAUDE.md` status.

**Commands run:**
```bash
uv run --project backend python scripts/failure_review.py --protocol P-unseen   # 26,557 paired (cutoff, horizon) samples, 195 s
uv run --project backend python scripts/failure_review.py --protocol P-known    # 120,612 samples
make test && make lint                                                         # 275 passed
# after the leakage review's fixes, both reviews were re-run with the commands above
```
The first attempt at both reviews crashed on a `ProtocolRef` attribute access in the script. It was fixed and both were re-run.

**Results:** reviews of `P-known_noaa_us_gulf_2023-06_20261021T152618Z` and `P-unseen_noaa_us_gulf_2023-06_20261021T153929Z`. Details are in `docs/failure-review.md`.
- **Stationary loss is false departures:**
  - 11–15% of stationary cutoffs (P-known), every one a loss, with a median excess of +4.2 km at 1 h and +12.4 km at 24 h.
  - Real departures: retrieval ties persistence.
- **Wrong branch at junctions** (oracle diagnostic): 26–40% of pooled underway samples, against 14–19% for the rotated control. In transit: 21–40% against 7–11%. The real excess is 14–29 points: a bounded lever.
- **Timing:** about 40% of underway samples have an error over 1 km that is mostly along-track. Retrieval still beats the baseline median on them up to 12 h.
- **References exhausted:** ≤ 6% up to 12 h, about 61–65% at 24 h. Held samples in transit still beat damped velocity at 24 h.
- **By class:** fishing loses to constant velocity at 1 h (skill −1.45 [−2.53, −0.36]); tugs and "other" lose at 24 h. Tanker, cargo and passenger are strong.
- **Weather-driven turns:** not assessable (no archived weather).

**Leakage review (leakage-reviewer subagent):** no leakage, and nothing truth-selected is reported as accuracy. Fixed:
1. The review now uses the run's own best baseline and scores no losses where the run has none. It had been scoring stationary 1–6 h "losses" against a 0 m persistence.
2. All truth-conditioned categories are marked †, and the tautological 100% loss rate of false departures is no longer shown as a finding.
3. Added a rotated-candidate control for the `wrong_branch` oracle (§5 N1). The doc now claims only the excess over it.
4. Corrected doc and log claims: fishing's reference is CV; unique sample counts; P-unseen false departures are 15–19%; claims the review does not compute were removed.
5. `sparse_history`'s documented stale limit now matches the code (30 min).
6. Test-set reuse: `docs/failure-review.md` § "Test-set use" sets the `informed_by` labelling and records a pending human decision (a fresh test month vs. a dev window carved from calibration).

**Blockers / open questions:**
- **Human decision needed (AGENTS.md rule 12, data download):** a fresh untouched test period (e.g. July 2023, about 10 GB) for the claim of any retrieval v2 against v1, and a dev window carved from the calibration period for development. See `docs/failure-review.md`.
- Milestone 3 lists P-region benchmarks, but P-region eligibility (side-aware) and P-region benchmarks are not built. The runner and `eligible()` refuse them. This is the remaining Milestone 3 task.

**Next step:**
1. P-region retrieval eligibility and benchmark (needs context positions for the side test). Then `/milestone-check` for Milestone 3.
2. Retrieval v2 experiments on the **calibration split**, in the order given in `docs/failure-review.md`: stationary handling, speed-matched continuation, branch choice.

## 2026-10-21 — Fresh July test period and a dev window (user decision)

**Milestone:** 3 — historical-route retrieval
**Decision (user, 2026-10-21):** fetch July 2023 as a fresh, untouched test period, and carve a dev window out of the calibration period for retrieval v2 development. Reason: the failure review read the June test results, so June can no longer serve as held-out for anything that review motivates.

**Done:**
- Fetched July 2023: 31 days, filtered to the Gulf truth region. Manifest `data/manifests/noaa_us_gulf_2023-07-01_2023-07-31.json`.
- New combined dataset `noaa_us_gulf_2023-06-07` (June + July). Its June days are identical to `noaa_us_gulf_2023-06`: 87,377,629 rows, symmetric difference 0 excluding `source`.
- Split schema: a new `dev` split name.
  - `evaluation/splits.py`: `JUNE_JULY_2023_PERIODS`. Train [06-01, 06-17) is unchanged; dev is [06-18, 06-22) (the old calibration period); calibration is [06-23, 07-01) (the old June test, for Milestone 4 conformal radii); test is [07-02, 08-01), fresh. Every period is separated by a 24 h purge.
  - `P_UNSEEN_ASSIGNMENT_V2`: the same salt, train and test vessels; the old calibration bucket is halved into dev and calibration. Manifest version `splits-v2`.
  - The June-only manifests are unchanged (`splits-v1`).
- `run_benchmark.py --split {test,dev}`. Makefile `DS=` / `SPLIT=` for splits, fit, mask and benchmark.
- 2 new unit tests in `test_splits.py`: periods and purges, train unchanged, no June test, P-unseen train and test vessels identical to v1.
- **Rebuilt for the new dataset:** manifests, damped-velocity fits, navigable masks.
  - τ and masks are identical to the June-only ones (same train data).
  - Dev reference benchmarks for both protocols.
- **Docs and rules:**
  - Docs: `evaluation-protocol.md` (June+July section and rules), `failure-review.md` (decision recorded), `benchmarks.md`, `CLAUDE.md`.
  - `AGENTS.md` rule 13 now also excludes dev-split runs from the README.

**Commands run:**
```bash
uv run --project backend python scripts/fetch_noaa.py --start 2023-07-01 --end 2023-07-31   # ~31 min, 31 days ok
uv run --project backend python scripts/canonicalize.py --start 2023-06-01 --end 2023-07-31 --dataset-id noaa_us_gulf_2023-06-07
make splits DS=noaa_us_gulf_2023-06-07
make fit PROTOCOL=P-known DS=noaa_us_gulf_2023-06-07 && make mask PROTOCOL=P-known DS=noaa_us_gulf_2023-06-07   # and P-unseen
make benchmark PROTOCOL=P-known DS=noaa_us_gulf_2023-06-07 SPLIT=dev    # 250 s
make benchmark PROTOCOL=P-unseen DS=noaa_us_gulf_2023-06-07 SPLIT=dev   # 67 s
make test && make lint
```

**Results:**
- **Dev runs (final):** `P-known_noaa_us_gulf_2023-06-07_dev_20261021T184406Z` (7,372 cutoffs) and `P-unseen_noaa_us_gulf_2023-06-07_dev_20261021T184833Z` (366 cutoffs). Digests and the skill table are in `docs/benchmarks.md`.
  - They supersede the first dev runs (`…_20261021T182911Z`, `…_20261021T183325Z`), made before the review fixes below.
  - The same cutoffs and predictions; 30 P-known records lost truth to the dev truth cap.
- **P-known dev** reproduces the June-test pattern:
  - transiting: +0.61 / +0.74 / +0.70 at 1 / 3 / 6 h, a tie at 24 h;
  - stationary: losses from 6 h.
- **P-unseen dev** is too small to decide on (78 dev vessels in the cutoff region).
- **Split vessel counts in the cutoff region (P-known):** train 2,498; dev 1,622; calibration 2,066; test 3,282.

**Blockers / open questions:**
- P-unseen dev and calibration are small: 5% of vessels each, over 4 and 8 days. Conformal radii under P-unseen (Milestone 4) may need pooled strata.
- P-region retrieval eligibility and benchmark are still not built (the remaining Milestone 3 task).

**Next step:**
1. P-region retrieval eligibility and benchmark, then `/milestone-check` for Milestone 3.
2. Retrieval v2 on P-known dev, in the order given in `docs/failure-review.md`. July is benchmarked only when a v2 design is frozen, as one run of the baselines plus v1 plus v2.

**Leakage review of the split change (leakage-reviewer subagent):** no data path leaks into July (purge and containment, the frozen pool, train-only τ and masks all checked). The process could touch July without a record, so the following were added:
1. `evaluation/holdout.py` enforcement:
   - `--confirm-test` and a clean tree are required.
   - A committed access ledger (`data/manifests/test_access/`) is appended before scoring.
   - Re-scoring the same model version and config needs a reason.
   - The API refuses benchmarks and forecast evaluation that touch July (403 `holdout_test_period`).
   - `failure_review.py` refuses July runs without `--allow-holdout`, and picks the latest run by dataset and split.
2. Non-test runs carry the split in their run id and report header.
3. Benchmarks of dev and calibration cap truth at their period's end.
4. The test period ends Jul 31 00:00, where the data end. Manifests, fits and masks were regenerated: τ and masks are unchanged, and the dev runs were re-made.
5. `tune_navigable_mask.py` tunes on dev for splits-v2 manifests.
6. Docs:
   - P-unseen calibration is not the old June test.
   - P-unseen July vessels are the reviewed vessels (fresh in time only).
   - Milestone 4 conformal coverage is judged on July, per stratum and week; calibration and July are not exchangeable because pool staleness differs.
7. Tests: `test_holdout.py` (4, including P-region × dev) and an API holdout test.
   - The suggestion to use a new P-unseen salt was not taken. It would change train vessels and every train fit; the limitation is documented instead.

```bash
make splits DS=noaa_us_gulf_2023-06-07 && make fit/mask PROTOCOL={P-known,P-unseen} DS=noaa_us_gulf_2023-06-07
make benchmark PROTOCOL={P-known,P-unseen} DS=noaa_us_gulf_2023-06-07 SPLIT=dev
make test   # 282 passed
```

## 2026-10-21 — Milestone 3 slice 4 (P-region) and Milestone 3 check: complete

**Milestone:** 3 — historical-route retrieval

**P-region slice (commit `3fccd4d`):**
- **Cutoff selection:** P-region cutoffs are chosen by context side (`runner.select_cutoffs`, `region_window_ok`). Test windows keep all context in the Houston box. Train windows keep context and labels, through the truth bracket, beyond the 50 km buffer.
- **Retrieval eligibility:** only segments wholly beyond the buffer.
- **τ:** a P-region damped-velocity τ was fitted on the Gulf's train side.

**Leakage review:** no leak. Fixed:
1. The label window now covers the 30 min truth bracket.
2. The drop count and effective sampling region are recorded in the run.
3. The rolling-pool caveat and the June-only (not held-out) status are documented.

**Results** (`docs/benchmarks.md`):
- `P-region_noaa_us_gulf_2023-06_20261021T195800Z`: all models. Retrieval returns `no_references` on 19,843 of 19,843 cutoffs, so there are no paired cells: it does not transfer to new waters.
- `P-region_noaa_us_gulf_2023-06_20261021T200112Z`: baselines only, 504 cells. The baselines transfer.
- 2,796 sampled cutoffs were dropped by the side check.
- The first P-region runs (`…194547Z`, `…194901Z`) are superseded.

**Milestone 3 check:**

| Criterion | Evidence | Status |
|---|---|---|
| Protocol-aware segment index (§5 R1) | `data/segment_index.py`, `test_segment_index.py`, golden `test_segment_index_real.py` | met |
| Matching, alignment, continuation, top-K, central prediction, provenance | `models/route_retrieval.py`, `test_route_retrieval.py` | met |
| Benchmark vs baselines on identical samples under P-known, P-unseen, P-region | `P-known_…152618Z`, `P-unseen_…153929Z`, P-region runs above (retrieval has no forecasts there, which is recorded) | met |
| Failure review by category | `docs/failure-review.md`, `scripts/failure_review.py`. Weather-driven turns are documented as not assessable (no archived weather) | met |
| **Done when:** head-to-head with baselines by vessel class and horizon | Class strata in both runs; `failure-review.md` §5 | met |
| **Done when:** provenance shows the references behind each forecast | `CandidateTrajectory.reference_segment_ids`, `central_trajectory_id`; tested | met |
| **Done when:** leakage tests prove no post-cutoff reference was eligible | Unit full-vs-truncated equivalence; frozen forecast unchanged by other vessels' pre-cutoff test-period data; synthetic and real invariance; golden "every reference known by train end" for P-known and P-unseen | met |

**Commands run:**
```bash
make fit PROTOCOL=P-region                                    # train-side tau
make benchmark PROTOCOL=P-region                              # 190 s
uv run --project backend python scripts/run_benchmark.py --protocol P-region --models persistence_v1 constant_velocity_v1 constant_turn_rate_v1 damped_velocity_v1   # 108 s
make test && make lint                                        # 283 passed; clean
cd backend && uv run pytest -m golden tests/golden/test_route_retrieval_real.py tests/golden/test_invariance_real.py tests/golden/test_segment_index_real.py   # 8 passed, 596 s
```

**Outcome:** Milestone 3 is complete.

**Next step:**
- **Before Milestone 4:** the retrieval v2 experiments agreed with the user are developed on the P-known dev window, in the order in `docs/failure-review.md` (stationary handling first). The design is then frozen and July is scored once, with baselines, v1 and v2 together (`--confirm-test`, recorded in the ledger).
- **Milestone 4, smallest first slice:** split-conformal displacement radii per model × horizon × stratum, fitted on the `noaa_us_gulf_2023-06-07` calibration split (Jun 23–30). Empirical coverage is judged on the July records of that one run, per stratum and per July week.

## 2026-10-21 — Retrieval v2 developed on the dev window and frozen

**Milestone:** between 3 (complete) and 4. These are the retrieval improvements agreed after the failure review.
**Done:**
- `route_retrieval_v2` (2.0.0), commits `b472ecb` and `672a766`. It is v1 plus a stationary hold.
  - A target stationary at the cutoff (the sampler's rule) keeps its last position: persistence, labelled as such.
  - Underway targets are unchanged.
- **Gate design** (dev only; scratch diagnostic on the 4,1xx P-known dev stationary cutoffs):
  - Releasing the hold once 50 / 60 / 75 / 90% of the reference weight had moved more than 1 km was worse than always holding, in median and mean, at 1–24 h. For example, at 24 h the median was 1.47 / 0.76 / 0.39 / 0.31 km against 0.22 km.
  - So v2 always holds.
- **Speed-matched continuation** (dev underway cutoffs) was mixed:
  - transiting median 1.83 → 1.47 km at 1 h and 3.92 → 3.49 km at 2 h;
  - pooled 8.25 → 8.76 km at 12 h;
  - better than v1 on fewer than half the samples.
  - Kept as an off-by-default option, not in v2.
- **Branch choice** (failure-review item 3) was not attempted; v2 is frozen without it.
- **Leakage review (leakage-reviewer subagent):** no leak, and no July use (no ledger; every June+July run is dev). Fixed:
  1. The hold is honestly labelled persistence (`stationary_policy`, warning, description).
  2. The hold is unconditional when `depart_share >= 1`, with no 0/0 path; relative weights are used otherwise.
  3. `failure_review` tolerates forecasts without a central reference.
  4. A test now exercises the partial release.
  5. Pre-freeze runs are noted as not v2.0.0.

**Commands run:**
```bash
uv run --project backend python <scratch>/gate_sweep.py    # dev, stationary cutoffs
uv run --project backend python <scratch>/speed_sweep.py   # dev, underway cutoffs
uv run --project backend python scripts/run_benchmark.py --protocol P-known --dataset-id noaa_us_gulf_2023-06-07 --split dev --models persistence_v1 constant_velocity_v1 damped_velocity_v1 route_retrieval_v1 route_retrieval_v2 --no-geojson   # 447 s
make test   # 285 passed
```

**Results:** `P-known_noaa_us_gulf_2023-06-07_dev_20261021T204207Z` (clean `672a766`).
- **Stationary:** v2 = persistence on all 27,354 records, so its skill is 0.00. v1's was −15.3 at 24 h.
- **Underway and transiting:** v2 = v1. Transiting skill is +0.61 / +0.70 / −0.08 at 1 / 6 / 24 h.
- Details are in `docs/benchmarks.md`.

**Blockers / open questions:** the July hold-out run needs the user's go-ahead. It consumes the fresh test set once for baselines, v1 and v2 (`--confirm-test`, recorded in the ledger).

**Next step:**
1. On approval, run the July test for P-known and P-unseen with baselines, v1 and v2, then update the README from those runs (AGENTS.md rule 13).
2. Then Milestone 4: conformal radii fitted on the calibration split and judged on those July records.

## 2026-10-21 — July hold-out scored once (user approval); README updated from it

**Milestone:** between 3 (complete) and 4.
**Done:**
- July test split of `noaa_us_gulf_2023-06-07`, P-known and P-unseen, scored once with every product model, from clean `2757bd3` with `--confirm-test`. Both runs are recorded in the ledger before scoring.
- The first P-unseen attempt was refused by the clean-tree guard (an uncommitted edit to the figure script). The edit was stashed, P-unseen was re-run from the clean tree, and nothing was scored or logged for the refused attempt.
- `scripts/make_readme_figures.py` points at the July P-known run and plots v2. The map example is chosen by a fixed rule (the transiting tanker nearest Bolivar Roads), not by its errors. Titles and footers were fixed.
- README results now rest on the July hold-out (AGENTS.md rule 13), and the losses are stated. `benchmarks.md`, `failure-review.md`.

**Commands run:**
```bash
uv run --project backend python scripts/run_benchmark.py --protocol P-known  --dataset-id noaa_us_gulf_2023-06-07 --split test --confirm-test --informed-by <June runs, reviews, dev run>   # 5,152 s
uv run --project backend python scripts/run_benchmark.py --protocol P-unseen --dataset-id noaa_us_gulf_2023-06-07 --split test --confirm-test --informed-by <same>                             # 1,002 s
uv run --project backend python scripts/make_readme_figures.py
```

**Results** (`docs/benchmarks.md`):
- **`P-known_noaa_us_gulf_2023-06-07_20261021T213109Z`** (106,503 cutoffs):
  - Transiting skill of v2 is +0.60 / +0.76 / +0.65 / +0.44 / +0.30 at 1 / 3 / 6 / 12 / 24 h, with every CI above 0.
  - Pooled underway wins to 6 h, ties at 12 h, and loses at 24 h (−0.31).
  - Stationary: v2 = persistence.
- **`P-unseen_noaa_us_gulf_2023-06-07_20261021T225730Z`** (23,166 cutoffs):
  - Transiting +0.57 / +0.70 / +0.60 / +0.44 / +0.36, every CI above 0.
  - Pooled wins to 3 h, ties at 6 h, and loses at 12 h (−0.17) and 24 h (−0.44).
- **June to July:** transiting held up. Pooled underway at 24 h went from a June tie to a July loss under P-known.

**Blockers / open questions:** none.

**Next step:** Milestone 4, smallest first slice:
- split-conformal displacement radii for persistence, damped velocity and v2, per horizon × stratum, fitted on the calibration split (Jun 23–30);
- empirical coverage measured on the records of the two July runs above (no new July scoring), per stratum and per July week;
- the API returns radii with their nominal level and calibration-set id.

## 2026-10-21 — Milestone 4 slice 1: split-conformal radii, coverage on July

**Milestone:** 4 — conformal uncertainty
**Done:**
- `evaluation/conformal.py`, `scripts/conformal.py`, and `run_benchmark --split calibration`.
- Calibration-split benchmark runs for both protocols, followed by the radii and the July coverage analysis. Commits `950fa39`, `822563c`, `9baab70` and `7ce72c3`.
- `provenance.git_sha` now marks untracked code and parameters as dirty. The previous check ignored them, so the first conformal outputs, made from untracked code, claimed clean `950fa39`. Those outputs are moved to `_superseded_*` and the analysis was re-run from clean `822563c`, with identical numbers.
  - The July benchmark runs are unaffected: the tree was fully clean at `2757bd3` when they started.
- `holdout.record_analysis`: each coverage analysis of a held-out run is appended to the ledger.

**Leakage review (leakage-reviewer subagent):** no leakage. Radii are fitted only on calibration errors, and the coverage reading is legitimate. Fixed:
1. Dirty-aware SHA (above). The conformal script now requires a clean tree.
2. Calibration sizes (n, vessels) are reported, with a P-unseen header saying its radii come from a 5% vessel bucket.
3. Model failures and records with no radius are reported beside coverage.
4. The headline count uses non-overlapping cells. "CI∋level" is added as a descriptive column.
5. Analyses are never overwritten and are recorded in the ledger.
6. Model versions are compared as well as configs.
7. More tests.
8. The back-filled class caveat is stated.

Not adopted, because they would be chosen after seeing July coverage:
- a per-stratum minimum-vessels rule;
- a vessel condition on the verdict.

Both are reported descriptively instead.

**Commands run:**
```bash
uv run --project backend python scripts/run_benchmark.py --protocol P-known  --dataset-id noaa_us_gulf_2023-06-07 --split calibration --no-geojson   # 1,468 s
uv run --project backend python scripts/run_benchmark.py --protocol P-unseen --dataset-id noaa_us_gulf_2023-06-07 --split calibration --no-geojson   # 215 s
uv run --project backend python scripts/conformal.py --protocol P-known && uv run --project backend python scripts/conformal.py --protocol P-unseen
make test   # 291 passed
```

**Results** (`docs/benchmarks.md`):
- **P-known:** 69 of 72 motion-level cells within ±3% at 90%. The misses are v1 on stationary vessels at 1–3 h.
  - v2 transiting coverage is 0.884–0.896, with smaller areas than damped velocity up to 12 h.
  - v2 underway coverage drifts from 0.90 to 0.87 over July as the frozen pool ages.
- **P-unseen:** 50 of 72. Underway under-covers (0.79–0.89) because the calibration bucket is 5% of vessels.

**Pre-registration for slice 2 (H3 Mondrian binning), recorded before it is built or any H3 result is seen:**
- **Bins:** motion state × H3 cell (resolution 5) of the cutoff report's position.
- **Own radius:** a bin gets one with at least 30 calibration errors. Otherwise it uses the slice-1 chain (motion:class, then motion:ALL, then ALL).
- **Same as slice 1:** levels (80/90/95%) and verdict (±0.03 on n ≥ 200).
- **Adopted only if** July coverage at 90% stays within tolerance on at least as many motion-level cells as slice 1, and the mean area at 90% is smaller for v2 on underway traffic.
- **No retuning:** no other resolution or threshold is tried on July.

**Next step:**
1. API returns radii with nominal level and calibration-set id. A radius is returned only at target times that coincide with a fitted horizon; other times get none.
2. Slice 2 (H3), then `/milestone-check` for Milestone 4.

## 2026-10-21 — Milestone 4: radii served by POST /forecasts

**Milestone:** 4 — conformal uncertainty
**Done** (commits `0c16654`, `9edfe42`, `48a4891`):
- `ConformalRegion` changes:
  - nullable radii, so no radius at unfitted target times and none interpolated;
  - per-radius stratum, calibration n and vessels;
  - fitted horizons and the back-filled class source;
  - wording now states a marginal, exchangeability-based claim.
- `ForecastRequest.conformal_level`. Generated TS types updated.
- `evaluation/conformal`: `motion_state` (the sampler's rule), `calibration_like` (the population the radii were calibrated on), `last_vessel_class`, and `region_for` with its refusals. The API attaches radii in `_with_radii`.
- `scripts/conformal.py --params-only` refits the radii file without reading test records. The radii now carry per-model config hashes and `label_reach_end`. They were regenerated from clean commits, and the radii are numerically identical to the ones analysed.

**Leakage review (leakage-reviewer subagent):** no leakage. Fixed:
1. Stale `as_of`: radii are refused if `as_of` is more than 10 min after the last report.
2. Non-calibration-like vessels are refused: outside the cutoff region, not Class A/B, too few reports, history under 3 h.
3. The model-config hash is checked.
4. Honest wording, with sample sizes in the response.
5. The 30 min label-reach edge after the calibration end.
6. The cache key covers the radii file and manifest.
7. A warning when no radii are fitted.
8. A clean refusal (not a 500) when there is no calibration period.
9. Class is taken from the last report, as the benchmark does.

Not done: gating served radii on July per-cell coverage verdicts. Coverage is documented in `docs/benchmarks.md` instead.

**Commands run:**
```bash
make test && make lint && make gen-api && make web-test   # 293 passed; clean; 10 passed
uv run --project backend python scripts/conformal.py --protocol P-known --params-only   # and P-unseen
```

**Results:** a real July forecast (v2, a transiting tanker) carries 12.7 / 26.8 / 43.5 / 91.1 km radii at 1 / 2 / 3 / 6 h. Behind them are 980 / 960 / 953 / 903 calibration errors from about 280 vessels. Unfitted hours are null.

**Next step:** slice 2 (H3 Mondrian bins, pre-registered above), then `/milestone-check` for Milestone 4.

## 2026-10-21 — H3 slice: clarifications recorded before any H3 result

Two points the pre-registration above left open, fixed before the H3 code exists or any H3 coverage is computed:
1. **Both protocols:** the adoption rule is applied to P-known and P-unseen separately. H3 binning is adopted only if it passes in both.
2. **"Mean area smaller for v2 on underway traffic"** means: at 90%, v2's mean disc area on `underway:ALL` is smaller under H3 than under slice 1 at **4 or more of the 6 horizons**.

Everything else is as pre-registered:
- H3 resolution 5, on the cutoff report's position;
- bins of motion × cell, needing at least 30 calibration errors, otherwise the slice-1 chain;
- levels 80/90/95%, verdict ±0.03 on n ≥ 200;
- coverage criterion: at least as many motion-level cells within tolerance at 90% as slice 1 (P-known 69/72, P-unseen 50/72).

## 2026-10-22 — Milestone 4 slice 2: H3 Mondrian bins, pre-registered rule applied → not adopted

**Milestone:** 4 — conformal uncertainty
**Done** (commits `bf6239b`, then a fix, `86a9cad`, `8d70849`, `7cd89fa`):
- `conformal.add_h3` and an optional motion × H3 stratum ahead of the slice-1 chain.
- `scripts/conformal.py --bins h3`, which computes the pre-registered adoption rule against the latest slice-1 analysis of the same test run.
- Dependency `h3` 4.5 (Apache-2.0). Unit test for bins and fallback.
- **Crash, recorded:** the first H3 run per protocol wrote `coverage.parquet` and `report.md`, then crashed in the adoption step: it globbed its own half-written directory. That was before its ledger entry.
  - The two outputs were moved to `_crashed_*` without being read, and recorded in the ledger.
  - The glob was fixed to complete analyses only, and the analysis re-run.
  - The earlier commit `a2677b2` / `9348b60` messages say "radii and coverage analysis ledger entry", but those commits contain only the H3 radii files (the crash came first). The chain did not stop because `| tail` hid the exit code; `pipefail` is used from now on.

**Results** (`docs/benchmarks.md`):
- **P-known:** 69/72 → 61/72 cells within tolerance (fails), although the areas are smaller at 6/6 horizons.
- **P-unseen:** 50/72 → 55/72 (passes), with smaller areas at 6/6 horizons.
- **Not adopted**, since the rule requires both protocols. The P-known cells that broke are retrieval's, already at the tolerance edge from pool staleness.
- No other resolution or threshold was tried, as pre-registered.

**Leakage review (leakage-reviewer subagent):** no leakage and no test-informed choice. The design matches the pre-registration, the rule was computed as registered, and "not adopted" is correct. The crashed outputs turned out byte-identical to the re-run. Fixed:
1. `scripts/conformal.py` writes a `*_started` ledger entry before reading test records, so a crash can no longer leave a look unrecorded.
2. Ledger corrections:
   - the P-unseen crashed entry's SHA is `a2677b2…`, not `bf6239b`;
   - both crashed entries get full SHAs and their real access times, with a note;
   - the re-run entries get a `rerun_reason`.
3. The README area claim is corrected to 6–13% (P-known) and 4–8% (P-unseen).
4. A record without an H3 cell now falls through to the chain instead of pooling into a fake bin (no record was affected).
5. The ledger config hash includes the bins setting.

**Commands run:**
```bash
uv add 'h3>=4,<5'
uv run --project backend python scripts/conformal.py --protocol P-known --bins h3   # and P-unseen (after the crash fix)
```

**Next step:** `/milestone-check` for Milestone 4.

## 2026-10-22 — Milestone 4 check: complete

**Milestone:** 4 — conformal uncertainty

| Criterion (build plan §7) | Evidence | Status |
|---|---|---|
| Split-conformal radii per model × horizon × stratum on a protocol-compliant calibration window | `evaluation/conformal.py`. Radii in `data/model_params/conformal/*.json`, fitted on the calibration-split runs (Jun 23–30; disjoint from the train/index period and from the July test). Tests in `test_conformal.py` | met |
| Report empirical coverage and area on test | July coverage analyses (`docs/benchmarks.md`, "Conformal displacement radii"): coverage with vessel CIs, mean disc area, per stratum and per July week, at 80/90/95%. Each analysis is recorded in the test-access ledger | met |
| API returns radii with nominal level and calibration-set id | `POST /forecasts` → `ModelForecast.uncertainty` (`ConformalRegion`: radii at fitted horizons, nominal level, calibration run id, strata, calibration n and vessels). `test_api.py`; real-data smoke check | met |
| **Done when:** empirical coverage within a stated tolerance of nominal per admitted horizon, **or deviations reported per stratum** | Tolerance ±0.03 (n ≥ 200), stated before any coverage was seen. P-known: 69/72 motion-level cells within tolerance at 90%; the 3 deviations (v1 stationary 1–3 h) are reported. P-unseen: 50/72, and its under-coverage of underway traffic (5% vessel calibration bucket) is reported per stratum. Per-class strata are in each `coverage.parquet` | met (deviations reported) |

Beyond the plan, the H3 Mondrian variant was pre-registered, evaluated and **not adopted** (P-known coverage fails).

**Commands run:** `make test` (294 passed), `make lint` (clean); evidence as above.

**Outcome:** Milestone 4 is complete.

**Known limitations carried forward:**
- Retrieval's coverage drifts down through July as its frozen pool ages. A rolling refresh of the pool would need its own protocol work.
- P-unseen radii are under-powered.
- Radii exist only at the fitted horizons.

**Next step:** Milestone 5, the workbench UI (§9). Smallest first slice: the forecast comparison screen. It shows the models' paths for a chosen vessel and cutoff, the conformal discs at the fitted horizons, the retrieval candidates (labelled similarity), and the withheld-truth overlay only after the forecast exists.

## 2026-10-22 — Milestone 5 slice 1: forecast comparison screen (Screen B)

**Milestone:** 5 — workbench UI
**Done** (uncommitted):
- **Cutoff mode in the explorer.** A checkbox plus a slider over the loaded track's report times sets `as_of`, so a cutoff is always a real report (§6.1). In this mode the map draws only reports at or before `as_of` (`clipTrack`), and every report after it is removed from the drawn layers.
- **`ForecastPanel`:**
  - choice of horizon (1–24 h), conformal level (none/80/90/95%) and models; calibration-only models are hidden;
  - `POST /forecasts`; dashed, model-coloured paths from the anchor report, with a step dot every 10 min;
  - retrieval candidates as faint lines labelled "similarity, not probability";
  - a target-time slider. The conformal disc is drawn only where a radius was fitted, with its level, calibration n, vessels and stratum. Elsewhere the panel says so;
  - per-model warnings, model toggles, and a reproducibility line (forecast id, data version, model versions).
- **Withheld truth:**
  - fetched from `POST /forecasts/evaluation` only after a forecast exists for the identical request; any change to the request clears both;
  - drawn solid green with hollow points, never bridged across a missing target time; dotted error lines at the selected time;
  - an error table (whole hours × models, along/cross on hover, `n/e` with reasons, paired ADE, a truth-availability row);
  - a July cutoff gets the API's `holdout_test_period` refusal, shown as "Truth withheld".
- **Experimental horizons:** `ForecastResponse.input_coverage` gains `motion_state` (the sampler's rule) and `vessel_class` (last report, back-filled). The panel looks up §6.4 admission for `motion:class` and labels each horizon first-class, experimental or not audited. Additive API change; types regenerated.
- **MMSI search:** typing a full MMSI opens it even when it is not on the current vessel page.
- Pure layer functions in `lib/forecast.ts`, with Vitest tests on synthetic data (clipping, anchor, discs only at fitted times, toggles, truth never bridged, admission lookup).

**Commands run:**
```bash
make gen-api; make lint; make test            # 294 passed
cd frontend && npm run typecheck && npm test && npm run build   # 18 passed; build ok
```

**Checked in the browser (real data, `make dev`):**
- Tanker 636010378, `as_of` 2023-06-20T11:59:24Z, 6 h horizon, P-known parameters. Truth was shown. Errors at 1/2/3/4 h:
  - CV 0.66/1.84/3.30/4.91 km;
  - retrieval v2 1.29/2.44/3.39/4.70 km;
  - damped 5.1–51 km;
  - persistence 22–87 km.

  This is one diagnostic sample on the dev window, not a result. No radii: `as_of` is before the calibration labels' reach.
- Cargo ship 636092697, `as_of` 2023-07-10T12:00:10Z. 90% discs at 6 h of 87–160 km (n=334 from 113 vessels, `underway:cargo`). Truth was refused (July hold-out).

**Known gaps:**
- Admission exists only for `noaa_us_gulf_2023-06`. On the June+July dataset every horizon reads "not audited".
- The explorer fetches the whole day's track to the browser and clips it client-side. Post-cutoff reports are not drawn, but they are in the browser's memory. That is the same data `GET /track` already serves.

**Next step:** slice 2, the benchmark dashboard (Screen C): model × horizon table with CIs, along/cross, error versus lead time, strata, evaluable/`not_evaluable` counts, reproducibility metadata. Then the failure gallery: open a benchmark sample on the map by run id. Then the Playwright smoke test.

## 2026-10-22 — Milestone 5 slice 2: benchmark dashboard (Screen C)

**Milestone:** 5 — workbench UI
**Done** (slice 1 committed as `c125662`; slice 2 uncommitted):
- **`GET /benchmarks`:** every run directory, newest first. It returns header fields from `run.json` (protocol, dataset, sampler split, operational, models, horizons, cutoffs, git SHA) plus running/incomplete directories. Checksums are verified only by `GET /benchmarks/{run_id}`, which the dashboard reads for the full record. Unit test added; types regenerated.
- **`/benchmarks` page:** run list with split badges. The run and stratum are in the URL (`?run=&stratum=`), so a view can be reproduced from its run id. Every number comes from the immutable record:
  - **Paired model × horizon table:** median km with the 95% vessel-bootstrap CI. A second line gives skill against the best baseline, with its paired CI, coloured by whether the CI excludes 0. The lowest median is bold. The best-baseline column is marked. Column headers show paired samples / vessels. Non-headline cells get an asterisk. Hovering a cell shows p90 and failures. Dropped cells are listed.
  - **Error versus lead time:** log–log, CI bands, crosshair tooltip.
  - **Along/cross-track medians**, with n decomposed.
  - **Run-level `not_evaluable` reasons** by horizon.
  - **Reproducibility block:** git, manifest version and hash, sampler seed / n_boot / history / step / region / bracket, `informed_by`, model versions with their configs, and the `make benchmark` command.
  - Dev and calibration runs carry a "not a held-out result" note.
- **Model colours** are now fixed per model, so a model keeps its colour whatever else is selected. They come from the dataviz reference categorical order with blue and green left out, because those mean the observed track and truth on the map. Validated: lightness, chroma, adjacent CVD ΔE ≥ 9.1 and normal-vision ΔE ≥ 19.6 all pass. The contrast warning for the lighter hues is covered by legends, direct table labels and the table views.
- Explorer/Benchmarks nav. Pure shaping in `lib/benchmark.ts`, with Vitest tests on a synthetic run.

**Commands run:**
```bash
make gen-api; make lint; make test           # 295 passed
cd frontend && npm run typecheck && npm test && npm run build   # 24 passed
node <dataviz>/scripts/validate_palette.js "#eb6834,#1baf7a,#eda100,#e87ba4,#6250d6,#e34948" --mode light
```

**Checked in the browser** against `docs/benchmarks.md` for the July P-known hold-out run `…_20261021T213109Z`. The dashboard reproduces the ledger:
- transiting v2 1.71 / 5.97 / 13.4 / 20.2 / 25.4 km at 1/3/6/12/24 h;
- transiting skill +60% [57, 63] at 1 h;
- underway skill +53% [50, 56] at 1 h;
- v1 = v2 on underway cells.

`GET /benchmarks` takes about 0.1 s for 27 runs. The full record takes about 0.26 s (0.8 MB, checksums verified).

**Known gaps:**
- Which runs are kept or superseded is recorded only in `docs/benchmarks.md`. The list shows every run and points there.
- Feasibility rates are not on the dashboard yet.
- No per-sample view yet.

**Next step:** slice 3, the failure gallery. List the worst paired samples of a run (per stratum and horizon) from `records.parquet` and open one on the map at its cutoff, with the same forecast and truth overlay. The July test samples need a design decision: the evaluation endpoint refuses July truth, but those truths are already scored in the immutable run. Then the Playwright smoke test and `/milestone-check`.

## 2026-10-22 — Milestone 5 slice 3: failure gallery and sample replay

**Milestone:** 5 — workbench UI
**Done** (slice 2 committed as `94a5e1a`; slice 3 uncommitted):
- **`GET /benchmarks/{run_id}/samples`:** one paired cell (stratum × horizon) for one model, read from the run's verified `records.parquet` joined to `cutoffs.parquet`.
  - Samples where the model **failed** on an evaluable sample are listed first, with status and error code (`n_failed`). The rest are ranked by the model's error, or by its loss to the cell's best baseline on the same sample.
  - `n_paired` equals the cell's `n_paired` (asserted in the unit test; checked on 17 real runs).
  - Runs from before per-sample positions were stored (the 8 earliest B0 runs) get 409 `records_without_positions`.
- **Hold-out guard:** 403 `holdout_test_period` when a run is on a hold-out test split, **or** when its cutoffs plus the longest horizon and the truth bracket reach into the test period of its protocol's current hold-out manifest (`touches_test`, as in forecast evaluation).
  - Every look at test records belongs in the test-access ledger, which only the CLI writes; the API never writes it. Aggregates stay on the dashboard.
  - On today's runs it refuses exactly the two July runs. Calibration and dev runs are served.
- **Gallery on the dashboard:** model, horizon and ranking selectors. Each card has a thumbnail (anchor, prediction, baseline, truth; display only), the errors, along/cross, land flags, and "Open on map".
- **Replay in the explorer:**
  - `/?dataset=&vessel=&as_of=&models=&horizon=&run=&cutoff=&expect=` loads the track from the day before to the day after, sets the cutoff to the exact report, and runs the forecast.
  - It then checks the forecast against the stored prediction ("Reproduces the run record", or the Δ). The link carries the predicted position only, never truth or the stored error.
  - A banner links back to the run.

**Finding: retrieval's path depends on the requested horizon.**
- The runner predicts once at its longest horizon (24 h) and scores shorter horizons on that path (`runner.predict_all`). Retrieval's medoid is chosen over the whole requested horizon, so a 6 h request can give a different path for its first 6 h.
- With a 1 h replay, 13 of 24 sampled retrieval forecasts differed from the record. Replaying at 24 h, all 64 checks reproduced exactly: dev run `…_dev_20261021T204207Z` (v2, clean `672a766`) and June run `…152618Z` (v1), underway and transiting, at 1 and 6 h. Constant velocity reproduces at any horizon.
- **Benchmarks are honest** (pairing holds; no truth involved). They describe "retrieval asked for 24 h, read at h".
- **The served conformal radii are mislabeled for shorter retrieval requests.** `POST /forecasts` with `horizon_hours` < 24 attaches radii calibrated on 24 h-request paths to a different forecaster. Not fixed here: it needs a choice (see next step).

**Leakage review (leakage-reviewer subagent):** no temporal or retrieval leakage. Fixed:
1. Model failures were dropped from the gallery (they are now listed first).
2. The hold-out guard keyed on the version string only (it now also checks times; there is an unmonkeypatched time-based test).
3. The stored error was shown before evaluation (removed from the link).
4. Exact float horizon match (now a tolerance).
5. An empty vessel class was handled differently from the runner (`nullif`).

Also noted: `GET /vessels/{id}/track` serves raw July positions without restriction. The ledger covers model records, not raw tracks. Whether that is intended should be written down in `docs/evaluation-protocol.md`.

**Commands run:**
```bash
make gen-api; make lint; make test           # 296 passed
cd frontend && npm run typecheck && npm test && npm run build   # 26 passed
```

**Checked in the browser:** gallery on `…152618Z`, transiting, 6 h. The worst case, passenger ship 368225760, replays on the map, matches the record, and after truth is revealed shows 246.7 km (same as stored). Failed samples (`no_references`) show first in `underway:ALL`.

**Next step:**
1. Decide how to fix the radius mislabelling for retrieval:
   - (a) `POST /forecasts` predicts at the calibration run's longest horizon and slices; or
   - (b) the radii carry their request horizon and are refused otherwise; or
   - (c) make retrieval's medoid horizon-independent (a model change, so a new version and new benchmarks).
2. Then the Playwright smoke test and `/milestone-check`.
