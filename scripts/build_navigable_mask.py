"""Fit the navigable-water mask for a protocol (OSM land minus TRAIN-split traffic cells).

Needs the land polygons (scripts/fetch_land_polygons.py) and the canonical dataset. Traffic
comes only from the protocol's train split (and, under P-unseen, train-assigned vessels).

Output:
    data/processed/geo/navigable_<protocol>__<dataset>.npz     (bit-packed raster)
    data/manifests/masks/navigable_<protocol>__<dataset>.json  (sidecar, committed)

Usage:
    uv run --project backend python scripts/build_navigable_mask.py --protocol P-known
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime

from maritime_forecast.config import MANIFEST_DIR, default_region
from maritime_forecast.geo.navigable_mask import (
    TrafficConfig,
    fit_navigable_mask,
    mask_path,
    mask_sidecar_path,
)
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", required=True, choices=["P-known", "P-unseen"])
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    args = ap.parse_args()

    mpath = MANIFEST_DIR / "splits" / f"{args.protocol}__{args.dataset_id}.json"
    raw = mpath.read_bytes()
    manifest = SplitManifest.model_validate_json(raw)
    t0 = time.time()
    mask, side = fit_navigable_mask(
        ParquetStore(), args.dataset_id, manifest, hashlib.sha256(raw).hexdigest(),
        default_region("truth_region"), TrafficConfig(),
    )  # fmt: skip
    out = mask_path(args.protocol, args.dataset_id)
    side["mask_sha256"] = mask.save(out)
    side["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    side["git_sha"] = git_sha()
    sp = mask_sidecar_path(args.protocol, args.dataset_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(side, indent=2) + "\n")
    print(f"{out} ({out.stat().st_size / 1e6:.1f} MB), sidecar {sp}, {time.time() - t0:.0f} s")
    print(f"infeasible cells {side['infeasible_cells']:,} of {side['land_cells']:,} land cells")


if __name__ == "__main__":
    main()
