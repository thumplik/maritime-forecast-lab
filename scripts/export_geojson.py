"""Export a benchmark run as GeoJSON (predictions, truth points, anchors) for a map viewer.

Runs are immutable, so exports go to data/runs/exports/<run_id>/<name>.geojson with a JSON
sidecar (run id, files.json digest, filter, git SHA). See maritime_forecast.evaluation.export.

Usage:
    uv run --project backend python scripts/export_geojson.py --run-id <id>
    uv run --project backend python scripts/export_geojson.py --run-id <id> \\
        --strata underway:TRANSITING --models persistence_v1 damped_velocity_v1 --paired-only
"""

from __future__ import annotations

import argparse

from maritime_forecast.evaluation.export import write_run_geojson
from maritime_forecast.storage.run_store import RunStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--strata", nargs="*", default=None)
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--paired-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="first N cutoffs (by cutoff_id)")
    ap.add_argument("--name", default=None, help="output name (default: hash of the filter)")
    args = ap.parse_args()
    out = write_run_geojson(
        RunStore(), args.run_id, args.strata, args.models, args.paired_only, args.limit, args.name
    )
    print(f"{out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
