# Maritime Forecast Lab

**Where will this ship be in 1 hour, 6 hours, or 3 days?** A research workbench for forecasting vessel trajectories from AIS position reports. Its core is an evaluator built so that no forecast can see the future.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/forecast-example-dark.png">
  <img alt="Map of Galveston Bay and the Houston Ship Channel. A tanker's real track (dotted) follows the winding channel north-west into the river. The route-retrieval forecast follows the channel's bends close to the real track. A constant-velocity forecast runs straight across land, a damped-velocity forecast stops short in the bay, and persistence stays at the cutoff." src="docs/img/forecast-example-light.png" width="720">
</picture>

*One real cutoff from the June 2023 test set (picked for the baseline README, before retrieval existed). Every model saw only the reports up to the black square. The dotted line is what the ship actually did, withheld until scoring. Straight-line extrapolation crosses land within hours. Route retrieval follows earlier ships' tracks up the channel, but more slowly: at 6 h it is about where the ship really was at 3 h.*

---

## What this is

Pick a vessel and a moment in the past. The workbench then:

1. **replays** only the AIS data that existed at that moment (the *cutoff*);
2. **runs one or more forecasters**: kinematic baselines and retrieval of historical routes today; foundation and learned models next;
3. **reveals the real track** and scores every model on exactly the same samples, with confidence intervals, coverage gaps and feasibility (did the forecast put the ship on land?).

The same machinery runs thousands of cutoffs unattended, as a benchmark, and serves single forecasts through an API to a map-based explorer.

It is **not** a port-destination classifier and **not** (yet) a live tracker. The question is geographic: *position at a horizon*.

## Why evaluation comes first

