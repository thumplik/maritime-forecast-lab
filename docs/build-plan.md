# Maritime Forecast Lab — Codex Build Plan

**Version:** 0.4  
**Date:** 2026-10-22  
**Purpose:** Build a reproducible, extensible system for forecasting a vessel's geographic trajectory over the next hours to days from AIS history.

## 0. What changed

### v0.4 (2026-10-22, from Milestones 3–6 measured on real data; `docs/benchmarks.md`)

1. **The product output becomes a calibrated set of H3 cells per horizon** ("where will the ship be, within which hexes"), replacing a point plus a conformal disc as the headline. Futures branch at junctions, and the 90% discs at 6 h are 87–160 km across, covering land and both branches at once. A cell set can be multimodal and follow the water. New **Milestone 7** (§5 H, §6.7).
2. **Milestone 6 (Timestral) is closed as a documented negative result** (dev window only; never scored on July).
   - Zero-shot Timestral on positions (T1-pos) or velocities (T1-vel) never beat retrieval, and on transiting ships it never beat the best kinematic baseline.
   - Longer context helps only short port moves.
   - T1-vel+route was not built: it would mostly re-derive what retrieval already provides.
3. **Route knowledge is where the skill is.** Retrieval v2 beats every kinematic baseline by 50–75% on transiting ships up to 12 h; everything without route knowledge loses from about 6 h. The set models are therefore built on historical traffic: retrieval candidates first (H1), then an H3 transition model (H2).
4. **TrAISformer moves to Milestone 8** and is reframed as a candidate *distribution* model (its output is already a classification over position bins), scored by the §6.7 set metrics. Multi-day credibility becomes Milestone 9.

### v0.3 (2026-10-19, from the Milestone 0 findings in `docs/coverage-audit.md`)

1. **Horizon admission is decided per stratum** (vessel class × motion state), not pooled. Tugs, which stay inside coverage, make up about 60% of underway cutoffs and were enough on their own to pass the pooled rule at every horizon (§6.4).
2. **Damped-velocity baseline** added to B0. Persistence beats constant velocity from 2 h onward on the pooled Houston underway set, so a baseline whose speed decays over time is the honest bar (§5 B0, Milestone 2).
3. **Transiting stratum** is reported as a headline stratum alongside the pooled numbers: cargo, tanker and passenger-ship cutoffs that have been underway for the last 30 minutes. This is where route knowledge should matter (§6.3).

### v0.2 (changes from v0.1)


v0.1's evaluation guardrails are kept intact. The changes are about sequencing, scope, and closing gaps the review found.

1. **Milestone 0 is now a data spike, not an app scaffold.** Measure ground-truth coverage, motion-state mix and baseline error on real NOAA data before building UI. The measured coverage decides which horizons are first-class.
2. **Evaluator calibration against published numbers.** The leakage-aware evaluation paper (arXiv 2609.25827) ran on NOAA Houston–Galveston data, June 2023, and reports constant-velocity errors on that corpus. Reproducing them is a required regression test.
3. **Explicit cutoff sampling policy.** Port AIS is dominated by stationary vessels and tugs. Cutoffs are now sampled by motion state with per-vessel caps, and results are stratified.
4. **Truth region larger than cutoff region.** A transiting ship leaves a Houston-sized box within hours. Ground truth is drawn from the whole U.S. Gulf coast so long-horizon labels aren't limited to slow or stationary vessels.
5. **Model order changed.** Historical-route retrieval comes before Timestral. Published evidence points to route and lane priors as the main source of skill; Timestral is a general-purpose forecaster released 2026-10-15 with no geographic knowledge.
6. **Timestral representations specified.** Velocity-integrated and retrieval-covariate variants are added alongside ENU positions.
7. **Two named operational protocols.** "Known vessel" (prior voyages of the same vessel allowed, strictly earlier in time) and "unseen vessel" (vessel-disjoint) answer different deployment questions. Both are reported.
8. **Paired metrics and error decomposition.** All models are scored on the identical evaluable set, with bootstrap confidence intervals and along-track/cross-track error.
9. **Navigable-water mask replaces Natural Earth** for feasibility scoring. Natural Earth is too coarse for ship channels.
10. **Conformal displacement radii** are added as a cheap, calibrated uncertainty baseline, well before any multi-route model.
11. **Schema additions.** Draft, dimensions, transceiver class, and provenance of static fields (NOAA back-fills static fields from a vessel database, so they are not strictly as-of data).
12. **TrAISformer via the paper's reimplementation; SKETCH moved to a watch list.**

## 1. Product direction

The system is a research-oriented **historical AIS playback and vessel trajectory prediction workbench**. An operator picks a vessel and a historical `as_of` time; the application reveals only the AIS information available at that cutoff, generates forecast trajectories using one or more models, then optionally overlays the withheld actual track for evaluation.

The primary question is **where will this vessel be at a given horizon after the cutoff?** This is *not* a port-destination classification application, and it is *not* initially a live AIS tracker.

### Product principles

