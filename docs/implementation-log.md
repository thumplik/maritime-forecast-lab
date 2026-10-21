# Implementation log

**Current milestone: 2 — Baselines and the evaluator** (in progress: slices 1–3 done). Milestone 1 completed 2026-10-20. Milestone 0 was completed and reviewed on 2026-10-19.

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
