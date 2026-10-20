"""Import AIS CSV/Parquet files from any provider into the canonical store via a mapping.

Mappings live in config/sources/<name>.yaml (see docs/data-contracts.md). Write a new one for a
new provider; set `verified: true` only after checking it against a real file.

Usage:
    uv run --project backend python scripts/import_ais.py --mapping dma_denmark \\
        --dataset-id dma_2023-06 path/to/aisdk-2023-06-01.csv [more files ...]
    uv run --project backend python scripts/import_ais.py --mapping path/to/my_source.yaml ...

Options --region <name> keeps only rows inside a region from config/regions.yaml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from maritime_forecast.config import region
from maritime_forecast.data.ingest import ingest, manifest_path
from maritime_forecast.data.mapping import load_mapping


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mapping", required=True, help="preset name or path to a mapping YAML")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--region", default=None)
    ap.add_argument("--no-hash", action="store_true", help="skip SHA-256 of inputs")
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args()

    mapping = load_mapping(args.mapping)
    if not mapping.verified:
        msg = (
            f"WARNING: mapping {mapping.mapping_id} is unverified against real provider files; "
            "check the ingest report before using this dataset."
        )
        print(msg, file=sys.stderr)
    report, written = ingest(
        args.files, mapping, args.dataset_id,
        region=region(args.region) if args.region else None, hash_inputs=not args.no_hash,
    )  # fmt: skip
    print(report.model_dump_json(indent=2))
    print(f"{len(written)} day partitions written -> manifest {manifest_path(args.dataset_id)}")


if __name__ == "__main__":
    main()