1. **Measure before building.** Know the data's coverage and the baselines' error before writing product code.
2. **Evaluation is the product's core.** The UI is a window onto a trustworthy evaluator, not the other way around.
3. **Model-agnostic architecture.** Baselines, retrieval, Timestral, TrAISformer, and future models share one contract.
4. **No hindsight leakage.** A forecast only sees data and covariates actually available at `as_of`. Labels are held out.
5. **Credible evaluation.** Report failure, missing ground truth, and insufficient history explicitly. Compare against baselines on identical samples, not against paper metrics.
6. **Uncertainty is earned.** Never invent probabilities from per-coordinate quantiles or label an arbitrary buffer as a 90% region. Calibrated regions require held-out calibration.
7. **Local-first development.** No managed cloud, authentication, Kubernetes, or paid AIS API for the MVP.
8. **Replay before real time.** Live AIS integration comes after offline behavior is credible.

### Operating envelope

- **Cutoff region:** Houston–Galveston, 28.0–30.5°N, 96.0–93.3°W (matches the calibration paper). Configurable, never hardcoded.
- **Truth region:** the full U.S. Gulf coast terrestrial coverage, configurable. Forecasts that start in the cutoff region are scored against truth anywhere in the truth region.
- **Calibration period:** June 2023 (matches the calibration paper). Later periods can be added once the evaluator is calibrated.
- **Resolution:** 10-minute regular grid by default (matches the paper and TrAISformer convention). 1-minute available for experiments; NOAA's native floor is about one minute.
- **Horizons:** decided by the Milestone 0 coverage audit, using the rule in §6.4. Working assumption until then: 1/2/3/6 h first-class; 12/24 h conditional on coverage; 48/72 h experimental and requiring additional data (Milestone 9).
- **Imports:** support offline CSV/Parquet from other AIS providers so multiday ocean-going evaluation can be added without changing the core architecture.
- Never silently extrapolate a continuous track through gaps in receiver coverage.

## 2. Proposed stack

- **Frontend:** Next.js, React, TypeScript, Tailwind; MapLibre GL JS for tracks and prediction overlays.
- **Backend:** Python 3.11+, FastAPI, Pydantic, uv.
- **Data processing:** Polars, PyArrow, NumPy; pandas only where an upstream model requires it.
- **Geographic math:** pyproj and geographiclib for geodesics, Shapely for geometry.
- **Navigable-water mask:** OSM water/land polygons or NOAA ENC-derived polygons, plus an optional AIS-density mask fit from **training-period data only**. Natural Earth is used only for zoomed-out basemap rendering, never for feasibility scoring.
- **Analytical storage:** partitioned Parquet queried with DuckDB. No database service in v0.x.
- **Model runtime:** PyTorch; Timestral package only when running its adapter.
- **Analysis:** Jupyter or marimo notebooks under `research/` for the Milestone 0 spike; logic that survives the spike moves into the package with tests.
- **Tests:** pytest, Ruff, mypy where practical; Vitest for frontend units; one Playwright smoke test.
- **Developer workflow:** `make dev`, `make test`, `make demo`, `make calibrate`, `.env.example`, pinned Python and Node dependencies, GitHub Actions smoke CI (no proprietary data or model weights).
- **Licensing:** document datasets, tiles, geospatial files, model code, model weights and their license restrictions separately. Check the Timestral weights license before any non-research use.

A CPU-only laptop must be able to run the baselines, retrieval, the evaluator and the UI. Model downloads and inference are optional and lazy. Docker Compose may be added for convenience but is never required.

## 3. Architecture

```text
NOAA daily CSV (stream-filtered by bbox) / user CSV / Parquet
               |
       ingest + validate
               |
    canonical observations (Parquet, partitioned by date/region)
               |
        track segmenter (gaps, motion state)
               |
    cutoff sampler  -------------------------+
               |                             |
    regularly sampled model windows     split manifests
               |                        (time / vessel / region)
+--------------+----------------+            |
|                               |            |
model registry              evaluation engine <+
|                               |
+-- Persistence                 +-- paired scoring on identical evaluable set
+-- Constant velocity           +-- along/cross-track decomposition
+-- Constant turn rate          +-- bootstrap CIs, stratification
+-- Damped velocity             |
+-- Route retrieval             +-- conformal radii
+-- Timestral (3 variants)      +-- golden-number calibration
+-- TrAISformer (paper reimpl.) +-- immutable runs
|                               |
+---------------+---------------+
                |
           FastAPI
                |
     Next.js + MapLibre UI
```

### Repository structure

```text
maritime-forecast-lab/
  README.md
  AGENTS.md
  .env.example
  Makefile
  docs/
    architecture.md
    data-contracts.md
    evaluation-protocol.md
    cutoff-sampling.md
    coverage-audit.md          # Milestone 0 findings; decides horizons
    model-integration-notes.md
    implementation-log.md
  backend/
    pyproject.toml
    src/maritime_forecast/
      api/
      config/
      data/{ingest,validate,segment,resample,windows,cutoffs}.py
      geo/{coordinates,geodesic,navigable_mask}.py
      models/{base,registry,persistence,constant_velocity,constant_turn,route_retrieval,timestral_adapter}.py
      evaluation/{splits,metrics,decomposition,runner,bootstrap,conformal,calibration_check}.py
      storage/{parquet_store,run_store}.py
    tests/{unit,integration,fixtures,golden}/
  frontend/
    app/
    components/{map,vessel,forecast,evaluation}/
    lib/{api,types}/
    tests/
  scripts/
    fetch_noaa.py
    import_ais.py
    coverage_audit.py
    run_benchmark.py
    build_demo.py
  data/
    README.md
    raw/.gitkeep
    processed/.gitkeep
    manifests/.gitkeep          # split manifests, including the paper's if obtained
    runs/.gitkeep
  research/
    notebooks/
    adapters/                   # pinned, isolated third-party code
```

