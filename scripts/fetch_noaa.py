"""Download NOAA MarineCadastre daily AIS files and keep only the truth region.

Each national daily file (~340 MB zipped, ~1.5 GB CSV) is downloaded, hashed,
unzipped to a temp dir, filtered by bounding box with DuckDB into a per-day
Parquet partition, and then deleted. Only the filtered Parquet is kept.

Output:
    data/raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet   (raw NOAA columns, typed)
    data/manifests/noaa_<region>_<start>_<end>.json        (files, checksums, filter, counts)

Usage:
    uv run --project backend python scripts/fetch_noaa.py --start 2023-06-01 --end 2023-06-30
    uv run --project backend python scripts/fetch_noaa.py --list-only   # print URLs, no download

Re-running is resumable: days whose Parquet partition and manifest entry exist are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock

import duckdb
import httpx

from maritime_forecast.config import MANIFEST_DIR, RAW_DIR, BBox, default_region, region

BASE_URL = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler/{year}/AIS_{year}_{mm}_{dd}.zip"
FILTER_VERSION = "noaa-bbox-v2"  # v2: skip+count malformed CSV lines

# NOAA CSV columns, typed. TRY_CAST so a malformed value becomes NULL instead of aborting.
SELECT_TYPED = """
    TRY_CAST(MMSI AS VARCHAR)            AS MMSI,
    TRY_CAST(BaseDateTime AS TIMESTAMP)  AS BaseDateTime,
    TRY_CAST(LAT AS DOUBLE)              AS LAT,
    TRY_CAST(LON AS DOUBLE)              AS LON,
    TRY_CAST(SOG AS DOUBLE)              AS SOG,
    TRY_CAST(COG AS DOUBLE)              AS COG,
    TRY_CAST(Heading AS DOUBLE)          AS Heading,
    VesselName, IMO, CallSign,
    TRY_CAST(VesselType AS INTEGER)      AS VesselType,
    TRY_CAST(Status AS INTEGER)          AS Status,
    TRY_CAST(Length AS DOUBLE)           AS Length,
    TRY_CAST(Width AS DOUBLE)            AS Width,
    TRY_CAST(Draft AS DOUBLE)            AS Draft,
    TRY_CAST(Cargo AS INTEGER)           AS Cargo,
    TransceiverClass