Published AIS trajectory-prediction results are often inflated. The [calibration paper](https://arxiv.org/abs/2609.25827) behind this project shows three causes: oracle best-of-N decoding, vessels shared between train and test, and geographic overlap. Here the evaluator prevents all three by default:

| Guard | How it is enforced |
|---|---|
| **No hindsight** | A model only receives reports at or before the cutoff. The invariance test deletes, shifts, spikes and floods every report after the cutoff; every registered model must give an identical forecast. It runs on real NOAA cutoffs |
| **Named protocols** | **P-known** is a chronological split; a vessel's own earlier voyages are allowed. **P-unseen** adds vessel-disjoint assignment by MMSI hash. Fitted parameters (τ, masks, retrieval index) come only from the protocol's train split |
| **Paired scoring** | Every model in a comparison is scored on the identical set of (cutoff, horizon) samples. A model that fails on a sample is recorded as failed, never silently dropped |
| **Honest uncertainty** | Vessel-bootstrap 95% CIs. No best-of-N in any operational number. Similarity scores are labelled as similarity, not probability |
| **Real ground truth only** | Truth is interpolated only between reports ≤ 30 min apart and never bridged across a coverage gap. Horizons with too little truth are marked *experimental* per stratum |
| **Calibrated evaluator** | Constant velocity on the paper's protocol must reproduce the published numbers within 10% before any model comparison is trusted |
| **Immutable runs** | Every benchmark is write-once and checksummed, and records data provenance, protocol, sampler seed, model versions and git SHA |

## How it works

```mermaid
flowchart LR
    A[NOAA daily AIS<br/>stream-filtered to the Gulf] --> B[Canonical observations<br/>Parquet + DuckDB]
    B --> C[Gap-aware segmentation<br/>motion state]
    C --> D[Cutoff sampler<br/>underway / stationary / transiting]
    D --> E[Causal history<br/>at or before as_of]
    E --> F[Forecasters<br/>persistence · CV · CTR · damped · retrieval ...]
    C --> G[Withheld truth<br/>never bridged across gaps]
    F --> H[Paired evaluator<br/>CIs · strata · feasibility]
    G --> H
    H --> I[Immutable run<br/>JSON · GeoJSON · report]
    H --> J[FastAPI] --> K[Next.js + MapLibre explorer]
```

- **Cutoff region:** Houston–Galveston.
- **Truth region:** the whole U.S. Gulf coast, so a ship that leaves the port box is still scored.
- **Cutoffs:** sampled by motion state with per-vessel caps. Port AIS is dominated by moored ships and tugs, so uniform sampling would measure the wrong thing.

## Results so far

All numbers come from immutable runs on the June 2023 NOAA test period (Jun 23–30); run ids and checksums are in [docs/benchmarks.md](docs/benchmarks.md). Since then, the June test results have been read for a [failure review](docs/failure-review.md), so they are no longer an untouched hold-out for later models. July 2023 has been fetched as a fresh test period, and the next model comparison will be reported on it.
- **Skill** is the paired reduction in median error against the best of persistence, constant velocity and damped velocity in that cell, with a 95% vessel-bootstrap CI.
- **Transiting** means cargo, tanker and passenger ships that have been underway for the last 30 minutes.
- The evaluator itself is calibrated: paper-mode constant velocity gives 4.03 / 9.65 / 16.16 km at 1 / 2 / 3 h, against the published 3.69 / 9.03 / 15.33 km (+9% / +7% / +5%, within the 10% tolerance).

### Historical-route retrieval beats every baseline for ships under way

`route_retrieval_v1` finds the training-period voyages that passed closest to the ship's current position on a similar course, speed and recent track. It lines them up at the ship and follows the closest match forward. It has no learned parameters, and its references are only voyages completed before the train split ended (Jun 17).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/error-by-horizon-dark.png">
  <img alt="Median error by horizon for ships in transit, log scales. Route retrieval is lowest at every horizon: 1.6 km at 1 h, 5.9 km at 3 h and 26 km at 24 h. Constant velocity is 4.2 km at 1 h and grows to 410 km at 24 h. Damped velocity levels off at about 37 km and persistence at about 53 km." src="docs/img/error-by-horizon-light.png" width="720">
</picture>

Ships in transit, median error in km (best baseline in brackets):

| Horizon | P-known retrieval | Best baseline | Skill [95% CI] | P-unseen retrieval | Best baseline | Skill [95% CI] |
|---|---|---|---|---|---|---|
| 1 h | **1.6** | 4.2 (CV) | +62% [57, 66] | **1.6** | 5.1 (CV) | +69% [62, 75] |
| 3 h | **5.9** | 25.2 (CV) | +77% [73, 79] | **7.2** | 24.6 (CV) | +71% [60, 80] |
| 6 h | **12.8** | 37.3 (damped) | +66% [59, 70] | **16.6** | 33.5 (damped) | +50% [37, 66] |
| 12 h | **20.8** | 35.8 (damped) | +42% [33, 48] | **23.0** | 32.7 (damped) | +30% [16, 44] |
| 24 h | **25.6** | 37.1 (damped) | +31% [20, 43] | 33.3 | 32.3 (damped) | −3% [−53, +31], tie |

Where it does **not** win:
- **All underway traffic (mostly tugs and harbour craft):**
  - P-known: it wins up to 12 h (+64% at 1 h, +17% at 12 h) and ties at 24 h.
  - P-unseen: it wins up to 3 h (+54% at 1 h), ties at 6–12 h and **loses at 24 h** (−38% [−63, −10]).
  - Without the vessel's own earlier voyages, long-horizon harbour moves are poorly matched by other ships' tracks.
- **Moored and anchored vessels:** persistence is better at every horizon. At 24 h retrieval's median is 2.9 km against 0.2 km. A nearby reference ship that later left drags the forecast away. Departure timing is a separate problem.
- **New waters:** under P-region (train elsewhere in the Gulf, test in Houston), retrieval has no historical route nearby and fails on every cutoff. The kinematic baselines, which need no geography, still work there.
- **References run out:**
  - Most historical segments end within hours, when the reference ship leaves receiver coverage.
  - After that, the forecast holds the reference's last position.
  - This is why the advantage shrinks with horizon.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/on-land-dark.png">
  <img alt="Share of underway forecasts that put the ship on land. Constant velocity: 61% at 1 h rising to 88% at 24 h. Damped velocity: about 26–29%. Route retrieval: 9% at 1 h rising to 29% at 24 h. Persistence: about 0%. The mask's own error, measured on the true positions, rises from 1.4% to 8%." src="docs/img/on-land-light.png" width="720">
</picture>

- **Kinematics leave the channel.** In port, 61% of constant-velocity forecasts are on land within an hour.
- **Retrieval mostly stays in water:** 9% of its paths have a point on land by 1 h, 29% by 24 h.
  - Its *path* crossing rate is higher (28% at 1 h): straight 10-minute chords between points cut the bends of narrow channels.
- **The mask:** the navigable-water mask is OSM land minus water cells that training-period traffic actually used, because OpenStreetMap's land polygons treat the ship channel itself as land.
  - Its own error is measured on the true positions and reported in every cell (dashed line).

The success condition for every model is the same: **beat the best of persistence, constant velocity and damped velocity, per stratum, on paired held-out samples under P-known and P-unseen.** Retrieval meets it for ships in transit (except 24 h under P-unseen, a tie) and for short horizons in all underway traffic. It does not meet it for stationary vessels.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 0 | Data spike & coverage audit: what horizons can the data support? | ✓ |
| 1 | Contracts, data pipeline, read API, track explorer, CI | ✓ |
| 2 | Baselines & the evaluator: paired benchmark, calibration, invariance, feasibility, benchmark API | ✓ |
| 3 | Historical-route retrieval: kNN over train-period segments, aligned and continued along real routes ([failure review](docs/failure-review.md)) | ✓ (v2 in development on a dev window; fresh July test pending) |
| 4 | **Conformal uncertainty:** calibrated displacement radii per stratum | ← next |
| 5 | Workbench UI: forecast comparison, truth overlay, benchmark dashboard | |
| 6 | Timestral zero-shot adapter (position, velocity, velocity + retrieved route) | |
| 7 | Research models: TrAISformer via the calibration paper's reimplementation | |
| 8 | Multi-day credibility with longer-range AIS; optional live ingest | |

Full specification: [docs/build-plan.md](docs/build-plan.md). Progress, decisions and every command run: [docs/implementation-log.md](docs/implementation-log.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12) and Node 24 for the web app.

```bash
make setup                        # Python env + frontend node_modules
make test                         # unit tests (no network, no data)
make dev                          # API on :8000 (docs at /docs) + web app on http://localhost:3000
```

With data (about 10 GB downloaded, filtered on the fly to about 1 GB of Gulf data):

```bash
make fetch && make canonical      # NOAA June 2023 -> canonical observations
make splits                       # P-known / P-unseen / P-paper manifests
make calibrate                    # evaluator vs published numbers + real-data golden tests
make fit PROTOCOL=P-known         # damped-velocity tau on the train split
make fetch-land && make mask PROTOCOL=P-known   # navigable-water mask for feasibility
make benchmark PROTOCOL=P-known   # unattended paired benchmark -> immutable run + GeoJSON
make index                        # prebuilt retrieval segment index (the model builds its own pool)
```

The API serves datasets, vessels and tracks, `POST /forecasts`, `POST /forecasts/evaluation` (truth only after the forecast exists), and `POST /benchmarks` with status and summary endpoints. See [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | Contents |
|---|---|
| `backend/src/maritime_forecast/` | The package: `data/` (ingest, segmentation, cutoffs, segment index), `models/`, `evaluation/` (runner, truth, bootstrap, feasibility, invariance), `geo/`, `api/`, `schemas/` |
| `scripts/` | Thin CLIs: fetch, canonicalize, audit, calibrate, fit, benchmark, masks, index, figures |
| `frontend/` | Next.js + MapLibre explorer (tracks with gaps drawn as gaps) |
| `config/` | Regions and provider column mappings (no hardcoded coordinates) |
| `docs/` | Spec, coverage audit, evaluation protocol, benchmarks, implementation log |
| `data/` | Gitignored except the committed manifests (the provenance record) |

## Data and licensing

- **AIS:** [NOAA MarineCadastre AccessAIS](https://coast.noaa.gov/digitalcoast/tools/ais.html), U.S. Coast Guard terrestrial AIS, one-minute filtered. Public domain. Static fields such as vessel type are back-filled by NOAA and treated as non-as-of. Other providers import through column mappings; one day of Danish Maritime Authority AIS is verified.
- **Land polygons and basemap:** © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL), via [osmdata.openstreetmap.de](https://osmdata.openstreetmap.de) and [OpenFreeMap](https://openfreemap.org).
- **Code:** MIT. Data and third-party models carry their own licenses; see [data/README.md](data/README.md).

The README figures are produced by `scripts/make_readme_figures.py` from the runs cited above. No vessel identities are shown.

## Working on this repo with coding agents

[`AGENTS.md`](AGENTS.md) holds the binding rules: milestones in order, leakage invariants, never best-of-N, paired scoring only. `CLAUDE.md` files describe each directory. Project skills live in `.claude/skills/`, and a leakage-review subagent in `.claude/agents/` reviews every evaluator, sampler, retrieval or model change before it is committed.