Data files, downloaded models, secrets and checkpoints belong in `.gitignore`. Third-party research code stays in `research/adapters/` or pinned isolated environments, never in first-party modules.

## 4. Data contract

### Canonical AIS observation

```text
vessel_id: str                # normalized identity, typically MMSI
observed_at: datetime UTC     # timezone-aware, never local-time strings
latitude_deg: float
longitude_deg: float
sog_knots: float | null
cog_deg: float | null
heading_deg: float | null
nav_status: str | null
vessel_type: str | null
length_m: float | null
width_m: float | null
draft_m: float | null
transceiver_class: str | null  # A / B
destination: str | null        # not present in NOAA point data; other sources only
eta: datetime | null           # not present in NOAA point data; other sources only
source: str
static_fields_provenance: str  # "broadcast" | "backfilled" | "unknown"
quality_flags: list[str]
```

Optional: source message id, IMO and call sign if licensed, receiver class, ingestion timestamp.

**Notes:**
- MMSI is a source identity, not a guaranteed permanent hull identity. Document its limitations.
- NOAA MarineCadastre back-fills IMO, name, dimensions and vessel type from the AIS Vessel Identification Database. These fields are therefore not strictly as-of data. Accept this, set `static_fields_provenance = "backfilled"`, and document it. Do not treat these fields as evidence of what an operator knew at `as_of`.
- Draft is a cheap loaded/ballast signal. Record changes in draft as a feature candidate.

### Processing rules

- Validate and deduplicate; order strictly by UTC time per vessel.
- Drop or flag out-of-range coordinates and SOG/COG sentinel values per the source's data dictionary.
- Flag implausible jumps using elapsed time and a configurable speed bound; don't delete legitimate high-speed observations.
- Remove stationary-anchor GPS jitter for model windows, but keep stationary periods as segments with a motion-state label (they matter for persistence and departure timing).
- Segment by vessel, motion state and gaps (default gap threshold 30 minutes, configurable). Never interpolate across large gaps; preserve gap indicators.
- Regularize **within** segments at the configured interval without using points after the cutoff. Context-only interpolation may use context-only neighbors. Never create synthetic ground truth across significant gaps.
- For learned models, use local east-north coordinates centered at the last observed position, with the projection and its validity radius documented. Recover lat/lon geodesically. Handle the antimeridian and projection limits; flag forecasts that leave the validity radius.
- Encode course cyclically. Preserve navigation and motion state.
- Track coverage and valid label availability by horizon.
- Do not imply terrestrial AIS covers whole ocean voyages; label gaps caused by receiver reach or interference.

### Data acquisition

- NOAA national daily files are large. Stream-filter by bounding box and time with DuckDB or Polars at download time and keep only the Gulf truth region. Never keep national files in `data/processed/`.
- Record file names, checksums and filter parameters in a dataset manifest.

### Forecast request and response

```json
{
  "vessel_id": "123456789",
  "as_of": "2023-06-10T12:00:00Z",
  "history_hours": 24,
  "horizon_hours": 6,
  "step_minutes": 10,
  "model_ids": ["constant_velocity_v1", "route_retrieval_v1"],
  "show_ground_truth": false
}
```

The response contains `forecast_id`, model/version/config, source and cutoff time, input coverage and quality, `target_times`, predicted `[{timestamp, latitude_deg, longitude_deg}]`, runtime, warnings, forecast status, and an optional, properly labeled uncertainty representation (for example a conformal radius per horizon with its nominal level and calibration set id). Multiple candidate routes are represented as `trajectories`; scores attached to them are labeled `similarity`, not `probability`, unless calibrated. Withheld truth and error metrics are returned only by a separate evaluation response.

**Reproducibility:** every prediction must be reproducible from a pinned model version, input snapshot, seed (where applicable) and preprocessing config.

## 5. Models

### Contract

```python
class TrajectoryForecaster(Protocol):
    model_id: str
    def capabilities(self) -> ModelCapabilities: ...
    def predict(self, history: AISHistory, horizon: ForecastHorizon) -> ForecastResult: ...
```

`ModelCapabilities` covers required history, horizon and resolution limits, missing-input support, uncertainty support, whether it reads data beyond the target vessel's own history (retrieval index, neighbors), and whether dependencies and checkpoints are installed. Unsupported requests return a typed error, never fake predictions.

### B0 — Deterministic baselines (Milestone 2)

- **Persistence:** stays at the last position. Strong for stationary vessels.
- **Constant velocity:** travels `SOG × elapsed` along `COG` on the WGS84 geodesic from the last valid observation. Implement a "paper-compatible" mode that uses the anchor report's speed and course exactly as the calibration paper describes.
- **Constant turn rate:** estimates turn rate from recent COG history and holds it, with a configurable cap and decay.
- **Damped velocity:** travels along COG with speed decaying exponentially from the anchor SOG, `v(t) = SOG · exp(−t/τ)`. Distance therefore saturates at `SOG · τ`. τ is fit per vessel class on protocol-permitted training data only (it must never be tuned on test). τ → ∞ recovers constant velocity, and τ → 0 recovers persistence.
- Respect stale observation timestamps and surface warnings.
- Never silently constrain paths to water; feasibility is scored separately.
- Tests: bearing wrap, longitude wrap, high latitudes, zero speed, knot conversion, missing COG, irregular timestamps.

### R1 — Historical-route retrieval (Milestone 3)

