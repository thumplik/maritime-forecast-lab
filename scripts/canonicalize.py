"""Convert raw NOAA Parquet partitions into canonical §4 observations.

Input:  data/raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet   (from fetch_noaa.py)
Output: data/processed/canonical/<dataset_id>/date=YYYY-MM-DD/part-0.parquet
        data/manifests/<dataset_id>.canonical.json

Usage:
    uv run --project backend python scripts/canonicalize.py --start 2023-06-01 --end 2023-06-30
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta

import duckdb

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR, RAW_DIR, default_region
from maritime_forecast.data.canonical import CANONICAL_VERSION, noaa_to_canonical_sql
from maritime_forecast.provenance import git_sha


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 6, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2023, 6, 30))
    ap.add_argument("--region", default=None)
    ap.add_argument("--dataset-id", default=None)
    ap.add_argument("--force", action="store_true", help="rebuild existing partitions")
    args = ap.parse_args()

    region = args.region or default_region("truth_region").name
    dataset_id = args.dataset_id or f"noaa_{region}_{args.start:%Y-%m}"
    raw_root = RAW_DIR / f"noaa_{region}"
    out_root = PROCESSED_DIR / "canonical" / dataset_id

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    stats: dict[str, dict] = {}
    missing: list[str] = []
    for d in days:
        src = raw_root / f"date={d.isoformat()}" / "part-0.parquet"
        dst_dir = out_root / f"date={d.isoformat()}"
        dst = dst_dir / "part-0.parquet"
        if not src.exists():
            missing.append(d.isoformat())
            continue
        if dst.exists() and not args.force:
            n = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
            stats[d.isoformat()] = {"rows_out": int(n), "skipped": True}
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        rows_in = con.execute(f"SELECT count(*) FROM read_parquet('{src}')").fetchone()[0]
        tmp = dst.with_suffix(".parquet.tmp")
        sql = noaa_to_canonical_sql(f"read_parquet('{src}')")
        con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)")
        tmp.replace(dst)
        rows_out = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
        stats[d.isoformat()] = {"rows_in": int(rows_in), "rows_out": int(rows_out)}
        print(f"ok {d}: {rows_out:,}/{rows_in:,} rows", flush=True)

    manifest = {
        "dataset_id": dataset_id,
        "canonical_version": CANONICAL_VERSION,
        "source_raw": str(raw_root.relative_to(RAW_DIR.parent.parent)),
        "days": stats,
        "missing_days": missing,
        "git_sha": git_sha(),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = MANIFEST_DIR / f"{dataset_id}.canonical.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"{len(stats)} days canonical, {len(missing)} missing -> {out_root}\nmanifest: {path}")


if __name__ == "__main__":
    main()
