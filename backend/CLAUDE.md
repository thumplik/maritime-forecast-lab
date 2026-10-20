# backend/ — the `maritime_forecast` package

- `uv sync` here creates `.venv`; run anything with `uv run` (from repo root: `uv run --project backend ...`).
- Package modules follow §3 of the build plan. Do not add a module before something needs it.
- `config/__init__.py` owns repo paths and region loading — import paths from there, never build them ad hoc.
- Tests: `tests/unit/` (pure, fast, no network/data), `tests/golden/` (`@pytest.mark.golden`, needs local NOAA data), `tests/fixtures/` (small synthetic data, clearly labeled synthetic).
- Every model must pass the leakage invariance test once it exists (Milestone 2): perturbing post-cutoff observations leaves the forecast unchanged.
- Geometry tests must cover: all bearing quadrants, antimeridian, high latitude, zero speed, missing COG, knot↔m/s.
