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
| `maritime_forecast.models.baselines` | Persistence, constant velocity (standard + paper-compatible) |
| `maritime_forecast.evaluation.windows` | Paper-style fixed windows for §6.6 calibration |
| `maritime_forecast.evaluation.truth` | Bracketed ground truth that never bridges gaps |
| `maritime_forecast.evaluation.admission` | Per-stratum horizon admission (plan v0.3 §6.4) |
| `maritime_forecast.evaluation.splits` | Protocol rules: P-known, P-unseen, P-region, P-paper |
| `maritime_forecast.data.cutoffs` | Cutoff sampler, transiting stratum |
| `maritime_forecast.data.dejitter` | Causal stationary-jitter smoothing for model windows (running mean within stationary runs) |
| `maritime_forecast.schemas` | Pydantic contracts: `Observation`, `SplitManifest` (+`RegionSplit`), `DatasetManifest`/`IngestReport`, `ForecastRequest`/`ForecastResponse`/`ForecastEvaluation`, `BenchmarkRun` |

| `maritime_forecast.api` | FastAPI read API: app factory, typed errors, response models |

## Read API (Milestone 1)

`make api` serves on http://localhost:8000 (OpenAPI docs at `/docs`).

| Endpoint | Purpose | Limits |
|---|---|---|
| `GET /health` | Status, version, git SHA, dataset count, installed models (none yet) | — |
| `GET /datasets` | Datasets from the store, joined with their manifests (mapping, provenance, rows, ingest report). Includes per-stratum horizon admission from the newest coverage audit of that dataset | — |
| `GET /vessels?dataset_id&from&to&bbox&limit&offset` | Per-vessel summaries, ordered by id, with `total` for paging | Window ≤ 31 days (default: whole dataset). `limit` ≤ 1000. bbox is `lon_min,lat_min,lon_max,lat_max` |
| `GET /vessels/{id}/track?dataset_id&from&to` | Columnar points plus `segments` and `gaps`. A gap of more than 30 min starts a new segment, and clients must never draw across gaps | Window ≤ 31 days. At most 200,000 points (413 above that) |

Conventions:

- Every error has the shape `{"error": {"code", "message", "details"}}`. Codes: `invalid_parameter`, `window_too_large` (422), `dataset_not_found`, `vessel_not_found` (404), `too_many_points` (413). Validation errors return the location and message, never the submitted input.
- Query times must carry a UTC offset; any offset is converted to UTC. Windows are half-open `[from, to)`.
- Ids are validated by pattern, so dataset ids cannot traverse paths. Every value is a bound parameter.
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

`schemas/forecast.py` and `schemas/benchmark.py` fix the request and response shapes before any endpoint exists. They are published in the OpenAPI components, so they are already TypeScript types. The type system enforces these rules:

- A forecast response has no truth fields. Truth lives in `ForecastEvaluation`.
- Candidate scores are `similarity`.
- Uncertainty can only be a `conformal_radius` with a nominal level and calibration-set id.
- A non-ok model result has an error and no points.
- `BenchmarkRun` rejects unpaired cells and truth-selected (non-operational) records.

## Antimeridian

Fixed in Milestone 1, found with the synthetic fixture:

- Longitude interpolation (`geo.geodesic.lon_lerp_sql`, used by truth and by the P-paper grid) and the spike-filter distance take the short way round.
- The web map unwraps longitudes along a track.

Not yet built: run store, model registry (Milestone 2).
