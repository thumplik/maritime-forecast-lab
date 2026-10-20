---
name: new-forecaster
description: Checklist for adding a trajectory forecaster (baseline, retrieval, Timestral, TrAISformer, ...) behind the shared model contract in §5.
---

A new model is done only when all of these hold:

- [ ] Implements the `TrajectoryForecaster` contract (`model_id`, `capabilities()`, `predict(history, horizon)`) in `backend/src/maritime_forecast/models/`.
- [ ] `capabilities()` declares required history, horizon/resolution limits, missing-input support, uncertainty support, whether it reads beyond the target's own history, and install status.
- [ ] Unsupported requests return a typed error — never a fabricated prediction.
- [ ] Output timestamps exactly match the requested target grid.
- [ ] Factory in `models/registry.py` has the shape `(store, dataset_id) -> model`; any data beyond `AISHistory` is read from THAT store (the harness blocks the real archive while it runs).
- [ ] `ModelCapabilities.reads_beyond_own_history` declared truthfully (it has no default).
- [ ] Passes `evaluation/invariance.py` (unit test on the synthetic fixture + golden real-data test), with no vacuous cases.
- [ ] Any cache is keyed by request, config **and data version** — the harness cannot detect a process-global cache that ignores the data version.
- [ ] Exceptions and non-finite outputs come back as typed failures (BaseForecaster does this; don't bypass it).
- [ ] If it uses any index or fitted state: fit only on protocol-permitted data; provenance (reference ids, fit window) in the output.
- [ ] Pinned version / checkpoint revision recorded; third-party code isolated in `research/adapters/<name>/` with README (upstream, commit, license, modifications).
- [ ] No network or checkpoint download in ordinary unit tests.
- [ ] Benchmarked on the identical evaluable set as persistence and constant velocity; result logged via `/log-progress`.
- [ ] `docs/model-integration-notes.md` updated with adaptation differences and known limitations.
- [ ] Reviewed by the `leakage-reviewer` subagent.
