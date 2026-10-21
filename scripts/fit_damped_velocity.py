"""Fit damped-velocity tau per vessel class on a protocol's TRAIN split.

Writes data/model_params/damped_velocity_v1/<protocol>__<dataset>.json (committed: it is the
provenance of every benchmark that uses damped velocity). The benchmark only accepts a fit
whose manifest hash matches the manifest it evaluates.

Usage:
    uv run --project backend python scripts/fit_damped_velocity.py --protocol P-known
"""

from __future__ import annotations

import argparse
import time

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.data.ingest import manifest_path
from maritime_forecast.models.fit_damped import fit_damped_velocity, save_fit
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.storage.parquet_store import ParquetStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", required=True, choices=["P-known", "P-unseen", "P-region"])
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    args = ap.parse_args()

    mp = MANIFEST_DIR / "splits" / f"{args.protocol}__{args.dataset_id}.json"
    dm = manifest_path(args.dataset_id)
    prov = DatasetManifest.model_validate_json(dm.read_text()).mapping_id if dm.exists() else ""
    t0 = time.time()
    store = ParquetStore()
    store.con.execute("SET enable_progress_bar=false")
    fit = fit_damped_velocity(store, args.dataset_id, mp, data_provenance=prov)
    path = save_fit(fit)
    print(
        f"fit {args.protocol}: taus_hours={fit.taus_hours} n={fit.n_cutoffs} "
        f"({time.time() - t0:.0f} s) -> {path}"
    )


if __name__ == "__main__":
    main()
