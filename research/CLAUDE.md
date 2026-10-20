# research/

- `notebooks/`: spike analysis in jupytext percent format (`# %%` cells in `.py`). Open as notebooks with `uv run --project backend jupytext --to ipynb <file>` if needed; commit only the `.py`.
- Notebooks import from `maritime_forecast` rather than redefining logic. Anything a notebook proves useful moves into the package with tests.
- `adapters/`: pinned, isolated third-party research code (TrAISformer, Timestral wrappers). Each adapter has a README with upstream URL, commit hash, license, and every local modification. Never edit upstream files in place.
