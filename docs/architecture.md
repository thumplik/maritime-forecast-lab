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

Not yet built: API, frontend, run store, model registry, CI (Milestone 1 continues).
