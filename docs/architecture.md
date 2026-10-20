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
| `maritime_forecast.data.segment` | Gap- and motion-aware track segmentation, resampling |
| `maritime_forecast.geo.geodesic` | WGS84 direct/inverse problems, along/cross-track |
| `maritime_forecast.models.baselines` | Persistence, constant velocity (standard + paper-compatible) |
| `maritime_forecast.evaluation.windows` | Paper-style fixed windows for §6.6 calibration |
| `maritime_forecast.evaluation.truth` | Bracketed ground truth that never bridges gaps |
| `maritime_forecast.evaluation.admission` | Per-stratum horizon admission (plan v0.3 §6.4) |
| `maritime_forecast.evaluation.splits` | Protocol rules: P-known, P-unseen, P-paper |
| `maritime_forecast.data.cutoffs` | Cutoff sampler, transiting stratum |
| `maritime_forecast.schemas` | Pydantic contracts: `Observation`, `SplitManifest`, `DatasetManifest`/`IngestReport` |

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

Not yet built: frontend, run store, model registry, CI (Milestone 1 continues).
