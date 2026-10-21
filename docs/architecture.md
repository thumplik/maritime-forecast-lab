# Architecture

See build plan §3 for the target architecture. This file tracks what actually exists.

## Data flow (implemented so far)

```text
NOAA daily zip (national, ~340 MB)
   └─ scripts/fetch_noaa.py ── stream download, SHA-256, unzip, DuckDB bbox filter (truth region)
        └─ data/raw/noaa_us_gulf/date=YYYY-MM-DD/part-0.parquet      (+ data/manifests/*.json)
             └─ scripts/canonicalize.py ── data.ingest with the noaa_marinecadastre mapping
                  └─ data/processed/canonical/<dataset_id>/date=.../part-0.parquet   (+ DatasetManifest)

Other providers: CSV/Parquet ── scripts/import_ais.py --mapping <config/sources/*.yaml> ── same store
                       └─ scripts/coverage_audit.py, scripts/calibrate.py ── data/runs/<run_id>/
```

## Module ownership

| Module | Owns |
|---|---|
| `maritime_forecast.config` | Repo paths, region loading from `config/regions.yaml` |
| `maritime_forecast.data.mapping` | Source mappings (`config/sources/*.yaml`) |
| `maritime_forecast.data.canonical` | Mapping-driven canonical SQL: parsing, sentinels, drop reasons, dedup, flags |
| `maritime_forecast.data.ingest` | Files → store, with row-accounting report and dataset manifest |
| `maritime_forecast.storage.parquet_store` | Daily-partitioned store: date pruning, scan, track, causal `history`, vessel summaries, atomic per-day writes |
| `maritime_forecast.data.segment` | Gap-aware segmentation, spike removal, trailing SOG features, `motion_state` label, display segments/gaps |
| `maritime_forecast.geo.geodesic` | WGS84 direct/inverse problems, along/cross-track |
| `maritime_forecast.models.baselines` | Pure array functions: persistence, constant velocity, last-two-point velocity |
| `maritime_forecast.models.base` | Forecaster contract: `AISHistory` (rejects rows after as_of), `ForecastHorizon`, `ModelCapabilities`, `BaseForecaster` (typed failures) |
| `maritime_forecast.models.{persistence,constant_velocity,damped_velocity,constant_turn}` | B0 adapters: `persistence_v1`, `constant_velocity_v1`, `constant_velocity_paper_v1` (calibration only), `damped_velocity_v1`, `constant_turn_rate_v1` |
| `maritime_forecast.models.fit_damped` | Train-split τ fit for damped velocity |
| `maritime_forecast.models.registry` | model_id -> factory(ModelContext), capabilities, `product_model_ids()` |
| `maritime_forecast.evaluation.invariance` | Leakage-invariance harness |
| `maritime_forecast.evaluation.windows` | Paper-style fixed windows for §6.6 calibration |
| `maritime_forecast.evaluation.truth` | Bracketed ground truth that never bridges gaps |
| `maritime_forecast.evaluation.admission` | Per-stratum horizon admission (plan v0.3 §6.4) |
| `maritime_forecast.evaluation.splits` | Protocol rules: P-known, P-unseen, P-region, P-paper |
| `maritime_forecast.data.cutoffs` | Cutoff sampler, transiting stratum |
| `maritime_forecast.data.dejitter` | Causal stationary-jitter smoothing for model windows (running mean within stationary runs) |
| `maritime_forecast.schemas` | Pydantic contracts: `Observation`, `SplitManifest` (+`RegionSplit`), `DatasetManifest`/`IngestReport`, `ForecastRequest`/`ForecastResponse`/`ForecastEvaluation`, `BenchmarkRun` |

| `maritime_forecast.api` | FastAPI API: app factory, typed errors, response models, `jobs` (background benchmark processes) |
| `maritime_forecast.data.segment_index` | Retrieval index of completed segments with regular tracks; per-cutoff, per-protocol eligibility (Milestone 3) |
| `maritime_forecast.geo.navigable_mask` | Navigable-water raster: OSM land minus train-split traffic cells; point and segment tests |
| `maritime_forecast.evaluation.feasibility` | Per-step feasibility flags (land, crossing, speed, acceleration) |
| `maritime_forecast.evaluation.export` | GeoJSON of a run (predictions, truth points, anchors) outside the immutable run directory |
| `maritime_forecast.evaluation.forecast_eval` | Scores one served forecast against withheld truth (`ForecastEvaluation`) |

## API (Milestones 1–2)

`make api` serves on http://localhost:8000 (OpenAPI docs at `/docs`).