- Index completed track segments that ended **strictly before** the cutoff. Which vessels are eligible depends on the protocol (§6.2): the target's own earlier voyages are allowed under P-known and excluded under P-unseen.
- Match on initial position, recent course and speed, vessel type and recent track shape. Start with plain nearest neighbors over local-frame trajectories; no vector database until scale requires one.
- Align matched references to the current position and continue along them to produce top-K candidate paths and a central prediction (for example a medoid or weighted mean).
- Output similarity scores, clearly labeled **not probabilities**.
- Provenance: each forecast lists the reference segment ids it used.
- Expect this to beat neural models at channel junctions and in lanes. Evaluate rather than assume.

### U1 — Conformal displacement radii (Milestone 4)

- For each model, horizon and stratum (vessel class × motion state), fit split-conformal radii on a calibration window that is disjoint from both the retrieval index period and the test period.
- Report nominal level, empirical coverage on test, and mean area. This is the honest uncertainty baseline that any later probabilistic model must beat.

### T1 — Timestral zero-shot adapter (Milestone 6; closed, negative result)

Timestral was released 2026-10-15 and is pretrained mostly on synthetic series. It has no knowledge of coastlines or shipping lanes. Expect it to behave roughly like a smoothed kinematic extrapolator unless given route information; test that expectation rather than assuming either way.

- Use the official package and API (`mistralai/timestral`), starting with `Timestral-28M`. Lazy download and cache; CPU only.
- **Pin the exact checkpoint revision and package commit.** Public materials for the release list inconsistent parameter counts, so record the revision hash, not just the name.
- Architecture facts that constrain the adapter: patches of 32 time steps; up to 1,024 forecast steps per forward pass; longer horizons are rolled out block by block by appending the median forecast to the context.
  - At 10-minute resolution, 24 h of history is only 144 steps (about 4.5 patches). Test 48 h and 72 h history lengths too.
  - At 1-minute resolution, horizons beyond about 17 h exceed one forward pass and compound through block rollout. Record which regime each result used.
- Implement three named representations and compare them on identical samples:
  - **T1-pos:** local east/north position channels.
  - **T1-vel:** east/north velocity components, forecast and then integrated geodesically to positions.
  - **T1-vel+route:** T1-vel plus the displacement of the best R1 retrieved route supplied as a known-future covariate. This is legal because the route comes only from pre-cutoff data under the active protocol.
- Use only observed, permitted context covariates. Any future weather or current covariate must be an archived forecast available at `as_of`.
- Validate finite outputs and plausible speeds after reconstruction.
- Timestral provides marginal quantiles per variable. **Never combine marginal x/y or lat/lon intervals into a claimed route-confidence corridor.** Use U1 conformal radii for uncertainty instead.
- Smoke inference is optional in CI; ordinary tests never download checkpoints.

### N1 — TrAISformer (Milestone 8)

- Prefer the leakage paper's released reimplementation and split manifests over the original upstream repository. That reimplementation reproduced the published Danish numbers within a few percent, and it already runs on the NOAA Houston corpus.
- If the paper's artifacts can't be obtained, fall back to the upstream repo (`CIA-Oceanix/TrAISformer`): reproduce one native checkpoint first, then build the conversion.
- Report greedy (deterministic) decoding as the operational number. Best-of-N may be shown only as a separately labeled diagnostic, next to the same oracle applied to a noise-perturbed constant-velocity control.
- TrAISformer uses absolute position bins, so it is expected to fail on region-disjoint tests. Document this rather than treat it as a bug.

### H — Hex prediction sets (Milestone 7)

The answer to "where will this vessel be at horizon h?" is a **probability over H3 cells** and a **calibrated prediction set**: the smallest group of cells holding the true position with the nominal probability (e.g. 90%).

- **Resolution:** H3 res 7 (about 5.2 km² per cell) up to 3 h, res 6 (about 36 km²) beyond. Fixed per horizon; the set is reported in km².
- **H1 — retrieval ensemble:** the top-K (e.g. 50) retrieved continuations that cover the horizon, weighted by similarity, as a kernel density in the local frame.
  - It is mixed with a small broad component around the central path, so the truth always has non-zero density.
  - The density is evaluated at cell centres. Land cells can be removed with the train-split navigable mask.
- **H2 — H3 transition model:** a table of (cell, course bin, speed bin) → cell after Δt, fitted on protocol-permitted training data only. It is rolled out by Monte Carlo to give the distribution at each horizon, and it reaches long horizons by chaining. Candidates from the literature: hex-grid route/destination probabilities (arXiv 2310.18948), Markov models over grids, non-parametric transition sampling (arXiv 2608.14349).
- **Calibration:** split-conformal density level sets.
  - Score = density at the true position.
  - Threshold = the calibration quantile.
  - Set = cells at or above it.
  - Fitted per horizon × stratum on a calibration window, exactly like U1. Marginal coverage only; stated as such.
- **Baseline to beat:** U1's conformal disc around the best point model, converted to cells at the same resolution. Success means the same coverage with a clearly smaller area.
- Similarity-derived weights become probabilities only after this calibration; until then they are labelled densities.

### Watch list (not scheduled)

