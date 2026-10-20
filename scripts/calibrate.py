"""Evaluator calibration against the calibration paper's golden numbers (build plan §6.6).

Reconstructs the P-paper protocol from its description (the released manifests have not been
obtained) and runs constant velocity on it. Every reconstruction choice that the paper's text
leaves open is a named variant; all variants are reported, and one is declared *primary* in
advance (PRIMARY below) so the comparison is not chosen after seeing the numbers.

Protocol as described: NOAA Houston–Galveston, June 2023; Class A/B and coordinate filter;
stationary jitter removal; 30-min gap segmentation; 10-min grid; 18 context + 18 target steps;
test windows spaced 3 h apart; vessel-disjoint split.

Output: data/runs/calibration_<UTC>/{summary.json, report.md}

Usage:
    uv run --project backend python scripts/calibrate.py [--dataset-id noaa_us_gulf_2023-06]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

import duckdb
import numpy as np

from maritime_forecast.config import PROCESSED_DIR, RUNS_DIR, region
from maritime_forecast.data.segment import SegmentConfig, segmented_sql
from maritime_forecast.evaluation.bootstrap import vessel_bootstrap_ci
from maritime_forecast.evaluation.windows import grid_sql, make_windows
from maritime_forecast.geo.geodesic import distance_m
from maritime_forecast.models.baselines import constant_velocity, velocity_from_last_two
from maritime_forecast.provenance import git_sha

GOLDEN_CV_KM = {1: 3.69, 2: 9.03, 3: 15.33}
GOLDEN_TRAISFORMER_KM = {1: 2.20, 2: 5.07, 3: 8.44}
TOLERANCE = 0.10
STEP_MIN, N_CTX, N_TGT, STRIDE = 10, 18, 18, 18
TEST_FRACTION = 0.2

PRIMARY = {
    "jitter": "drop_stationary",
    "cv_mode": "anchor_report",
    "vessels": "test_split",
    "stat": "mean",
}
STATIONARY_SOG_KN = 0.5


def is_test_vessel(mmsi: str) -> bool:
    """Stable vessel-disjoint assignment by MMSI hash."""
    return int(hashlib.sha256(mmsi.encode()).hexdigest(), 16) % 1000 < TEST_FRACTION * 1000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    ap.add_argument("--region", default="houston_galveston")
    ap.add_argument("--n-boot", type=int, default=1000)
    args = ap.parse_args()

    box = region(args.region)
    canon = f"read_parquet('{PROCESSED_DIR / 'canonical' / args.dataset_id / '*' / '*.parquet'}')"
    run_dir = RUNS_DIR / f"calibration_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    run_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET memory_limit='8GB'")

    base_filter = f"{box.sql_predicate()} AND transceiver_class IN ('A', 'B')"
    jitter_filters = {
        "none": "TRUE",
        "drop_stationary": f"coalesce(sog_knots, 0) >= {STATIONARY_SOG_KN}",
    }
    hz_steps = {h: h * 60 // STEP_MIN for h in GOLDEN_CV_KM}
    results = []
    counts = {}
    for jitter, jf in jitter_filters.items():
        print(f"jitter={jitter}: segmenting + gridding ...", flush=True)
        con.execute(
            f"CREATE OR REPLACE TABLE obs AS SELECT * FROM {canon} WHERE {base_filter} AND {jf}"
        )
        con.execute(
            "CREATE OR REPLACE TABLE seg AS "
            f"SELECT * FROM ({segmented_sql('obs', SegmentConfig(gap_minutes=30))}) "
            "ORDER BY vessel_id, segment_no, observed_at"
        )
        grid = con.execute(grid_sql("seg", STEP_MIN)).pl()
        w = make_windows(grid, N_CTX, N_TGT, STRIDE)
        test_mask = np.array([is_test_vessel(v) for v in w.vessel_id])
        counts[jitter] = {
            "reports": con.execute("SELECT count(*) FROM obs").fetchone()[0],
            "windows_all": int(len(w.vessel_id)),
            "windows_test": int(test_mask.sum()),
            "vessels_all": int(len(set(w.vessel_id))),
            "vessels_test": int(len(set(w.vessel_id[test_mask]))),
        }
        print(f"  {counts[jitter]}", flush=True)

        lat_a, lon_a = w.lat[:, N_CTX - 1], w.lon[:, N_CTX - 1]
        leads = np.array([k * STEP_MIN * 60.0 for k in range(1, N_TGT + 1)])
        sog2, cog2 = velocity_from_last_two(w.lat[:, :N_CTX], w.lon[:, :N_CTX], STEP_MIN * 60.0)
        preds = {
            "anchor_report": constant_velocity(lat_a, lon_a, w.anchor_sog, w.anchor_cog, leads),
            "last_two_grid": constant_velocity(lat_a, lon_a, sog2, cog2, leads),
        }
        for cv_mode, (plat, plon) in preds.items():
            err_km = distance_m(plat, plon, w.lat[:, N_CTX:], w.lon[:, N_CTX:]) / 1000
            for vessels, mask in [("test_split", test_mask), ("all", np.ones_like(test_mask))]:
                for stat, fn in [("mean", np.nanmean), ("median", np.nanmedian)]:
                    row = {
                        "jitter": jitter,
                        "cv_mode": cv_mode,
                        "vessels": vessels,
                        "stat": stat,
                        "n_windows": int(mask.sum()),
                        "n_failed": int(np.isnan(err_km[mask, 0]).sum()),
                    }
                    for h, k in hz_steps.items():
                        est, lo, hi = vessel_bootstrap_ci(
                            err_km[mask, k - 1], w.vessel_id[mask], fn, n_boot=args.n_boot
                        )
                        row[f"{h}h"] = [est, lo, hi]
                        row[f"{h}h_rel_err"] = est / GOLDEN_CV_KM[h] - 1
                    row["within_tolerance"] = all(
                        abs(row[f"{h}h_rel_err"]) <= TOLERANCE for h in GOLDEN_CV_KM
                    )
                    row["primary"] = all(row[k] == v for k, v in PRIMARY.items())
                    results.append(row)

    primary = next(r for r in results if r["primary"])
    summary = {
        "dataset_id": args.dataset_id,
        "region": box.as_dict(),
        "protocol": {
            "step_min": STEP_MIN,
            "n_ctx": N_CTX,
            "n_tgt": N_TGT,
            "stride": STRIDE,
            "gap_minutes": 30,
            "test_fraction": TEST_FRACTION,
            "stationary_sog_kn": STATIONARY_SOG_KN,
            "transceiver": ["A", "B"],
        },
        "golden_cv_km": GOLDEN_CV_KM,
        "tolerance": TOLERANCE,
        "primary_variant": PRIMARY,
        "primary_result": primary,
        "passed": primary["within_tolerance"],
        "counts": counts,
        "variants": results,
        "git_sha": git_sha(),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (run_dir / "report.md").write_text(render(summary, run_dir.name))
    print(f"primary within tolerance: {summary['passed']}\nrun: {run_dir}")


def render(s: dict, run_id: str) -> str:
    L = [
        f"# Calibration run `{run_id}`",
        "",
        "Golden CV (km): " + ", ".join(f"{h} h = {v}" for h, v in s["golden_cv_km"].items()),
        f"Tolerance ±{100 * s['tolerance']:.0f}%. Primary variant: `{s['primary_variant']}` → "
        f"**{'PASS' if s['passed'] else 'FAIL'}**",
        "",
        "## Sample counts",
        "",
        "| jitter | reports | windows (all) | windows (test) | vessels (all) | vessels (test) |",
        "|---|---|---|---|---|---|",
    ]
    for j, c in s["counts"].items():
        L.append(
            f"| {j} | {c['reports']:,} | {c['windows_all']:,} | {c['windows_test']:,} | "
            f"{c['vessels_all']} | {c['vessels_test']} |"
        )
    L += [
        "",
        "## All variants (km, 95% vessel-bootstrap CI; relative error vs golden)",
        "",
        "| jitter | CV mode | vessels | stat | n | 1 h | 2 h | 3 h | within ±10% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def cell(r: dict, h: str) -> str:
        return f"{r[h][0]:.2f} [{r[h][1]:.2f}, {r[h][2]:.2f}] ({100 * r[h + '_rel_err']:+.0f}%)"

    for r in s["variants"]:
        mark = " **(primary)**" if r["primary"] else ""
        L.append(
            f"| {r['jitter']} | {r['cv_mode']} | {r['vessels']} | {r['stat']}{mark} | "
            f"{r['n_windows']:,} | {cell(r, '1h')} | {cell(r, '2h')} | {cell(r, '3h')} | "
            f"{'yes' if r['within_tolerance'] else 'no'} |"
        )
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
