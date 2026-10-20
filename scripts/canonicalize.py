"""Ingest raw NOAA Parquet partitions (from fetch_noaa.py) into the canonical store.

Input:  data/raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet
Output: data/processed/canonical/<dataset_id>/date=YYYY-MM-DD/part-0.parquet
        data/manifests/<dataset_id>.canonical.json   (DatasetManifest)

One day per ingest call, so each day's partition is replaced atomically and only from its own
source file (expect_days guards against spill into neighbouring days).

Usage:
    uv run --project backend python scripts/canonicalize.py --start 2023-06-01 --end 2023-06-30
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from maritime_forecast.config import RAW_DIR, default_region
from maritime_forecast.data.canonical import noaa_mapping
from maritime_forecast.data.ingest import ingest, manifest_path
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.storage.parquet_store import ParquetStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 6, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2023, 6, 30))
    ap.add_argument("--region", default=None, help="raw region name (default: truth region)")
    ap.add_argument("--dataset-id", default=None)
    ap.add_argument("--force", action="store_true", help="rebuild days already ingested")
    args = ap.parse_args()

    region = args.region or default_region("truth_region").name
    dataset_id = args.dataset_id or f"noaa_{region}_{args.start:%Y-%m}"
    raw_root = RAW_DIR / f"noaa_{region}"
    # The raw files were already bbox-filtered at download; the mapping view of NOAA's raw
    # Parquet is the CSV mapping with the file format switched.
    mapping = noaa_mapping().model_copy(update={"file_format": "parquet"})
    store = ParquetStore()
    done = set()
    mp = manifest_path(dataset_id)
    if mp.exists() and not args.force:
        done = set(DatasetManifest.model_validate_json(mp.read_text()).partitions)

    missing = []
    for i in range((args.end - args.start).days + 1):
        d = (args.start + timedelta(days=i)).isoformat()
        src = raw_root / f"date={d}" / "part-0.parquet"
        if not src.exists():
            missing.append(d)
            continue
        if d in done:
            continue
        report, written = ingest([src], mapping, dataset_id, store=store, expect_days={d},
                                 notes=f"NOAA MarineCadastre raw, region {region}")  # fmt: skip
        print(f"ok {d}: {report.rows_out:,}/{report.rows_in:,} rows "
              f"(dropped {sum(report.dropped.values())}, dup {report.duplicates_removed})",
              flush=True)  # fmt: skip
    if missing:
        print(f"missing raw days (run fetch_noaa.py): {missing}")
    print(f"manifest: {mp}")


if __name__ == "__main__":
    main()
