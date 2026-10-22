"""Unattended paired benchmark of registered models (build plan §6, Milestone 2).

Writes an immutable run to data/runs/benchmarks/<run_id>/ (run.json, records.parquet,
cutoffs.parquet, report.md, files.json), then a GeoJSON export of the paired transiting
stratum to data/runs/exports/<run_id>/transiting.geojson (scripts/export_geojson.py for more).

Usage:
    uv run --project backend python scripts/run_benchmark.py --protocol P-known
    uv run --project backend python scripts/run_benchmark.py --protocol P-unseen \\
        --models persistence_v1 constant_velocity_v1 --horizons 1 2 3 6 12 24
    uv run --project backend python scripts/run_benchmark.py --protocol P-known \
        --dataset-id noaa_us_gulf_2023-06-07 --split dev      # model development (dev window)
"""

from __future__ import annotations

import argparse
import time

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.evaluation.export import write_run_geojson
from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", required=True, choices=["P-known", "P-unseen", "P-region"])
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    ap.add_argument("--models", nargs="*", default=[], help="default: all registered models")
    ap.add_argument("--horizons", nargs="*", type=float, default=[1, 2, 3, 6, 12, 24])
    ap.add_argument("--step-minutes", type=int, default=10)
    ap.add_argument("--history-hours", type=float, default=24)
    ap.add_argument("--seed", type=int, default=20230601)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument(
        "--split",
        default="test",
        choices=["test", "dev", "calibration"],
        help="dev: model development on the dev window; calibration: errors for conformal radii "
        "(June+July manifests only)",
    )
    ap.add_argument(
        "--confirm-test",
        action="store_true",
        help="score a HELD-OUT test split (splits-v2 manifests): design frozen on dev, "
        "clean tree; the run is recorded in data/manifests/test_access/ first",
    )
    ap.add_argument(
        "--informed-by",
        nargs="*",
        default=[],
        help="run/review ids whose results shaped the models (recorded in the ledger)",
    )
    ap.add_argument(
        "--test-rerun-reason",
        default=None,
        help="required to score an already-scored model version on a held-out test",
    )
    ap.add_argument("--max-cutoffs-per-set", type=int, default=None, help="smoke runs only")
    ap.add_argument("--no-geojson", action="store_true", help="skip the GeoJSON export")
    args = ap.parse_args()

    cfg = BenchmarkConfig(
        dataset_id=args.dataset_id,
        manifest_path=MANIFEST_DIR / "splits" / f"{args.protocol}__{args.dataset_id}.json",
        model_ids=tuple(args.models),
        horizons_h=tuple(args.horizons),
        step_minutes=args.step_minutes,
        history_hours=args.history_hours,
        seed=args.seed,
        n_boot=args.n_boot,
        max_cutoffs_per_set=args.max_cutoffs_per_set,
        split=args.split,
        confirm_test=args.confirm_test,
        informed_by=tuple(args.informed_by),
        test_rerun_reason=args.test_rerun_reason,
    )
    t0 = time.time()
    run = run_benchmark(cfg, ParquetStore(), RunStore())
    print(f"run {run.run_id}: {run.sampler['n_cutoffs']:,} cutoffs, {len(run.results)} cells, "
          f"{time.time() - t0:.0f} s")  # fmt: skip
    print(f"report: data/runs/benchmarks/{run.run_id}/report.md")
    if not args.no_geojson:
        out = write_run_geojson(
            RunStore(), run.run_id, ["underway:TRANSITING"], paired_only=True, name="transiting"
        )
        print(f"geojson: {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