| Endpoint | Purpose | Limits |
|---|---|---|
| `GET /health` | Status, version, git SHA, dataset count, installed models (none yet) | — |
| `GET /datasets` | Datasets from the store, joined with their manifests (mapping, provenance, rows, ingest report). Includes per-stratum horizon admission from the newest coverage audit of that dataset | — |
| `GET /vessels?dataset_id&from&to&bbox&limit&offset` | Per-vessel summaries, ordered by id, with `total` for paging | Window ≤ 31 days (default: whole dataset). `limit` ≤ 1000. bbox is `lon_min,lat_min,lon_max,lat_max` |
| `GET /models?dataset_id` | Registered models with capabilities, `calibration_only`, fit status and parameters in use | — |
| `POST /forecasts` | `ForecastRequest` → `ForecastResponse`: causal history via `history_from_store`, every requested model, typed per-model failures, input coverage. Never contains truth | `ForecastRequest` limits (≤ 72 h, ≤ 1024 steps, ≤ 10 models) |
| `POST /forecasts/evaluation` | Same `ForecastRequest` → `ForecastEvaluation` for the forecast `POST /forecasts` serves (same `forecast_id`, same cache). Truth is read only after the forecast exists, with the benchmark's rule (`runner.truth_on_grid`: truth region, 30 min bracket, scan window horizon + 1 h, never bridged). Every target time is reported for every model: a truth reason where there is no truth, `model_failed` where a model gave nothing. `paired` marks the target times every model scored; `ade_m` averages only those | As `POST /forecasts` |
| `POST /benchmarks` | `BenchmarkRequest` (P-known / P-unseen, models, horizons, seed, n_boot) → 202 + `BenchmarkStatus`, `Location: /benchmarks/{run_id}`. The runner's own `benchmark_context` + `preflight` run first, so anything the CLI refuses (unknown or unfitted model, in-sample fit split, short horizons) is a 422 `benchmark_refused` before a process starts. The run itself is `run_benchmark` in a spawned process writing to the immutable run store | One running benchmark per API (409 `benchmark_busy`); ≤ 12 horizons, `n_boot` 100–5000 |
| `GET /benchmarks/{run_id}` | `running` (this API's process alive), `incomplete` (a directory without `run.json`: a CLI run in progress, or interrupted), or `succeeded`/`failed` with the full `BenchmarkRun`, checksums verified on every read (409 `run_tampered`) | — |
| `GET /benchmarks/{run_id}/summary?stratum&headline_only` | One row per (stratum, horizon) cell with every model side by side on that cell's paired set: median error + CI, p90, failure rate, skill vs the best baseline + paired CI, admission and headline flags | 409 `run_not_finished` until finalized |
| `GET /vessels/{id}/track?dataset_id&from&to` | Columnar points plus `segments` and `gaps`. A gap of more than 30 min starts a new segment, and clients must never draw across gaps | Window ≤ 31 days. At most 200,000 points (413 above that) |

Conventions:

- Every error has the shape `{"error": {"code", "message", "details"}}`. Codes: `invalid_parameter`, `window_too_large`, `unknown_model`, `benchmark_refused` (422), `dataset_not_found`, `vessel_not_found`, `manifest_not_found`, `run_not_found` (404), `benchmark_busy`, `run_not_finished`, `run_tampered` (409), `too_many_points` (413). Validation errors return the location and message, never the submitted input.
- Query times must carry a UTC offset; any offset is converted to UTC. Windows are half-open `[from, to)`.
- Ids are validated by pattern, so dataset ids cannot traverse paths. Every value is a bound parameter.
- Forecasts use the **P-known train-split fit** (`ApiSettings.deployment_protocol`) for fitted models, recorded in each model's `config`.
- `forecast_id` is a hash of the request, model configs and versions, and the **data version** (dataset manifest identity plus each partition file's name, size and mtime). It keys a bounded in-process cache. A rewrite that keeps row counts still invalidates it, and a test checks this.
- Each request gets its own DuckDB cursor. Cursors do not inherit session settings, so `ParquetStore` sets `TimeZone='UTC'` on every connection it is given.

Measured on the June NOAA data:

| Request | Time |
|---|---|
| Month-wide Houston vessel list | 2.0 s |
| Whole-Gulf page | 0.3 s |
| 32 k-point month track | 0.21 s (2.2 MB) |
| DMA 8 k-point day track | 0.016 s |

## Web explorer (Milestone 1)

`frontend/` is Next.js 16 with a single client page and MapLibre GL 6.

- The browser calls the read API directly (`NEXT_PUBLIC_API_URL`, CORS allows localhost:3000). Nothing is fetched at build time.
- API types are generated from the backend's OpenAPI schema by `make gen-api` (`lib/api/openapi.json` → `lib/api/schema.d.ts`). CI fails if either drifts.
- `lib/track.ts` turns a track into separate layers: observed segment lines, gap connectors, gap midpoint labels and report points. A gap of more than 30 min is never part of an observed line. It is drawn amber and dashed with a duration label that is always visible (a gap must always look like a gap).
- Basemap: OpenFreeMap positron (OSM, no key).
- Tailwind (listed in the build plan stack) is deferred to Milestone 5. One small stylesheet is enough for this slice.

## CI

`.github/workflows/ci.yml` runs on push to main and on PRs.

- **Backend job:** `uv sync --locked`, ruff, pytest (golden/data tests excluded), and an OpenAPI drift check.
- **Frontend job:** `npm ci`, a TS-types drift check, typecheck, vitest and `next build`.

No data or model weights are used.

## Contracts for Milestone 2

`schemas/forecast.py` and `schemas/benchmark.py` fix the request and response shapes; every one is now served by an endpoint, so FastAPI's OpenAPI schema (and the generated TypeScript) covers them. The type system enforces these rules:

- A forecast response has no truth fields. Truth lives in `ForecastEvaluation`.
- Candidate scores are `similarity`.
- Uncertainty can only be a `conformal_radius` with a nominal level and calibration-set id.
- A non-ok model result has an error and no points.
- `BenchmarkRun` rejects unpaired cells and truth-selected (non-operational) records.
- `ForecastEvaluation` has one score per target time per model, evaluable only where truth exists, and `paired` must equal "every model evaluable".

## Antimeridian

Fixed in Milestone 1, found with the synthetic fixture:

- Longitude interpolation (`geo.geodesic.lon_lerp_sql`, used by truth and by the P-paper grid) and the spike-filter distance take the short way round.
- The web map unwraps longitudes along a track.

Milestone 2 is complete. Milestone 3 (retrieval) has started with the segment index.