- **SKETCH** (ICML 2026): conditions on a predicted Next Key Point for long horizons. The repository has TODO paths and needs semantic key-point preprocessing for new datasets. Revisit only if retrieval clearly fails at long horizons and intent modeling is the measured gap.
- **Multi-route hybrid:** fuse kinematic prediction with retrieval and key-point hypotheses. Scenario probabilities only after held-out calibration. Build only after failure analysis shows where simpler models break.
- **Neighbor and interaction models:** the calibration paper found graph attention over nearby traffic gave no measurable benefit at this scale. Any model reading co-present traffic must declare that neighbor context can come from training vessels and measure the effect.

## 6. Evaluation design

This section matters more than model choice. Arxiv 2609.25827 (September 2026) showed that oracle best-of-N decoding, vessel-sharing splits and geographic overlap can inflate reported AIS forecasting accuracy severalfold. The evaluator prevents these by default.

### 6.1 Replay methodology

1. A cutoff sampler (§6.3) chooses vessel and `as_of` at a real observation time.
2. The model receives only history at or before the cutoff, plus whatever auxiliary data the protocol permits.
3. The model predicts a fixed target-time grid.
4. Withheld observations after the cutoff are retrieved from the **truth region** using a declared matching tolerance.
5. Only target times with real ground-truth coverage are scored. Others get `not_evaluable` with a reason (left coverage, gap, segment ended, insufficient history).
6. Immutable run metadata and per-sample, per-horizon records are stored.

### 6.2 Protocols and leakage guards

Each protocol is a named, versioned split manifest.

- **P-known (operational, known vessel):** chronological split. Training, retrieval index and calibration data strictly precede test cutoffs. The target vessel's own earlier voyages are allowed. Answers: "how well do we do on vessels we've seen before?"
- **P-unseen (vessel-disjoint):** chronological split plus vessel-disjoint assignment by MMSI hash. Answers: "how well do we do on a vessel we've never seen?"
- **P-region (region-disjoint):** train on one side of a geographic cut, test on the other; drop windows that straddle the cut. Answers: "does this transfer to new waters?"
- **P-paper (calibration only):** the calibration paper's own Houston manifests (or a faithful reconstruction, §6.6). Used to check the evaluator, not to report product performance.

Guards:
- Purge context and label windows that overlap split boundaries.
- Fit normalizers, retrieval indices, navigable-water density masks and conformal radii only on data permitted by the active protocol.
- Retrieval: reference segments must end before the cutoff. Near-duplicate voyages after the cutoff are never eligible.
- Best-of-K selected using future truth is never reported as operational accuracy.
- Persist source dataset, geography, vessel class, preprocessing version and cutoff metadata per run.

### 6.3 Cutoff sampling policy

Uniform sampling over observations would be dominated by moored and anchored vessels and by the largest classes (tugs and towing are the single largest class by vessel count in Houston–Galveston). The sampler is therefore explicit and configurable:

- **Underway set (primary):** SOG ≥ 2 kn averaged over the last 10 minutes; at least a minimum number of reports in the history window.
- **Stationary set (secondary):** separate, used for persistence and departure-timing analysis; never pooled with the underway set in headline metrics.
- **Spacing:** cutoffs within one track at least 3 hours apart so evaluated contexts don't share reports.
- **Caps:** at most K cutoffs per vessel per day (default 2) to stop a few high-volume vessels dominating.
- **Stratification:** every metric is reported by vessel class and motion state, with sample counts.
- **Transiting stratum (headline):** underway cutoffs from cargo, tanker and passenger classes (AIS types 60–89) whose trailing 30-minute mean SOG is ≥ 2 kn **and** that had no report below 0.5 kn in those 30 minutes. It is reported alongside the pooled underway set in every headline table, because pooled port metrics are dominated by short harbour moves.
- Sampler configuration and seed are part of the manifest.

### 6.4 Horizon admission rule

The rule is applied **per stratum** (vessel class × motion state, plus the transiting stratum). A horizon is first-class for a stratum only when, within that stratum, all three conditions hold:

- at least a configured fraction of its cutoffs (default 50%) have valid truth at that horizon;
- the evaluable cutoffs are not slower than the stratum's population (default: their mean cutoff SOG is no more than 25% below the stratum's mean);
- a minimum number of evaluable cutoffs and vessels exist (default 100 cutoffs from 20 vessels).

Strata that fail are marked experimental at that horizon in the UI and API. Results for experimental strata may be shown but are excluded from headline tables. A pooled admission is reported only for information. The coverage audit applies the rule and records the result in `docs/coverage-audit.md`.

### 6.5 Metrics

- Geodesic displacement error (km and nmi) at each admitted horizon.
- ADE over valid matched points and FDE at the horizon.
- **Along-track and cross-track error** relative to the true path, separating "wrong speed" from "wrong route."
- Median, p90 and mean, with **paired bootstrap 95% CIs** (resampling by vessel, not by sample).
- **Paired scoring:** all models in a comparison are scored on the identical set of evaluable (cutoff, horizon) pairs. A model that fails on a sample gets a recorded failure, not a silent drop.
- Skill relative to persistence, constant velocity and damped velocity (the best of the three per stratum is the reference to beat).
- Feasibility: implausible speed or acceleration, navigable-mask violations, impossible jumps.
- Ground-truth availability, evaluable counts and `not_evaluable` reasons by horizon.
- Runtime and approximate memory.
- Uncertainty (once added): empirical coverage versus nominal level, and region area.

### 6.7 Prediction-set metrics (Milestone 7)

