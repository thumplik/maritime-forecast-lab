# tests/fixtures — SYNTHETIC data only

Everything here is generated, not observed. Unit tests only, never benchmarks (AGENTS.md rule 4).

- `synthetic.py` generates `SYNTHETIC_tracks.csv` in NOAA MarineCadastre CSV layout. It has six vessels with MMSIs `999000001`–`999000006` (MID 999 is not allocated to any country): moving, turning, stopping (decelerate then moored with jitter), gaps (45 min and 3 h), antimeridian crossing, and dirty rows (duplicate, out of order, sentinels, bad latitude, position spike).
- Regenerate: `cd backend && uv run python tests/fixtures/synthetic.py`. A test checks the committed CSV matches the generator.
