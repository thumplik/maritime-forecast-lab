# scripts/ — thin command-line entry points

- Scripts parse arguments, do I/O, and call into `maritime_forecast`. Logic that is reused or needs tests belongs in the package.
- Run as `uv run --project backend python scripts/<name>.py`. Each script has a module docstring with usage; keep it current.
- Every script that produces results writes a JSON sidecar (inputs, config, seed, git SHA) next to its output.
- Long-running scripts must be resumable and print progress per unit of work.