- **Coverage:** whether the true position's H3 cell is in the set, against the nominal level, with vessel-bootstrap CIs, per stratum and horizon.
- **Size:** set area in km² (cells × cell area), mean and median.
- **Proper score:** the log score of the cell probability at the truth (with a stated floor). Also the hit rate of the single most probable cell.
- **Pairing:** every set model and the disc baseline are scored on the identical evaluable samples; the same rules as §6.5.
- **Adoption:** a set model replaces the disc baseline only if it is within the coverage tolerance on the held-out set and has a smaller mean area. The rule is pre-registered before any July look.

No accuracy target is advertised before analysis. The basic success condition is **reliably outperforming the best of persistence, constant velocity and damped velocity, per admitted stratum, on an untouched held-out set under P-known and P-unseen**, with the transiting stratum reported separately. If that doesn't happen, surface it clearly.

### 6.6 Evaluator calibration (golden numbers)

The calibration paper reports these results on its vessel-disjoint NOAA Houston–Galveston split (June 2023; 10-minute grid; 18 context and 18 target steps; 30-minute gap segmentation; Class A/B and coordinate filter; stationary jitter removal; test windows spaced 3 h apart):

| Method | 1 h | 2 h | 3 h |
|---|---|---|---|
| Constant velocity, deterministic | 3.69 km | 9.03 km | 15.33 km |
| TrAISformer, greedy | 2.20 km | 5.07 km | 8.44 km |

- Obtain the paper's released manifests and preprocessing if available. Otherwise reconstruct the protocol from the paper's description and document every assumption.
- `make calibrate` runs paper-compatible constant velocity on P-paper and compares with the table. Target: within about 10% at each horizon. A larger gap is a blocker to investigate before any model comparison is trusted.
- Store the comparison as a golden regression test in `tests/golden/`. This test needs local data, so it runs in `make calibrate`, not in CI.

## 7. Milestones

### Milestone 0 — Data spike and coverage audit (no app)

Goal: learn what the data can support before building product.

**Tasks**
- `scripts/fetch_noaa.py`: download NOAA MarineCadastre daily files for June 2023, stream-filtered to the Gulf truth region, with a dataset manifest. If network access is unavailable, stop and list the exact files for a human to download.
- Canonicalize to Parquet with the §4 schema (minimal validation is fine at this stage).
- Notebook plus `scripts/coverage_audit.py`:
  - Report inter-report interval distribution, class mix, and motion-state mix.
  - For underway cutoffs in the Houston cutoff region, report the fraction with valid truth at 1, 2, 3, 6, 12, 24 h, both within the cutoff region only and within the full truth region.
  - Report how the class and speed mix of evaluable cutoffs shifts as horizon grows (the survivorship check).
- Implement persistence and constant velocity in the notebook and compute errors on a first-pass cutoff sample.
- Attempt the §6.6 calibration with paper-compatible constant velocity.
- Write `docs/coverage-audit.md`: findings, admitted horizons per §6.4, calibration result, surprises, recommended changes to this plan.

**Done when:** the coverage audit is written, horizons are admitted or marked experimental by measured coverage, and constant velocity either matches the golden numbers or the discrepancy is explained. No frontend, no CI yet.

### Milestone 1 — Repository, contracts and data pipeline

**Tasks**
- Monorepo with backend and frontend, Makefile, lint, tests, `.env.example`, CI smoke workflow.
- `AGENTS.md` per §12.
- Pydantic schemas for observations, forecasts and benchmark runs; generated TypeScript types.
- Promote spike code into the package: versioned NOAA and generic CSV/Parquet ingestion with column mapping, validation, deduplication, jitter handling, gap-aware segmentation, partitioned Parquet with DuckDB queries.
- Cutoff sampler (§6.3) and split manifests for P-known, P-unseen, P-region and P-paper.
- Small synthetic fixture (moving, turning, stopping, gaps, antimeridian crossing), clearly labeled synthetic, for unit tests only.
- `GET /health`, `GET /datasets`, `GET /vessels`, `GET /vessels/{id}/track`.
- Minimal frontend: map showing a real vessel's track with gaps visibly distinct from observations.

**Done when:** one command starts both services; real NOAA data loads and replays without fabricated segments; tests cover bad values, timestamp cutoffs and manifest disjointness.

### Milestone 2 — Baselines and the evaluator

**Tasks**
- Persistence, constant velocity (standard and paper-compatible), constant turn rate and damped velocity (τ fit per class on training data).
- Evaluator: paired scoring, `not_evaluable` handling, ADE/FDE, along/cross-track decomposition, bootstrap CIs by vessel, stratification, feasibility flags against the navigable-water mask.
- Navigable-water mask from OSM or ENC polygons; optional training-only AIS density mask.
- `make calibrate` and the golden test (§6.6).
- `POST /forecasts` with deterministic cache keyed by request, config and data version; `POST /benchmarks`, `GET /benchmarks/{id}`, `GET /benchmarks/{id}/summary`.
- CLI for unattended benchmark runs, exporting JSON and GeoJSON.
- Leakage invariance test: altering or deleting all post-cutoff observations must leave every forecast unchanged.

**Done when:** an unattended CLI benchmark runs baselines on P-known and P-unseen, persists results, passes calibration, and every model passes the invariance test.

### Milestone 3 — Historical-route retrieval

**Tasks**
- Protocol-aware segment index (§5 R1).
- Matching, alignment, continuation, top-K candidates, central prediction, provenance.
- Benchmark against baselines on identical samples under P-known, P-unseen and P-region.
- Failure review by category: channel junctions, anchoring and departures, weather-driven turns, missing observations, new geography.

