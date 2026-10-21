"""Build the retrieval segment index for a dataset (build plan §5 R1; Milestone 3 slice 1).

One row per completed track segment, with its regular track. Protocol eligibility is applied
per cutoff at query time (maritime_forecast.data.segment_index.eligible), so one index serves
every protocol.

Output:
    data/processed/segment_index/<dataset>/segments.parquet
    data/manifests/segment_index/<dataset>.json   (config, dataset manifest identity, counts)

Usage:
    uv run --project backend python scripts/build_segment_index.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.data.segment_index import (
    IndexConfig,
    build_segment_index,
    index_path,
    index_sidecar_path,
)
from maritime_forecast.provenance import git_sha
from maritime_forecast.storage.parquet_store import ParquetStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    args = ap.parse_args()

    cfg = IndexConfig()
    t0 = time.time()
    ix = build_segment_index(ParquetStore(), args.dataset_id, cfg)
    out = index_path(args.dataset_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    ix.write_parquet(out)
    ds_manifest = MANIFEST_DIR / f"{args.dataset_id}.canonical.json"
    side = {
        "dataset_id": args.dataset_id,
        "dataset_manifest_sha256": hashlib.sha256(ds_manifest.read_bytes()).hexdigest()
        if ds_manifest.exists() else None,
        "config": cfg.as_dict(),
        "segments": ix.height,
        "vessels": ix["vessel_id"].n_unique(),
        "track_points": int(ix["track_t"].list.len().sum()),
        "first_start": ix["start_t"].min().isoformat(),
        "last_end": ix["end_t"].max().isoformat(),
        "index_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "seconds": round(time.time() - t0, 1),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
    }  # fmt: skip
    sp = index_sidecar_path(args.dataset_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(side, indent=2) + "\n")
    print(f"{ix.height:,} segments, {side['track_points']:,} track points, "
          f"{out.stat().st_size / 1e6:.0f} MB, {side['seconds']} s -> {out}")  # fmt: skip


if __name__ == "__main__":
    main()
