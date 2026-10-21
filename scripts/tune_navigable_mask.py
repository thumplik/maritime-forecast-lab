"""Choose the navigable mask's traffic parameters on a protocol's CALIBRATION split.

For each (min_vessels, dilate_cells), the mask is fitted on the TRAIN split exactly as
build_navigable_mask.py does. It is then scored on real reports from the calibration split
(its period, and under P-unseen its calibration-assigned vessels) inside the cutoff region:
the share it calls infeasible (lower is better: vessels are not on land), against the share
of OSM land it still flags (higher is better). Test data is never read.

Output: data/runs/mask_tuning/<protocol>__<dataset>.json (table, config, seed, git SHA).

Usage:
    uv run --project backend python scripts/tune_navigable_mask.py --protocol P-known
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

from maritime_forecast.config import MANIFEST_DIR, RUNS_DIR, default_region
from maritime_forecast.geo.navigable_mask import (
    NavigableMask,
    infeasible_report_rates,
    land_source_id,
    load_land_raster,
    traffic_cells_sql,
    traffic_raster,
    traffic_source_sql,
)
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", required=True, choices=["P-known", "P-unseen"])
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    ap.add_argument("--n-sample", type=int, default=500_000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    raw = (MANIFEST_DIR / "splits" / f"{args.protocol}__{args.dataset_id}.json").read_bytes()
    manifest = SplitManifest.model_validate_json(raw)
    # splits-v2 reserves "calibration" for conformal radii: tune on the dev window there.
    tune_split = "dev" if any(p.split == "dev" for p in manifest.periods or []) else "calibration"
    store = ParquetStore()
    box = default_region("truth_region")
    grid, land = load_land_raster(box)
    src, cover = traffic_source_sql(store, args.dataset_id, manifest, "train")
    cells = store.con.execute(traffic_cells_sql(src, grid)).pl()
    cutoff_box = default_region("cutoff_region")
    rows = []
    for mv in (1, 2, 3):
        for dl in (0, 1, 2):
            m = NavigableMask.build(grid, land, traffic_raster(cells, grid, mv, dl), "tuning")
            r = infeasible_report_rates(
                store, args.dataset_id, manifest, m, cutoff_box, tune_split,
                args.n_sample, args.seed,
            )  # fmt: skip
            kept = float(m.infeasible.sum() / land.sum())
            rows.append({"min_vessels": mv, "dilate_cells": dl, **r, "land_still_flagged": kept})
            print(f"min_vessels={mv} dilate={dl}: stationary {r['stationary']:.2%} "
                  f"underway {r['underway']:.2%} land kept {kept:.1%}")  # fmt: skip
    land_only = infeasible_report_rates(
        store, args.dataset_id, manifest, NavigableMask.build(grid, land, None, "land"),
        cutoff_box, tune_split, args.n_sample, args.seed,
    )  # fmt: skip
    print(f"OSM land only: stationary {land_only['stationary']:.2%} "
          f"underway {land_only['underway']:.2%}")  # fmt: skip
    out = RUNS_DIR / "mask_tuning" / f"{args.protocol}__{args.dataset_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "protocol": args.protocol, "dataset_id": args.dataset_id,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(), "fit_on": cover,
        "scored_on": {"split": tune_split, "region": cutoff_box.name},
        "land_source": land_source_id(box.name), "n_sample": args.n_sample, "seed": args.seed,
        "land_only": land_only, "table": rows, "git_sha": git_sha(),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=2) + "\n")  # fmt: skip
    print(f"-> {out}")


if __name__ == "__main__":
    main()