**Done when:** retrieval results are compared head-to-head with baselines by vessel class and horizon, provenance shows which references informed each forecast, and leakage tests prove no post-cutoff references were eligible.

### Milestone 4 — Conformal uncertainty

**Tasks**
- Split-conformal radii per model × horizon × stratum on a protocol-compliant calibration window.
- Report empirical coverage and area on test.
- API returns radii with nominal level and calibration-set id.

**Done when:** empirical coverage on test is within a stated tolerance of nominal for each admitted horizon, or deviations are reported per stratum.

### Milestone 5 — Workbench UI

**Tasks**
- Explorer, forecast comparison and benchmark dashboard (§9).
- Withheld-truth overlay only after a forecast is generated, styled distinctly.
- Failure gallery linked to the map.
- One Playwright smoke test: select vessel → set cutoff → run baseline → overlay truth → view error.

**Done when:** an operator can reproduce any benchmark sample on the map from its run id, and the dashboard shows paired results with CIs and evaluable counts.

### Milestone 6 — Timestral

**Tasks**
- Lazy, pinned `Timestral-28M` adapter with clear missing-dependency and download warnings.
- Representations T1-pos, T1-vel and T1-vel+route (§5 T1); history lengths 24/48/72 h; record resolution and rollout regime.
- Benchmark against baselines and retrieval on identical samples.
- Side-by-side map comparison; marginal quantiles shown only as per-axis marginals, never as a corridor.

**Done when:** all three variants run reproducibly on real replays, and the dashboard shows their error relative to baselines and retrieval, even if they lose.

**Status (2026-10-22): closed, negative result.**
- T1-pos and T1-vel were built, pinned and benchmarked on the dev window.
- A context-length study (24/48/72 h) was run on one paired set.
- Neither variant beat retrieval anywhere, and neither beat the best kinematic baseline on transiting ships. T1-vel+route was not built (v0.4).
- Findings: `docs/benchmarks.md`, `docs/model-integration-notes.md`. The adapter stays as an optional, unscored reference.

### Milestone 7 — Hex prediction sets

**Tasks**
- A set representation in the forecaster contract: per target time, H3 cells with probabilities, and the calibrated set with its nominal level, calibration-set id and area.
- H1 (retrieval ensemble) and the disc-to-cells baseline, compared on the dev window first. Calibrate on its first half, evaluate on its second.
- H2 (H3 transition model fitted on train).
- §6.7 metrics in the benchmark runner and dashboard. Draw sets on the map (MapLibre H3 polygons).
- Land-cell removal with the train-split navigable mask.
- Pre-registered July comparison against the disc baseline.

**Done when:** at least one set model is calibrated within tolerance on the July hold-out with a smaller mean area than the disc baseline, or the failure is documented per stratum. The workbench shows sets for any forecast.

### Milestone 8 — Research models

**Tasks**
- TrAISformer via the calibration paper's reimplementation (§5 N1); greedy decoding as the operational point, and its bin probabilities scored as a distribution under §6.7.
- Log every adaptation difference from the source papers.
- Decide on further model work (hybrid, intent modeling, SKETCH) from measured failure categories.

**Done when:** each integrated model runs through the same API, evaluator and map with documented limitations. A blocked integration never blocks other milestones.

### Milestone 9 — Multi-day credibility and optional near-real-time ingest

**Tasks**
- Source data suitable for 24–72 h horizons (licensed satellite AIS or additional regional networks). Verify data rights, coverage, cadence, gaps and label sufficiency.
- Apply the §6.4 admission rule to the new data before exposing long horizons.
- Only then consider live streaming, scheduled forecasts, alerts and storage for continuous ingestion.

**Done when:** multiday performance is backed by valid withheld ground truth, not coastal coverage gaps or extrapolated labels.

## 8. API surface

- `GET /health`: backend and adapter availability.
- `GET /datasets`: datasets, versions, sources, geography, coverage summaries and admitted horizons.
- `GET /vessels?dataset_id=&bbox=&limit=`: paginated vessels.
- `GET /vessels/{vessel_id}/track?dataset_id=&from=&to=`: raw and segmented track.
- `GET /models`: model ids, capabilities, installation status.
- `POST /forecasts`: validate, forecast, persist; return results or typed unsupported errors.
- `POST /benchmarks`: run a benchmark with a named protocol, sampler config and model set.
- `GET /benchmarks/{run_id}` and `/summary`: immutable configuration, status, metrics, CIs and errors.

Pydantic/OpenAPI generates typed TypeScript clients. Small forecasts may be synchronous; benchmarks use a local background process once implemented, not unimplemented external infrastructure.

## 9. UI behavior

**Screen A — Explorer:** full-bleed map. Side panel with dataset, vessel search, MMSI and type, last observed time, last SOG/COG, coverage, a historical time scrubber and a `Forecast` action.

**Screen B — Forecast comparison:** past observed path; model-specific dashed predictions with timestamps; conformal radii where available; optional truth overlay; horizon slider; model toggles; warnings and run metadata. Truth and prediction never share a style. Experimental horizons are visibly labeled. Never bridge AIS gaps.

**Screen C — Benchmark dashboard:** model × horizon table with CIs; along/cross-track breakdown; error versus lead time; stratification by class and motion state; evaluable and `not_evaluable` counts with reasons; failure gallery opening the map; reproducibility metadata.

