# Maritime Forecast Lab

Research workbench for forecasting where an AIS-tracked vessel will be 1 h – 3 days after a historical cutoff, with a leakage-aware evaluator at its core. Spec: @docs/build-plan.md (read the relevant § before working on a milestone). Agent rules: @AGENTS.md

## Where we are

**Current milestone: 1 — repository, contracts and data pipeline** (Milestone 0 done; build plan v0.3). No frontend, API or CI yet.
Status, blockers and next step live in [docs/implementation-log.md](docs/implementation-log.md) — read it first, append to it last.

## Commands

All Python runs through `uv` against the `backend/` project (Python 3.11+). If `uv` is not on PATH: `export PATH=$HOME/Library/Python/3.9/bin:$PATH`.

```bash
make setup        # uv sync the backend env
make test         # unit tests (no network, no local data)
make lint         # ruff check + format check
make fetch        # download June 2023 NOAA AIS, filtered to the Gulf truth region (~10 GB transfer, resumable)
make canonical    # raw NOAA parquet -> canonical §4 schema in data/processed/
make audit        # Milestone 0 coverage audit -> data/runs/coverage_audit/
make calibrate    # §6.6 golden-number check against the calibration paper
make splits       # materialize P-known / P-unseen / P-paper manifests
make import MAPPING=<preset|yaml> DATASET=<id> FILES="..."   # any provider via config/sources/
make fetch-dma DAYS="2023-06-01"   # Danish AIS; range-reads one day from monthly zips
```

Run one script directly: `uv run --project backend python scripts/<name>.py --help`.
Ad-hoc data questions: `uv run --project backend python -c "import duckdb; print(duckdb.sql(\"SELECT ... FROM 'data/processed/...'\"))"`.

## Layout

- `backend/src/maritime_forecast/` — the package. `config/` (paths, regions), `data/` (ingest, canonicalize, segment, cutoffs), `geo/` (geodesic math), `models/` (forecasters), `evaluation/` (truth, admission, splits, metrics), `schemas/` (Pydantic contracts).
- `scripts/` — thin CLIs over the package (fetch, canonicalize, audit, calibrate).
- `research/notebooks/` — spike notebooks (jupytext `.py` percent format, so they diff cleanly).
- `config/regions.yaml` — cutoff and truth regions. Never hardcode coordinates.
- `config/sources/*.yaml` — provider column mappings. Read data through `storage.ParquetStore`; build model context only with `ParquetStore.history()` (causal).
- `data/` — gitignored except manifests. See `data/README.md`.
- `docs/` — specs and findings. Each doc has a single purpose; see `docs/README.md`.

## Conventions

- Polars/DuckDB for data; pandas only if an upstream model demands it.
- Timestamps are timezone-aware UTC end to end. Distances in metres internally; report km and nmi.
- Geodesics via `geographiclib`/`pyproj` on WGS84 — never flat-earth approximations except in the documented local ENU frame.
- Course/heading in degrees clockwise from true north, wrapped to [0, 360). NOAA sentinels: heading 511, COG 360, SOG 102.3.
- Every benchmark or audit output writes a JSON sidecar with dataset id, filter, sampler config + seed, code git SHA.
- Keep functions pure and testable; scripts only do argument parsing and I/O.

## Gotchas

- NOAA files are ~340 MB zipped / ~1.5 GB CSV per day nationally. `fetch_noaa.py` streams, filters, and deletes them.
- NOAA's one-minute down-sampling means the native report floor is ~60 s.
- Houston–Galveston is dominated by tugs/towing and moored vessels. Uniform cutoff sampling is wrong — see §6.3 and `docs/cutoff-sampling.md`.
- The calibration paper's numbers are vessel-disjoint, 10-min grid, 18 context / 18 target steps. Match all of it before comparing.

## Skills and agents in this repo

- `/log-progress` — append a dated entry to the implementation log.
- `/calibrate` — run and interpret the §6.6 golden-number check.
- `/milestone-check` — verify a milestone's "done when" criteria before moving on.
- `/new-forecaster` — checklist for adding a model behind the forecaster contract.
- `leakage-reviewer` subagent — review a diff for temporal/protocol leakage. Use it before committing evaluator, sampler, retrieval or model changes.
