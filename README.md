# Maritime Forecast Lab

A research workbench for **forecasting where a ship will be** hours to days after a historical cutoff, from AIS position reports — built around an evaluator that refuses to leak the future.

Pick a vessel and a past moment; the system shows only what was known at that time, runs one or more forecasters, then reveals the real track and scores every model on the same samples.

> **Status:** Milestone 0 — data spike and coverage audit. No app yet. See [docs/implementation-log.md](docs/implementation-log.md).

## Why another AIS forecaster?

Published AIS trajectory-prediction results are often inflated by best-of-N decoding, vessels shared between train and test, and geographic overlap. This project puts the evaluation first:

- forecasts see only data available at the cutoff (tested by perturbing the future);
- models are compared on identical evaluable samples, with bootstrap CIs by vessel;
- missing ground truth and coverage gaps are reported, never papered over;
- uncertainty is calibrated (conformal radii) or not claimed;
- the evaluator is calibrated against published constant-velocity numbers before any model comparison is trusted.

## Data

NOAA MarineCadastre AccessAIS (U.S. Coast Guard terrestrial AIS, public domain). Forecast cutoffs in Houston–Galveston; ground truth anywhere along the U.S. Gulf coast. Calibration period June 2023. Details: [data/README.md](data/README.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.11+ itself).

```bash
make setup      # create the Python env
make test       # unit tests
make fetch      # ~10 GB download, filtered on the fly to ~1 GB of Gulf data
make canonical  # canonical observations
make audit      # coverage audit
make calibrate  # evaluator vs. published numbers
make api        # read API on http://localhost:8000/docs
```

## Roadmap

| # | Milestone |
|---|---|
| 0 | Data spike & coverage audit ← *now* |
| 1 | Repository, contracts, data pipeline |
| 2 | Baselines & evaluator |
| 3 | Historical-route retrieval |
| 4 | Conformal uncertainty |
| 5 | Workbench UI (Next.js + MapLibre) |
| 6 | Timestral zero-shot adapter |
| 7 | Research models (TrAISformer) |
| 8 | Multi-day credibility, optional live ingest |

Full specification: [docs/build-plan.md](docs/build-plan.md).

## Working on this repo with coding agents

`CLAUDE.md` (Claude Code) and `AGENTS.md` (all agents) hold the rules; nested `CLAUDE.md` files describe each directory. Project skills live in `.claude/skills/` and a leakage-review subagent in `.claude/agents/`.

## License

Code: MIT. Data and third-party models carry their own licenses — see [data/README.md](data/README.md) and `research/adapters/*/README.md`.
