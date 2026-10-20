"""Build split manifests (P-known, P-unseen, P-paper) for a canonical dataset.

The rule comes from maritime_forecast.evaluation.splits; this script materializes the vessel
lists and counts that the rule produces on the dataset, and writes one JSON per protocol.

Output: data/manifests/splits/<protocol>__<dataset_id>.json   (committed; provenance record)

Usage:
    uv run --project backend python scripts/make_splits.py [--dataset-id noaa_us_gulf_2023-06]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import duckdb
import polars as pl

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR, region
from maritime_forecast.evaluation.splits import BUILDERS
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest


def vessel_spans(dataset_id: str) -> pl.DataFrame:
    """Per vessel and day: report count, and whether it appears in the Houston cutoff box."""
    glob = PROCESSED_DIR / "canonical" / dataset_id / "*" / "*.parquet"
    box = region("houston_galveston")
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con.execute(
        f"""SELECT vessel_id, date_trunc('day', observed_at) AS day, count(*) AS reports,
                   bool_or({box.sql_predicate()}) AS in_cutoff_region
            FROM read_parquet('{glob}') GROUP BY ALL"""
    ).pl()


def materialize(m: SplitManifest, spans: pl.DataFrame) -> SplitManifest:
    days = spans.with_columns(
        split=pl.struct("vessel_id", "day").map_elements(
            lambda r: m.split_of(r["vessel_id"], r["day"] if m.periods else None),
            return_dtype=pl.Utf8,
        )
    )
    kept = days.filter(pl.col("split").is_not_null())
    vessels = {
        s: sorted(g["vessel_id"].unique().to_list())
        for (s,), g in kept.sort("split").group_by("split", maintain_order=True)
        if m.vessel_assignment is not None  # lists are only meaningful when vessel-disjoint
    }
    counts = {
        "by_split": {
            s: {
                "vessels": g["vessel_id"].n_unique(),
                "vessels_in_cutoff_region": g.filter("in_cutoff_region")["vessel_id"].n_unique(),
                "reports": int(g["reports"].sum()),
            }
            for (s,), g in kept.sort("split").group_by("split", maintain_order=True)
        },
        "reports_excluded": int(days.filter(pl.col("split").is_null())["reports"].sum()),
    }
    return m.model_copy(
        update={
            "vessels": vessels,
            "counts": counts,
            "created_at": datetime.now(UTC).replace(microsecond=0),
            "git_sha": git_sha(),
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    args = ap.parse_args()

    spans = vessel_spans(args.dataset_id)
    out_dir = MANIFEST_DIR / "splits"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, build in BUILDERS.items():
        m = materialize(build(args.dataset_id), spans)
        # Validate the round trip before writing: what we write is what we will read.
        text = m.model_dump_json(indent=2)
        assert SplitManifest.model_validate_json(text) == m
        path = out_dir / f"{name}__{args.dataset_id}.json"
        path.write_text(text + "\n")
        print(f"{name}: {m.counts['by_split']} -> {path}")


if __name__ == "__main__":
    main()
