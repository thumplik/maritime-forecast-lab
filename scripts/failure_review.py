"""Failure review of route_retrieval_v1 on a kept benchmark run (build plan §7, Milestone 3).

Re-runs retrieval on the run's cutoffs (checking it reproduces the run's predictions), flags
each paired sample with the categories in evaluation/failure_review.py, and writes
data/runs/reviews/<run_id>/{samples.parquet, summary.parquet, report.md, review.json}.

Usage:
    uv run --project backend python scripts/failure_review.py --protocol P-known
    uv run --project backend python scripts/failure_review.py --run-id <run_id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from maritime_forecast.config import RUNS_DIR
from maritime_forecast.evaluation import failure_review as fr
from maritime_forecast.evaluation.runner import BenchmarkConfig, benchmark_context
from maritime_forecast.models.registry import get_model
from maritime_forecast.provenance import git_sha
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore


def latest_run(protocol: str, dataset_id: str, split: str) -> str:
    """Newest finished run of `protocol` on `dataset_id` and `split` that includes retrieval."""
    runs = sorted(p.name for p in (RUNS_DIR / "benchmarks").glob(f"{protocol}_{dataset_id}_*")
                  if (p / "records.parquet").exists())  # fmt: skip
    for r in reversed(runs):
        run = RunStore().load(r)
        if (run.dataset_id == dataset_id and run.sampler.get("split") == split
                and fr.MODEL in {m.model_id for m in run.models}):  # fmt: skip
            return r
    raise SystemExit(f"no {protocol} {split} run on {dataset_id} includes {fr.MODEL}")


def render(run_id: str, summary: pl.DataFrame, n_failed: int) -> str:
    lines = [f"# Failure review: `{fr.MODEL}` on `{run_id}`", "",
             "Flags are not exclusive. `loss rate` = share of flagged samples where retrieval's "
             "error exceeds the run's best baseline for the cell (named per row). `share of "
             "losses` = flagged losses / all losses in the cell. Cells where the run has no "
             "skill reference (best baseline median < 10 m) compare with persistence for size "
             "only and score no losses (blank). Categories marked † select on truth: their "
             "errors describe a truth-conditioned subset, never accuracy. `wrong_branch` is an "
             "oracle over candidates; read it only against `wrong_branch_control`.", "",
             f"Retrieval forecasts that failed (`no_references`, not in the paired set): "
             f"{n_failed}.", ""]  # fmt: skip
    for (stratum,), g in summary.group_by("stratum", maintain_order=True):
        lines += [f"## {stratum}", "",
                  "| h | category | n | share | loss rate | share of losses | retrieval km | "
                  "best baseline km | median diff km |",
                  "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in g.iter_rows(named=True):
            sol = "" if r["share_of_losses"] is None else f"{r['share_of_losses']:.0%}"
            lr = "" if r["loss_rate"] is None else f"{r['loss_rate']:.0%}"
            cat = r["category"] + (" †" if r["selects_on_truth"] else "")
            lines.append(
                f"| {r['horizon_h']:g} | {cat} | {r['n']:,} | {r['share']:.0%} | "
                f"{lr} | {sol} | {r['rr_median_km']:.2f} | "
                f"{r['base_median_km']:.2f} ({r['base_model'].split('_')[0]}) | "
                f"{r['median_diff_km']:+.2f} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--protocol", choices=["P-known", "P-unseen", "P-region"])
    g.add_argument("--run-id")
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    ap.add_argument("--split", default="test", help="with --protocol: which split's latest run")
    ap.add_argument(
        "--allow-holdout",
        action="store_true",
        help="review a HELD-OUT test run (splits-v2); only after its results are "
        "reported, since a review shapes the next design",
    )
    args = ap.parse_args()
    run_id = args.run_id or latest_run(args.protocol, args.dataset_id, args.split)
    runs, store = RunStore(), ParquetStore()
    run = runs.load(run_id)
    sc = run.sampler
    cfg = BenchmarkConfig(dataset_id=run.dataset_id, manifest_path=Path(sc["manifest_path"]))
    _, ctx = benchmark_context(cfg, store)
    if ctx.manifest_sha256 != run.protocol.manifest_sha256:
        raise SystemExit("the split manifest changed since the run")
    from maritime_forecast.evaluation.holdout import is_holdout
    from maritime_forecast.schemas.manifest import SplitManifest

    manifest = SplitManifest.model_validate_json(Path(sc["manifest_path"]).read_text())
    if is_holdout(manifest) and sc.get("split") == "test" and not args.allow_holdout:
        raise SystemExit(f"{run_id} scored a held-out test split; pass --allow-holdout")
    model = get_model(fr.MODEL, ctx)
    ran = next(m for m in run.models if m.model_id == fr.MODEL)
    if json.dumps(ran.config, sort_keys=True, default=str) != json.dumps(
        model.config, sort_keys=True, default=str
    ):
        raise SystemExit("retrieval config differs from the run's; cannot reproduce it")
    records, cutoffs = runs.table(run_id, "records"), runs.table(run_id, "cutoffs")
    t0 = time.time()
    print(f"re-running {fr.MODEL} on {cutoffs.height:,} cutoffs of {run_id} ...", flush=True)
    diag = fr.rerun_diagnostics(model, store, run.dataset_id, cutoffs,
                                [h for h in sc["horizons_h"]], sc["step_minutes"],
                                sc["history_hours"])  # fmt: skip
    samples = fr.flag_samples(records, cutoffs, diag, fr.best_baselines(run))
    summary = fr.summarize(samples)
    n_failed = records.filter((pl.col("model_id") == fr.MODEL) & (pl.col("status") != "ok"))[
        "cutoff_id"
    ].n_unique()
    out = RUNS_DIR / "reviews" / run_id
    out.mkdir(parents=True, exist_ok=True)
    samples.write_parquet(out / "samples.parquet")
    summary.write_parquet(out / "summary.parquet")
    (out / "report.md").write_text(render(run_id, summary, n_failed))
    files = RUNS_DIR / "benchmarks" / run_id / "files.json"
    (out / "review.json").write_text(json.dumps({
        "run_id": run_id, "run_files_json_sha256": hashlib.sha256(files.read_bytes()).hexdigest(),
        "dataset_id": run.dataset_id, "protocol": run.protocol.model_dump(),
        "model_config": model.config,
        "thresholds": {k: getattr(fr, k) for k in (
            "SPREAD_MIN_M", "SPREAD_FRAC", "WRONG_BRANCH_MIN_ERR_M", "DEPART_M", "STAY_M",
            "SPARSE_STEPS", "TIMING_MIN_ERR_M", "CONTROL_SEED", "NO_REFERENCE_FALLBACK")},
        "sampler_seed": sc.get("seed"), "git_sha": git_sha(),
        "created_at": datetime.now(UTC).isoformat(), "seconds": round(time.time() - t0, 1),
    }, indent=2, default=str))  # fmt: skip
    n_unique = samples.select("cutoff_id", "horizon_h").unique().height
    print(f"wrote {out} ({n_unique:,} paired (cutoff, horizon) samples; {samples.height:,} rows "
          f"over strata; {time.time() - t0:.0f} s)")  # fmt: skip


if __name__ == "__main__":
    main()