"""


def day_url(d: date) -> str:
    return BASE_URL.format(year=d.year, mm=f"{d.month:02d}", dd=f"{d.day:02d}")


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def download(url: str, dest: Path, retries: int = 4) -> tuple[int, str]:
    """Stream url to dest; return (bytes, sha256)."""
    for attempt in range(1, retries + 1):
        try:
            h = hashlib.sha256()
            n = 0
            with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        n += len(chunk)
            return n, h.hexdigest()
        except (httpx.HTTPError, OSError) as e:
            if attempt == retries:
                raise
            print(f"  retry {attempt} for {url}: {e}", file=sys.stderr)
            time.sleep(5 * attempt)
    raise RuntimeError("unreachable")


def process_day(d: date, bbox: BBox, out_root: Path, keep_zip: bool) -> dict:
    url = day_url(d)
    part_dir = out_root / f"date={d.isoformat()}"
    out_file = part_dir / "part-0.parquet"
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="noaa_", dir=RAW_DIR) as tmp:
        tmpd = Path(tmp)
        zpath = tmpd / Path(url).name
        nbytes, sha = download(url, zpath)
        with zipfile.ZipFile(zpath) as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
            if len(members) != 1:
                raise RuntimeError(f"{url}: expected one CSV, got {members}")
            csv_name = members[0]
            zf.extract(csv_name, tmpd)
        csv_path = tmpd / csv_name
        if keep_zip:
            shutil.copy2(zpath, RAW_DIR / zpath.name)
        zpath.unlink()

        part_dir.mkdir(parents=True, exist_ok=True)
        tmp_out = part_dir / "part-0.parquet.tmp"
        con = duckdb.connect()
        con.execute("SET threads TO 2")
        # Malformed lines (e.g. stray quotes in vessel names) are skipped and counted, never
        # silently: the count goes into the manifest.
        con.execute(
            f"CREATE TABLE src AS SELECT * FROM read_csv('{csv_path}', all_varchar=true, "
            "header=true, ignore_errors=true, store_rejects=true)"
        )
        src = "src"
        rows_in = con.execute("SELECT count(*) FROM src").fetchone()[0]
        rows_rejected = con.execute("SELECT count(*) FROM reject_errors").fetchone()[0]
        con.execute(
            f"""
            COPY (
                SELECT {SELECT_TYPED} FROM {src}
                WHERE TRY_CAST(LAT AS DOUBLE) BETWEEN {bbox.lat_min} AND {bbox.lat_max}
                  AND TRY_CAST(LON AS DOUBLE) BETWEEN {bbox.lon_min} AND {bbox.lon_max}
                ORDER BY MMSI, BaseDateTime
            ) TO '{tmp_out}' (FORMAT parquet, COMPRESSION zstd)
            """
        )
        rows_kept = con.execute(f"SELECT count(*) FROM read_parquet('{tmp_out}')").fetchone()[0]
        con.close()
        tmp_out.replace(out_file)

    return {
        "date": d.isoformat(),
        "url": url,
        "source_file": Path(url).name,
        "csv_member": csv_name,
        "zip_bytes": nbytes,
        "zip_sha256": sha,
        "rows_in": int(rows_in),
        "rows_rejected_malformed": int(rows_rejected),
        "rows_kept": int(rows_kept),
        "output": str(out_file.relative_to(RAW_DIR.parent.parent)),
        "seconds": round(time.time() - t0, 1),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 6, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2023, 6, 30))
    ap.add_argument("--region", default=None, help="region name in config/regions.yaml")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument(
        "--keep-zip", action="store_true", help="also keep the national zip in data/raw"
    )
    ap.add_argument("--list-only", action="store_true", help="print the URLs and exit")
    args = ap.parse_args()

    bbox = region(args.region) if args.region else default_region("truth_region")
    days = daterange(args.start, args.end)
    if args.list_only:
        for d in days:
            print(day_url(d))
        return

    out_root = RAW_DIR / f"noaa_{bbox.name}"
    out_root.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"noaa_{bbox.name}_{args.start}_{args.end}.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "dataset_id": f"noaa_{bbox.name}_{args.start}_{args.end}",
            "source": "NOAA MarineCadastre AccessAIS national daily point files",
            "source_metadata": "https://www.fisheries.noaa.gov/inport/item/77594",
            "filter_version": FILTER_VERSION,
            "filter": {"bbox": bbox.as_dict(), "time": [str(args.start), str(args.end)]},
            "notes": "Static fields (name, IMO, type, dimensions) are back-filled by NOAA from "
            "the AIS Vessel Identification Database; they are not strictly as-of data.",
            "files": {},
        }
    )
    lock = Lock()

    def save() -> None:
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        tmp.replace(manifest_path)

    todo = [
        d
        for d in days
        if not (
            d.isoformat() in manifest["files"]
            and (out_root / f"date={d.isoformat()}" / "part-0.parquet").exists()
        )
    ]
    print(f"{len(days) - len(todo)} days already done, {len(todo)} to fetch -> {out_root}")

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_day, d, bbox, out_root, args.keep_zip): d for d in todo}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001 — report and continue other days
                failures.append(f"{d}: {e}")
                print(f"FAIL {d}: {e}", file=sys.stderr)
                continue
            with lock:
                manifest["files"][rec["date"]] = rec
                save()
            print(
                f"ok {rec['date']}: kept {rec['rows_kept']:,}/{rec['rows_in']:,} rows "
                f"in {rec['seconds']}s",
                flush=True,
            )

    print(f"manifest: {manifest_path}")
    if failures:
        print("Failed days (re-run to retry, or download manually):", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