Design direction: precise, professional analysis software; restraint over decoration; smooth with tens of thousands of points (simplify for rendering only, never for evaluation).

## 10. Tests and safeguards

- **Geometry:** bearings in all quadrants, high latitudes, antimeridian, zero speed, nmi/km conversions, invalid positions, ENU validity-radius flags.
- **Data:** out-of-order timestamps, duplicates, NaNs, sentinel values, impossible jumps, gaps, moored/anchored transitions, missing features, mixed MMSIs, static-field provenance.
- **Temporal leakage:** modify or delete all observations after `as_of`; every adapter's output must be unchanged.
- **Protocol leakage:** manifests are disjoint on their stated axis; retrieval, normalizers, masks and conformal radii use only protocol-permitted data.
- **Evaluation:** removing withheld truth still lets forecasts run, and evaluation reports missing labels; no interpolated truth across long gaps; paired scoring uses an identical sample set across models.
- **Golden:** `make calibrate` reproduces §6.6 within tolerance (local data required, not in CI).
- **Models:** every adapter produces the requested timestamp grid and a valid schema, or a typed error.
- **Frontend:** one end-to-end smoke test on the fixture.
- **Network:** no downloads in ordinary unit tests.
- **Security:** no secrets in logs, no arbitrary file-path access via API, input size limits, sanitized imported fields, license compliance.

## 11. Decisions deferred deliberately

- Cloud deployment, scale-out and multiuser authentication.
- Real-time or satellite AIS providers and paid subscriptions.
- Training deep models from scratch (beyond reproducing the paper's TrAISformer reimplementation).
- Global coverage, ice/river routing, collision-avoidance-grade outputs.
- An LLM agent interface. Trajectory forecasting is a numeric/geographic problem first.
- Any high-confidence 24–72 h claim before coverage and calibration support it.

## 12. Codex operating instructions

Create `AGENTS.md` with these requirements:

1. Work through milestones in order. Do not build several at once.
2. Use small, verifiable changes, with tests and doc updates at each step.
3. Prefer integration over novel infrastructure; avoid abstractions until two implementations need them.
4. Never substitute synthetic observations for real AIS in a benchmark.
5. Every result labels data provenance, protocol, sampler config, model revision, horizon and quality warnings.
6. Treat missing AIS and geographic infeasibility as first-class failure modes.
7. Never report best-of-N or other truth-selected numbers as operational accuracy.
8. Score models only on identical evaluable sample sets.
9. Maintain `docs/implementation-log.md` with completed tasks, blockers, commands run, results and the suggested next step.
10. Keep third-party research code isolated and pinned; never overwrite upstream files or assume research demos are drop-in.
11. Do not move to the next milestone unless acceptance criteria are met or a blocker is documented for review.
12. Ask for a human decision only for genuinely blocking choices (data purchases, network-restricted downloads, major architecture changes, failed calibration), not routine implementation preferences.

## 13. First Codex task — copy and paste

> You are the lead engineer on Maritime Forecast Lab. Read `AIS_Trajectory_Forecasting_Codex_Build_Plan.md` in the repository root and implement **Milestone 0 only**: the data spike and coverage audit. Do not build the frontend, API or CI yet. Write `scripts/fetch_noaa.py` to obtain NOAA MarineCadastre AIS for June 2023, stream-filtered to the U.S. Gulf truth region with a dataset manifest; if you cannot reach the network, stop and list the exact files for me to download. Canonicalize to Parquet per §4. Write `scripts/coverage_audit.py` and a notebook that produce the measurements listed under Milestone 0, implement persistence and constant velocity (including the paper-compatible mode), and attempt the §6.6 calibration. Write `docs/coverage-audit.md` with findings, horizons admitted under §6.4, the calibration result, and recommended plan changes. Report the commands you ran, results, open issues, and the smallest next slice of Milestone 1. Do not proceed to Milestone 1 without review.

## 14. References

- Timestral announcement and runtime: https://mistral.ai/news/timestral/ and https://github.com/mistralai/timestral
- Timestral model cards (architecture, horizon and patch details): https://huggingface.co/mistralai/Timestral-5M
- Leakage-aware evaluation of AIS trajectory prediction (calibration paper; NOAA Houston–Galveston and Danish corpora; released splits and code): https://arxiv.org/abs/2609.25827
- TrAISformer paper: https://arxiv.org/abs/2109.03958 — repository: https://github.com/CIA-Oceanix/TrAISformer
- Non-parametric state-conditioned transition sampling (beats TrAISformer top-1 without learned parameters): https://arxiv.org/abs/2608.14349
- EnvShip-Bench (vessel-disjoint public benchmark including NOAA): https://arxiv.org/abs/2606.15240
- Multi-path long-term forecasting with route and destination features (Spadon et al.): https://arxiv.org/abs/2310.18948
- Standard-route extraction from AIS for longer-horizon prediction: https://arxiv.org/abs/2503.22734
- SKETCH (watch list): https://arxiv.org/abs/2601.18537 — https://proceedings.mlr.press/v306/gan26b.html — https://github.com/LinyongGAN/SKETCH
- NOAA AccessAIS: https://coast.noaa.gov/digitalcoast/tools/ais.html
- NOAA national AIS point-data metadata (one-minute filtering, static-field back-fill): https://www.fisheries.noaa.gov/inport/item/77594

**No benchmark scores or multiday accuracy claims are guaranteed by this plan.** Treat every published comparison as dataset- and protocol-specific until replicated under our protocol.
