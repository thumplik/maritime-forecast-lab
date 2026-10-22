"""Milestone 7: fit H1 hex sets on the calibration window; optionally score them ONCE on the
held-out test run against the disc baseline (build plan §5 H, §6.7).

The design is frozen before this runs: `data/model_params/hexsets/h1_design.json` (spread
factor c per horizon chosen on the dev window, eps, K, resolution rule). This script fits only
the conformal parts on the calibration-split run (per stratum underway:ALL /
underway:TRANSITING and horizon: the disc radius around retrieval v2's scored point, the broad
scale, the density threshold) and writes them to
`data/model_params/hexsets/<protocol>__<dataset>.json` (served by the API).

Without --params-only it then scores the held-out test run: the analysis is appended to the
test-access ledger BEFORE any test record is read, the tree must be clean, and the adoption
rule pre-registered in docs/implementation-log.md is applied as written.

    uv run --project backend python scripts/hexsets_holdout.py --protocol P-known --params-only
    uv run --project backend python scripts/hexsets_holdout.py --protocol P-known
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from maritime_forecast.config import DATA_DIR, RUNS_DIR
from maritime_forecast.evaluation import hexsets as hx
from maritime_forecast.evaluation.holdout import config_sha, read_ledger, record_analysis
from maritime_forecast.geo.geodesic import LocalFrame
from maritime_forecast.models.base import ForecastHorizon, ModelContext, histories_for_cutoffs
from maritime_forecast.models.route_retrieval import V2, RouteRetrieval
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore

PARAMS_DIR = DATA_DIR / "model_params" / "hexsets"
STRATA = {"underway:ALL": pl.lit(True), "underway:TRANSITING": pl.col("transiting")}
TOLERANCE, TOLERANCE_MIN_N = 0.03, 200  # pre-registered (implementation log, 2026-10-22)
N_BOOT = 500


def latest(protocol: str, dataset_id: str, split: str) -> str:
    runs = RunStore()  # headers only here; the chosen run is loaded (checksums verified) later
    ids = [r for r in runs.runs() if r.startswith(f"{protocol}_{dataset_id}_")]
    ids = [
        r
        for r in ids
        if json.loads((runs.path(r) / "run.json").read_text())["sampler"].get("split") == split
    ]
    if not ids:
        raise SystemExit(f"no {split} run for {protocol} on {dataset_id}")
    return sorted(ids)[-1]


def underway_v2(records: pl.DataFrame, cutoffs: pl.DataFrame) -> pl.DataFrame:
    v2 = records.filter(
        (pl.col("model_id") == "route_retrieval_v2")
        & pl.col("paired")
        & (pl.col("motion_set") == "underway")
        & (pl.col("status") == "ok")
    )
    return v2.join(
        cutoffs.select("cutoff_id", pl.col("observed_at").alias("as_of")), on="cutoff_id"
    ).with_columns(
        target_time=pl.col("as_of") + pl.duration(minutes=(pl.col("horizon_h") * 60).cast(int))
    )


def rerun_candidates(run, k: int, cutoffs: pl.DataFrame):
    """Retrieval v2 with K candidates, exactly as the run predicted (pool, history, horizon)."""
    store = ParquetStore()
    ctx = ModelContext(
        store,
        run.dataset_id,
        run.protocol.protocol,
        run.protocol.manifest_sha256,
        params={"config": {"k": k}},
    )
    model = RouteRetrieval.from_context(ctx, V2)
    if model.unavailable_reason:
        raise SystemExit(model.unavailable_reason)
    s = run.sampler
    out, failed = {}, {}
    keys = cutoffs.select("cutoff_id", "vessel_id", "observed_at")
    for cid, h in histories_for_cutoffs(store, run.dataset_id, keys, s["history_hours"]):
        f = model.predict(h, ForecastHorizon(h.as_of, max(s["horizons_h"]), s["step_minutes"]))
        if f.status != "ok" or not f.trajectories:
            failed[cid] = f.error.code if f.error else "no_candidates"
            continue
        tt = f.target_times
        out[cid] = (
            tt,
            np.array([[p.latitude_deg for p in t.points] for t in f.trajectories]),
            np.array([[p.longitude_deg for p in t.points] for t in f.trajectories]),
            np.array(
                [
                    [t.reference_ends_at is None or x <= t.reference_ends_at for x in tt]
                    for t in f.trajectories
                ]
            ),
            np.array([t.similarity or 0.0 for t in f.trajectories]),
        )
    return out, failed, model


def mixture(row, cand, c, broad):
    frame = LocalFrame(row["pred_lat"], row["pred_lon"])
    if cand is None:
        return frame, hx.h1_mixture(frame, np.zeros(0), np.zeros(0), np.zeros(0), c, broad)
    tt, lat, lon, cov, w = cand
    j = tt.index(row["target_time"])
    use = cov[:, j]
    return frame, hx.h1_mixture(frame, lat[use, j], lon[use, j], w[use], c, broad)


def fit(design: dict, cal_run, level: float, k: int) -> tuple[dict, dict]:
    runs = RunStore()
    rows = underway_v2(runs.table(cal_run.run_id, "records"), runs.table(cal_run.run_id, "cutoffs"))
    cutoffs = runs.table(cal_run.run_id, "cutoffs").filter(
        pl.col("cutoff_id").is_in(rows["cutoff_id"].unique().implode())
    )
    print(f"calibration: retrieval v2 k={k} on {cutoffs.height} underway cutoffs ...")
    cand, failed, model = rerun_candidates(cal_run, k, cutoffs)
    cells = []
    for st, cond in STRATA.items():
        for hz in sorted(rows["horizon_h"].unique().to_list()):
            key = f"{hz:g}"
            if key not in design["c_by_horizon"]:
                continue
            res, c = hx.resolution_for(hz), design["c_by_horizon"][key]
            g = rows.filter(cond & (pl.col("horizon_h") == hz)).to_dicts()
            d = np.array(
                [
                    hx.disc_score(
                        LocalFrame(r["pred_lat"], r["pred_lon"]),
                        r["truth_lat"],
                        r["truth_lon"],
                        res,
                    )
                    for r in g
                ]
            )
            radius = hx.conformal_quantile(d, level)
            if radius is None:
                continue
            broad = max(radius / 2, 1000.0)
            sc = []
            for r in g:
                fr, mix = mixture(r, cand.get(r["cutoff_id"]), c, broad)
                sc.append(hx.density_score(mix, fr, r["truth_lat"], r["truth_lon"], res))
            tau = hx.density_threshold(np.array(sc), level)
            if tau is None:
                continue
            cells.append(
                {
                    "stratum": st,
                    "horizon_h": hz,
                    "level": level,
                    "res": res,
                    "c": c,
                    "n": len(g),
                    "n_vessels": len({r["vessel_id"] for r in g}),
                    "disc_radius_m": radius,
                    "broad_m": broad,
                    "tau": tau,
                }
            )
            print(f"  {st} {hz:g} h: n={len(g)}, c={c}, disc r={radius / 1000:.1f} km")
    return cells, {
        "failed": len(failed),
        "codes": dict(Counter(failed.values())),
        "model_config": model.config,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--protocol", default="P-known")
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06-07")
    ap.add_argument("--calibration-run")
    ap.add_argument("--test-run")
    ap.add_argument("--level", type=float, default=0.9)
    ap.add_argument(
        "--params-only",
        action="store_true",
        help="fit and write the served parameters; read no test records",
    )
    args = ap.parse_args()
    sha = git_sha()
    if sha.endswith("-dirty") or sha == "unknown":
        raise SystemExit("run from a clean, committed tree (outputs are stamped with its SHA)")
    design_path = PARAMS_DIR / "h1_design.json"
    design = json.loads(design_path.read_text())
    runs = RunStore()
    cal_id = args.calibration_run or latest(args.protocol, args.dataset_id, "calibration")
    cal_run = runs.load(cal_id)
    if cal_run.sampler.get("split") != "calibration":
        raise SystemExit(f"{cal_id} is not a calibration-split run")
    man = SplitManifest.model_validate_json(Path(cal_run.sampler["manifest_path"]).read_text())
    cells, info = fit(design, cal_run, args.level, design["k"])
    s = cal_run.sampler
    params = {
        "kind": "h1_hex_sets",
        "protocol": args.protocol,
        "dataset_id": args.dataset_id,
        "calibration_set_id": cal_id,
        "manifest_sha256": cal_run.protocol.manifest_sha256,
        "design": design,
        "design_sha256": hashlib.sha256(design_path.read_bytes()).hexdigest(),
        "request": {
            "horizon_h": max(s["horizons_h"]),
            "step_minutes": s["step_minutes"],
            "history_hours": s["history_hours"],
        },
        "retrieval": {
            "model_id": "route_retrieval_v2",
            "model_version": V2["model_version"],
            "config_sha256": config_sha(info["model_config"]),
            "k": design["k"],
        },
        "label_reach_end": (
            max(p.end for p in man.periods if p.split == "calibration")
            + timedelta(minutes=s["max_bracket_gap_minutes"])
        ).isoformat(),
        "retrieval_failures": {"n": info["failed"], "codes": info["codes"]},
        "class_source": "backfilled (NOAA; not as-of)",
        "cells": cells,
        "git_sha": sha,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    out_params = PARAMS_DIR / f"{args.protocol}__{args.dataset_id}.json"
    out_params.write_text(json.dumps(params, indent=1, default=str) + "\n")
    print(f"params -> {out_params}")
    if args.params_only:
        print("params only: no test records read")
        return
    evaluate(args, params, sha)


def evaluate(args, params: dict, sha: str) -> None:
    runs = RunStore()
    ledger = [
        e
        for e in read_ledger(args.dataset_id)
        if e["protocol"] == args.protocol and e.get("kind") is None
    ]
    test_id = args.test_run or (ledger[-1]["run_id"] if ledger else None)
    if test_id is None or test_id not in {e["run_id"] for e in ledger}:
        raise SystemExit("the test run must be a recorded held-out run (test-access ledger)")
    test = runs.load(test_id)
    if test.protocol.manifest_sha256 != params["manifest_sha256"]:
        raise SystemExit("calibration and test runs use different split manifests")
    req = (
        max(test.sampler["horizons_h"]),
        test.sampler["step_minutes"],
        test.sampler["history_hours"],
    )
    if req != tuple(params["request"].values()):
        raise SystemExit(f"test request {req} differs from the calibration's {params['request']}")
    hashed = {
        "design_sha256": params["design_sha256"],
        "level": args.level,
        "calibration_set_id": params["calibration_set_id"],
    }
    # On record BEFORE any test record is read: a crash can never leave a look unrecorded.
    record_analysis(args.dataset_id, test_id, "hexsets_h1_started", hashed, sha, "(started)")
    rows = underway_v2(runs.table(test_id, "records"), runs.table(test_id, "cutoffs"))
    cutoffs = runs.table(test_id, "cutoffs").filter(
        pl.col("cutoff_id").is_in(rows["cutoff_id"].unique().implode())
    )
    print(f"test: retrieval v2 k={params['design']['k']} on {cutoffs.height} underway cutoffs ...")
    cand, failed, _ = rerun_candidates(test, params["design"]["k"], cutoffs)
    out = []
    for f in params["cells"]:
        st, hz, res = f["stratum"], f["horizon_h"], f["res"]
        area = hx.cell_area_km2(res)
        g = rows.filter(STRATA[st] & (pl.col("horizon_h") == hz)).to_dicts()
        for r in g:
            fr, mix = mixture(r, cand.get(r["cutoff_id"]), f["c"], f["broad_m"])
            truth = hx.cell_of(r["truth_lat"], r["truth_lon"], res)
            kde = hx.level_set_cells(mix, fr, f["tau"], res)
            disc = hx.disc_cells(r["pred_lat"], r["pred_lon"], f["disc_radius_m"], res)
            out.append(
                {
                    "stratum": st,
                    "horizon_h": hz,
                    "cutoff_id": r["cutoff_id"],
                    "vessel_id": r["vessel_id"],
                    "week": r["as_of"].isocalendar().week,
                    "retrieval_error": failed.get(r["cutoff_id"]),
                    "kde_covered": truth in set(kde),
                    "kde_area_km2": len(kde) * area,
                    "disc_covered": truth in set(disc),
                    "disc_area_km2": len(disc) * area,
                }
            )
        print(f"  {st} {hz:g} h: {len(g)} samples")
    df = pl.DataFrame(out)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = RUNS_DIR / "hexsets" / test_id / stamp
    d.mkdir(parents=True)
    df.write_parquet(d / "samples.parquet")
    summary, verdict = summarise(df, params, args.level)
    report(d, test_id, params, summary, verdict, len(failed), sha)
    (d / "summary.json").write_text(
        json.dumps(
            {
                "test_run_id": test_id,
                "params": params,
                "level": args.level,
                "cells": summary,
                "verdict": verdict,
                "retrieval_failures": {"n": len(failed), "codes": dict(Counter(failed.values()))},
                "git_sha": sha,
                "created_at": stamp,
            },
            indent=1,
            default=str,
        )
        + "\n"
    )
    record_analysis(args.dataset_id, test_id, "hexsets_h1", hashed, sha, str(d))
    print((d / "report.md").read_text())
    print(f"-> {d}")


def _boot(g: pl.DataFrame, num: str, den: str | None) -> tuple[float, float]:
    rng = np.random.default_rng(20230601)
    agg = g.group_by("vessel_id").agg(
        pl.col(num).cast(float).sum().alias("a"),
        (pl.col(den).sum() if den else pl.len()).alias("b"),
    )
    a, b = agg["a"].to_numpy(), agg["b"].to_numpy().astype(float)
    picks = rng.integers(0, len(agg), (N_BOOT, len(agg)))
    est = a[picks].sum(1) / b[picks].sum(1)
    return float(np.quantile(est, 0.025)), float(np.quantile(est, 0.975))


def summarise(df: pl.DataFrame, params: dict, level: float):
    rows = []
    for f in params["cells"]:
        g = df.filter((pl.col("stratum") == f["stratum"]) & (pl.col("horizon_h") == f["horizon_h"]))
        if not g.height:
            continue
        n = g.height
        row = {
            "stratum": f["stratum"],
            "horizon_h": f["horizon_h"],
            "n": n,
            "n_vessels": g["vessel_id"].n_unique(),
            "disc_coverage": float(g["disc_covered"].mean()),
            "disc_ci": _boot(g, "disc_covered", None),
            "kde_coverage": float(g["kde_covered"].mean()),
            "kde_ci": _boot(g, "kde_covered", None),
            "disc_area": float(g["disc_area_km2"].mean()),
            "kde_area": float(g["kde_area_km2"].mean()),
            "area_ratio": float(g["kde_area_km2"].sum() / g["disc_area_km2"].sum()),
            "area_ratio_ci": _boot(g, "kde_area_km2", "disc_area_km2"),
        }
        ok = n >= TOLERANCE_MIN_N
        row["disc_within"] = ok and abs(row["disc_coverage"] - level) <= TOLERANCE
        row["kde_within"] = ok and abs(row["kde_coverage"] - level) <= TOLERANCE
        rows.append(row)
    pooled = [r for r in rows if r["stratum"] == "underway:ALL"]
    verdict = {
        "kde_cells_within": sum(r["kde_within"] for r in rows),
        "disc_cells_within": sum(r["disc_within"] for r in rows),
        "n_cells": len(rows),
        "pooled_horizons_smaller": sum(r["kde_area"] < r["disc_area"] for r in pooled),
        "n_pooled_horizons": len(pooled),
    }
    verdict["adopted"] = (
        verdict["kde_cells_within"] >= verdict["disc_cells_within"]
        and verdict["pooled_horizons_smaller"] >= 4
    )
    return rows, verdict


def report(d: Path, test_id, params, rows, verdict, n_failed, sha) -> None:
    lines = [
        f"# H1 hex sets vs disc on the held-out test run {test_id}",
        "",
        f"Calibration run `{params['calibration_set_id']}`; design sha "
        f"`{params['design_sha256'][:12]}`; git `{sha[:12]}`. Retrieval re-run failures: "
        f"{n_failed} (broad-only sets). Tolerance ±{TOLERANCE} on n ≥ {TOLERANCE_MIN_N}.",
        "",
        "| stratum | h | n (vessels) | disc coverage [CI] | H1 coverage [CI] | disc km² | "
        "H1 km² | H1/disc area [CI] |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['stratum']} | {r['horizon_h']:g} h | {r['n']} ({r['n_vessels']}) | "
            f"{r['disc_coverage']:.3f} [{r['disc_ci'][0]:.3f}, {r['disc_ci'][1]:.3f}]"
            f"{'' if r['disc_within'] else ' ✗'} | {r['kde_coverage']:.3f} [{r['kde_ci'][0]:.3f}, "
            f"{r['kde_ci'][1]:.3f}]{'' if r['kde_within'] else ' ✗'} | {r['disc_area']:,.0f} | "
            f"{r['kde_area']:,.0f} | {r['area_ratio']:.2f} [{r['area_ratio_ci'][0]:.2f}, "
            f"{r['area_ratio_ci'][1]:.2f}] |"
        )
    lines += [
        "",
        f"**Pre-registered verdict:** H1 within tolerance on {verdict['kde_cells_within']}"
        f" of {verdict['n_cells']} cells (disc: {verdict['disc_cells_within']}); H1 smaller "
        f"on {verdict['pooled_horizons_smaller']} of {verdict['n_pooled_horizons']} pooled "
        f"horizons -> **{'adopted' if verdict['adopted'] else 'not adopted'}**.",
    ]
    (d / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
