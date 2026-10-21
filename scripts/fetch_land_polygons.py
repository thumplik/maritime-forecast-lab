"""Download OSM land polygons and keep only those near the truth region (navigable-water mask).

Source: osmdata.openstreetmap.de "land-polygons-split-4326" (OSM coastline processed into
land polygons, split into ~1° tiles, WGS84). © OpenStreetMap contributors, ODbL 1.0.
The global zip (~0.9 GB) is downloaded resumably, hashed, read with pyshp, filtered to the
truth region's bounding box plus a margin, written as WKB Parquet, and then deleted.

Output:
    data/processed/geo/osm_land_<region>.parquet   (wkb, bbox columns; one row per polygon)
    data/manifests/osm_land_<region>.json          (URL, Last-Modified, size, SHA-256, filter)

Usage:
    uv run --project backend python scripts/fetch_land_polygons.py
    uv run --project backend python scripts/fetch_land_polygons.py --keep-zip

Re-running resumes a partial download; an existing output is replaced only with --force.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

import httpx
import polars as pl
import shapefile
from shapely.geometry import shape

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR, RAW_DIR, default_region
from maritime_forecast.geo.navigable_mask import land_polygons_path
from maritime_forecast.provenance import git_sha

URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"
SHP = "land-polygons-split-4326/land_polygons.shp"
FILTER_VERSION = "osm-land-bbox-v1"


def download(url: str, dest, chunk: int = 1 << 20) -> dict:
    """Resumable download; returns the server's Last-Modified and size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        head = c.head(url)
        head.raise_for_status()
        size = int(head.headers["content-length"])
        meta = {"last_modified": head.headers.get("last-modified"), "size": size}
        if dest.exists() and dest.stat().st_size == size:
            return meta
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with c.stream("GET", url, headers=headers) as r:
            r.raise_for_status()
            if have and r.status_code != 206:
                have = 0  # server ignored the range: start over
            with open(part, "ab" if have else "wb") as f:
                done, last = have, 0
                for b in r.iter_bytes(chunk):
                    f.write(b)
                    done += len(b)
                    if done - last >= 50 << 20:
                        print(f"  {done / 2**20:,.0f} / {size / 2**20:,.0f} MiB", flush=True)
                        last = done
    if part.stat().st_size != size:
        raise SystemExit(f"incomplete download: {part.stat().st_size} of {size} bytes; re-run")
    part.rename(dest)
    return meta


def sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--margin-deg", type=float, default=0.5, help="padding around the region")
    ap.add_argument("--keep-zip", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    box = default_region("truth_region")
    out = land_polygons_path(box.name)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists; pass --force to rebuild")
    zpath = RAW_DIR / "osm" / "land-polygons-split-4326.zip"
    print(f"downloading {URL}")
    meta = download(URL, zpath)
    digest = sha256(zpath)
    m = args.margin_deg
    bbox = [box.lon_min - m, box.lat_min - m, box.lon_max + m, box.lat_max + m]
    rows = []
    with shapefile.Reader(f"{zpath}/{SHP}") as sf:
        n_total = len(sf)
        for shp in sf.iterShapes(bbox=bbox):
            g = shape(shp.__geo_interface__)
            if g.is_empty:
                continue
            x0, y0, x1, y1 = g.bounds
            rows.append({"wkb": g.wkb, "lon_min": x0, "lat_min": y0, "lon_max": x1, "lat_max": y1})
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(out)
    manifest = {
        "dataset": f"osm_land_{box.name}",
        "source_url": URL,
        "license": "ODbL 1.0, © OpenStreetMap contributors",
        "last_modified": meta["last_modified"],
        "size_bytes": meta["size"],
        "sha256": digest,
        "filter": {"version": FILTER_VERSION, "region": box.name, "bbox_lon_lat": bbox},
        "polygons_in_source": n_total,
        "polygons_kept": len(rows),
        "output": str(out.relative_to(PROCESSED_DIR.parent.parent)),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
    }
    mpath = MANIFEST_DIR / f"osm_land_{box.name}.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"kept {len(rows):,} of {n_total:,} polygons -> {out}; manifest {mpath}")
    if not args.keep_zip:
        zpath.unlink()


if __name__ == "__main__":
    main()
