# Architecture

See build plan §3 for the target architecture. This file tracks what actually exists.

## Data flow (implemented so far)

```text
NOAA daily zip (national, ~340 MB)
   └─ scripts/fetch_noaa.py ── stream download, SHA-256, unzip, DuckDB bbox filter (truth region)
        └─ data/raw/noaa_us_gulf/date=YYYY-MM-DD/part-0.parquet      (+ data/manifests/*.json)
             └─ scripts/canonicalize.py ── §4 schema, sentinels → null, dedup, quality flags
                  └─ data/processed/canonical/<dataset_id>/date=.../part-0.parquet
                       └─ scripts/coverage_audit.py, scripts/calibrate.py ── data/runs/<run_id>/
```

## Module ownership

| Module | Owns |
|---|---|
| `maritime_forecast.config` | Repo paths, region loading from `config/regions.yaml` |
| `maritime_forecast.data.canonical` | NOAA → canonical mapping and validation |
| `maritime_forecast.data.segment` | Gap- and motion-aware track segmentation, resampling |
| `maritime_forecast.geo.geodesic` | WGS84 direct/inverse problems, along/cross-track |
| `maritime_forecast.models.baselines` | Persistence, constant velocity (standard + paper-compatible) |
| `maritime_forecast.evaluation.windows` | Paper-style fixed windows for §6.6 calibration |

Not yet built (by design): API, frontend, run store, registry, CI.
