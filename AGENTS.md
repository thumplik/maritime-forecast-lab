# AGENTS.md — rules for any coding agent on Maritime Forecast Lab

These rules are binding for every agent (Claude Code, Codex, others) and for humans.
The full specification is [docs/build-plan.md](docs/build-plan.md); section numbers (§) below refer to it.

## Operating rules (§12)

1. **Work through milestones in order.** Do not build several at once. The current milestone is recorded at the top of [docs/implementation-log.md](docs/implementation-log.md).
2. **Small, verifiable changes**, each with tests and doc updates.
3. **Prefer integration over novel infrastructure.** No abstraction until two implementations need it.
4. **Never substitute synthetic observations for real AIS in a benchmark.** Synthetic fixtures are for unit tests only and are labeled synthetic.
5. **Every result labels** data provenance, protocol, sampler config, model revision, horizon and quality warnings.
6. **Missing AIS and geographic infeasibility are first-class failure modes**, never silently dropped.
7. **Never report best-of-N** or any other truth-selected number as operational accuracy.
8. **Score models only on identical evaluable sample sets** (paired scoring).
9. **Maintain [docs/implementation-log.md](docs/implementation-log.md)**: completed tasks, blockers, commands run, results, suggested next step.
10. **Third-party research code stays isolated and pinned** in `research/adapters/`. Never overwrite upstream files; never assume research demos are drop-in.
11. **Do not start the next milestone** until acceptance criteria are met or a blocker is documented for review.
12. **Ask a human only for genuinely blocking choices**: data purchases, network-restricted downloads, major architecture changes, failed calibration. Not routine implementation preferences.

13. **Update the GitHub README after every improvement.** When a change produces new kept benchmark results (a new or changed model, or an evaluator change that moves numbers), update [README.md](README.md) in the same commit:
    - the results section, with the new numbers;
    - the figures (`scripts/make_readme_figures.py`, pointed at the new run ids);
    - the roadmap status.

    Every README number must come from an immutable run listed in [docs/benchmarks.md](docs/benchmarks.md). Never use diagnostic, smoke, dev-split or calibration-split runs. Report losses as plainly as wins: if the new model does not beat the best baseline in a stratum or at a horizon, the README says so.

## Leakage invariants (§6.2) — violating any of these is a bug

- A forecast sees only observations with `observed_at <= as_of` for the target vessel, plus auxiliary data the active protocol permits.
- Retrieval reference segments must **end strictly before** the cutoff. Under P-unseen, the target vessel's own history is excluded from the index.
- Normalizers, retrieval indices, density masks and conformal radii are fit **only on protocol-permitted data**.
- Truth is never interpolated across gaps > the configured gap threshold (default 30 min).
- NOAA static fields (name, IMO, type, dimensions) are **back-filled**, not as-of; mark `static_fields_provenance = "backfilled"`.
- Similarity scores are labeled `similarity`, never `probability`, unless calibrated on held-out data.
- Never combine per-axis marginal quantiles into a claimed 2-D confidence region.

## Data hygiene

- Raw/processed data, model weights and checkpoints are never committed (see `.gitignore`). Dataset manifests in `data/manifests/*.json` are committed — they are the provenance record.
- Ordinary unit tests never touch the network or local NOAA data. Tests that need local data are marked `@pytest.mark.golden` and run via `make calibrate`.
- Never keep national NOAA files in `data/processed/`.
