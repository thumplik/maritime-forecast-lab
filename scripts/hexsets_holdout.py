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

    uv run --project backend python scripts/hexsets_holdout.py --dry-run <dev or calibration run>
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
LEVEL = 0.9  # pre-registered: 90% nominal, no level correction (not a command-line option)
EPS_TOL = 1e-9  # coverage exactly at 0.87 or 0.93 counts as within (decided before the look)
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


def mixture(row, cand, cell):
    frame = LocalFrame(row["pred_lat"], row["pred_lon"])
    kw = {"eps": cell["eps"], "floor_m": cell["min_bandwidth_m"]}
    if cand is None:
        z = np.zeros(0)
        return frame, hx.h1_mixture(frame, z, z, z, cell["c"], cell["broad_m"], **kw)
    tt, lat, lon, cov, w = cand
    j = tt.index(row["target_time"])
    use = cov[:, j]
    return frame, hx.h1_mixture(
        frame, lat[use, j], lon[use, j], w[use], cell["c"], cell["broad_m"], **kw
    )


def expected_cells(design: dict) -> list[tuple[str, float]]:
    return [(st, float(h)) for st in STRATA for h in design["c_by_horizon"]]


def fit(design: dict, cal_run) -> tuple[list[dict], list[dict], dict]:
    runs = RunStore()
    rows = underway_v2(runs.table(cal_run.run_id, "records"), runs.table(cal_run.run_id, "cutoffs"))
    cutoffs = runs.table(cal_run.run_id, "cutoffs").filter(
        pl.col("cutoff_id").is_in(rows["cutoff_id"].unique().implode())
    )
    print(f"calibration: retrieval v2 k={design['k']} on {cutoffs.height} underway cutoffs ...")
    cand, failed, model = rerun_candidates(cal_run, design["k"], cutoffs)
    cells, skipped = [], []
    for st, hz in expected_cells(design):
        res, c = hx.resolution_for(hz), design["c_by_horizon"][f"{hz:g}"]
        g = rows.filter(STRATA[st] & (pl.col("horizon_h") == hz)).to_dicts()
        d = np.array(
            [
                hx.disc_score(
                    LocalFrame(r["pred_lat"], r["pred_lon"]), r["truth_lat"], r["truth_lon"], res
                )
                for r in g
            ]
        )
        radius = hx.conformal_quantile(d, LEVEL)
        if radius is None:
            skipped.append(
                {
                    "stratum": st,
                    "horizon_h": hz,
                    "n": len(g),
                    "why": "disc quantile undefined (n too small)",
                }
            )
            continue
        cell = {
            "stratum": st,
            "horizon_h": hz,
            "level": LEVEL,
            "res": res,
            "c": c,
            "eps": design["eps"],
            "min_bandwidth_m": design["min_bandwidth_m"],
            "n": len(g),
            "n_vessels": len({r["vessel_id"] for r in g}),
            "disc_radius_m": radius,
            "broad_m": max(radius / 2, 1000.0),
        }
        sc = []
        for r in g:
            fr, mix = mixture(r, cand.get(r["cutoff_id"]), cell)
            sc.append(hx.density_score(mix, fr, r["truth_lat"], r["truth_lon"], res))
        tau = hx.density_threshold(np.array(sc), LEVEL)
        if tau is None:
            skipped.append(
                {
                    "stratum": st,
                    "horizon_h": hz,
                    "n": len(g),
                    "why": "density quantile undefined (n too small)",
                }
            )
            continue
        cells.append({**cell, "tau": tau})
        print(f"  {st} {hz:g} h: n={len(g)}, c={c}, disc r={radius / 1000:.1f} km")
    info = {
        "failed": len(failed),
        "codes": dict(Counter(failed.values())),
        "model_config": model.config,
    }
    return cells, skipped, info


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--protocol", default="P-known")
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06-07")
    ap.add_argument("--calibration-run")
    ap.add_argument("--test-run")
    ap.add_argument(
        "--params-only",
        action="store_true",
        help="fit and write the served parameters; read no test records",
    )
    ap.add_argument(
        "--dry-run",
        metavar="RUN_ID",
        help="exercise the whole scoring path on a NON-test run (dev or calibration): "
        "no params written, no ledger entry",
    )
    ap.add_argument("--rerun-reason", help="required to look at the test run a second time")
    args = ap.parse_args()
    sha = git_sha()
    if not args.dry_run and (sha.endswith("-dirty") or sha == "unknown"):
        raise SystemExit("run from a clean, committed tree (outputs are stamped with its SHA)")
    design_path = PARAMS_DIR / "h1_design.json"
    design = json.loads(design_path.read_text())
    if (design["eps"], design["min_bandwidth_m"]) != (hx.H1_EPS, hx.H1_MIN_BANDWIDTH_M):
        raise SystemExit("the frozen design and evaluation/hexsets.py constants disagree")
    for _, hz in expected_cells(design):
        if hx.resolution_for(hz) != (7 if hz <= 3 else 6):
            raise SystemExit("resolution rule differs from the frozen design")
    runs = RunStore()
    cal_id = args.calibration_run or latest(args.protocol, args.dataset_id, "calibration")
    cal_run = runs.load(cal_id)
    if cal_run.sampler.get("split") != "calibration":
        raise SystemExit(f"{cal_id} is not a calibration-split run")
    man = SplitManifest.model_validate_json(Path(cal_run.sampler["manifest_path"]).read_text())
    cells, skipped, info = fit(design, cal_run)
    s = cal_run.sampler
    params = {
        "kind": "h1_hex_sets",
        "protocol": args.protocol,
        "dataset_id": args.dataset_id,
        "calibration_set_id": cal_id,
        "manifest_sha256": cal_run.protocol.manifest_sha256,
        "design": design,
        "design_sha256": hashlib.sha256(design_path.read_bytes()).hexdigest(),
        "level": LEVEL,
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
        "skipped_cells": skipped,
        "verdict": None,  # set by the held-out comparison; the API labels sets until then
        "git_sha": sha,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out_params = PARAMS_DIR / f"{args.protocol}__{args.dataset_id}.json"
    if args.dry_run:
        evaluate(args, params, sha, dry=True)
        return
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    out_params.write_text(json.dumps(params, indent=1, default=str) + "\n")
    print(f"params -> {out_params}")
    if args.params_only:
        print("params only: no test records read")
        return
    d, verdict, test_id = evaluate(args, params, sha, dry=False)
    params["verdict"] = {**verdict, "test_run_id": test_id, "analysis": str(d)}
    out_params.write_text(json.dumps(params, indent=1, default=str) + "\n")
    print(f"verdict written to {out_params}")


def evaluate(args, params: dict, sha: str, dry: bool):
    runs = RunStore()
    hashed = {
        "design_sha256": params["design_sha256"],
        "level": LEVEL,
        "calibration_set_id": params["calibration_set_id"],
    }
    informed: list[str] = []
    if dry:
        test_id = args.dry_run
        test = runs.load(test_id)
        if test.sampler.get("split") == "test":
            raise SystemExit("--dry-run never touches a test-split run")
    else:
        entries = read_ledger(args.dataset_id)
        scored = [e for e in entries if e["protocol"] == args.protocol and e.get("kind") is None]
        test_id = args.test_run or (scored[-1]["run_id"] if scored else None)
        if test_id is None or test_id not in {e["run_id"] for e in scored}:
            raise SystemExit("the test run must be a recorded held-out run (test-access ledger)")
        again = [
            e
            for e in entries
            if e["run_id"] == test_id and e.get("kind") in ("hexsets_h1_started", "hexsets_h1")
        ]
        if again and not args.rerun_reason:
            raise SystemExit(
                f"{test_id} was already looked at for H1 ({len(again)} ledger "
                "entries); pass --rerun-reason to look again"
            )
        # Disclosure: earlier analyses of this test run (e.g. the disc's July coverage).
        informed = [
            f"{e['kind']}@{e['accessed_at']}"
            for e in entries
            if e["run_id"] == test_id and e.get("kind") and not e["kind"].startswith("hexsets_h1")
        ]
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
    if not dry:
        # On record BEFORE any test record is read: a crash can never leave a look unrecorded.
        record_analysis(
            args.dataset_id,
            test_id,
            "hexsets_h1_started",
            hashed,
            sha,
            "(started)",
            informed_by=informed,
            rerun_reason=args.rerun_reason,
        )
    rows = underway_v2(runs.table(test_id, "records"), runs.table(test_id, "cutoffs"))
    cutoffs = runs.table(test_id, "cutoffs").filter(
        pl.col("cutoff_id").is_in(rows["cutoff_id"].unique().implode())
    )
    print(
        f"scoring {test_id}: retrieval v2 k={params['design']['k']} on {cutoffs.height} "
        "underway cutoffs ..."
    )
    cand, failed, _ = rerun_candidates(test, params["design"]["k"], cutoffs)
    out = []
    for f in params["cells"]:
        st, hz, res = f["stratum"], f["horizon_h"], f["res"]
        area = hx.cell_area_km2(res)
        g = rows.filter(STRATA[st] & (pl.col("horizon_h") == hz)).to_dicts()
        for r in g:
            fr, mix = mixture(r, cand.get(r["cutoff_id"]), f)
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
                    "n_candidates": len(mix.xy),
                    "kde_covered": truth in set(kde),
                    "kde_area_km2": len(kde) * area,
                    "disc_covered": truth in set(disc),
                    "disc_area_km2": len(disc) * area,
                }
            )
        print(f"  {st} {hz:g} h: {len(g)} samples")
    schema = {
        "stratum": pl.String,
        "horizon_h": pl.Float64,
        "cutoff_id": pl.UInt32,
        "vessel_id": pl.String,
        "week": pl.Int64,
        "retrieval_error": pl.String,
        "n_candidates": pl.Int64,
        "kde_covered": pl.Boolean,
        "kde_area_km2": pl.Float64,
        "disc_covered": pl.Boolean,
        "disc_area_km2": pl.Float64,
    }
    df = pl.DataFrame(out, schema=schema)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = RUNS_DIR / "hexsets" / test_id / (f"dryrun_{stamp}" if dry else stamp)
    d.mkdir(parents=True)
    df.write_parquet(d / "samples.parquet")
    summary, verdict = summarise(df, params)
    report(d, test_id, params, summary, verdict, len(failed), sha, informed, dry)
    (d / "summary.json").write_text(
        json.dumps(
            {
                "test_run_id": test_id,
                "dry_run": dry,
                "params": params,
                "level": LEVEL,
                "cells": summary,
                "verdict": verdict,
                "informed_by": informed,
                "retrieval_failures": {"n": len(failed), "codes": dict(Counter(failed.values()))},
                "git_sha": sha,
                "created_at": stamp,
            },
            indent=1,
            default=str,
        )
        + "\n"
    )
    if not dry:
        record_analysis(
            args.dataset_id,
            test_id,
            "hexsets_h1",
            hashed,
            sha,
            str(d),
            informed_by=informed,
            rerun_reason=args.rerun_reason,
        )
    print((d / "report.md").read_text())
    print(f"-> {d}")
    return d, verdict, test_id


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


def _within(covered: int, n: int) -> bool:
    """|coverage - level| <= tolerance with n >= the minimum, in counts (no float edge)."""
    return n >= TOLERANCE_MIN_N and abs(covered - LEVEL * n) <= TOLERANCE * n + EPS_TOL


def summarise(df: pl.DataFrame, params: dict):
    """Per cell results and the pre-registered verdict. Every expected cell (2 strata x the
    design's horizons) is counted: a missing one is neither within tolerance nor smaller."""
    rows = []
    for st, hz in expected_cells(params["design"]):
        g = df.filter((pl.col("stratum") == st) & (pl.col("horizon_h") == hz))
        if not g.height:
            rows.append(
                {
                    "stratum": st,
                    "horizon_h": hz,
                    "n": 0,
                    "missing": True,
                    "disc_within": False,
                    "kde_within": False,
                    "kde_smaller": False,
                }
            )
            continue
        n = g.height
        kc, dc = int(g["kde_covered"].sum()), int(g["disc_covered"].sum())
        row = {
            "stratum": st,
            "horizon_h": hz,
            "n": n,
            "missing": False,
            "n_vessels": g["vessel_id"].n_unique(),
            "zero_candidates": int((g["n_candidates"] == 0).sum()),
            "disc_coverage": dc / n,
            "disc_ci": _boot(g, "disc_covered", None),
            "kde_coverage": kc / n,
            "kde_ci": _boot(g, "kde_covered", None),
            "disc_area": float(g["disc_area_km2"].mean()),
            "kde_area": float(g["kde_area_km2"].mean()),
            "area_ratio": float(g["kde_area_km2"].sum() / g["disc_area_km2"].sum()),
            "area_ratio_ci": _boot(g, "kde_area_km2", "disc_area_km2"),
            "disc_within": _within(dc, n),
            "kde_within": _within(kc, n),
        }
        row["kde_smaller"] = row["kde_area"] < row["disc_area"]
        rows.append(row)
    pooled = [r for r in rows if r["stratum"] == "underway:ALL"]
    verdict = {
        "kde_cells_within": sum(r["kde_within"] for r in rows),
        "disc_cells_within": sum(r["disc_within"] for r in rows),
        "n_cells": len(rows),
        "missing_cells": [f"{r['stratum']}@{r['horizon_h']:g}h" for r in rows if r["missing"]],
        "pooled_horizons_smaller": sum(r["kde_smaller"] for r in pooled),
        "n_pooled_horizons": len(pooled),
    }
    verdict["adopted"] = (
        verdict["kde_cells_within"] >= verdict["disc_cells_within"]
        and verdict["pooled_horizons_smaller"] >= 4
    )
    return rows, verdict


def report(d: Path, test_id, params, rows, verdict, n_failed, sha, informed, dry) -> None:
    title = "DRY RUN (crash check on a non-test run; not a result)" if dry else "held-out test run"
    lines = [
        f"# H1 hex sets vs disc: {title} {test_id}",
        "",
        f"Calibration run `{params['calibration_set_id']}`; design sha "
        f"`{params['design_sha256'][:12]}`; git `{sha[:12]}`. Level {LEVEL:.0%}, tolerance "
        f"±{TOLERANCE} on n ≥ {TOLERANCE_MIN_N}. Retrieval re-run failures: {n_failed} "
        "(broad-only sets; per-cell zero-candidate counts below).",
        "",
    ]
    if informed:
        lines += [
            f"Earlier analyses of this run (the disc arm is not blind): {', '.join(informed)}.",
            "",
        ]
    lines += [
        "| stratum | h | n (vessels) | disc coverage [CI] | H1 coverage [CI] | disc km² | "
        "H1 km² | H1/disc area [CI] | 0-cand. |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["missing"]:
            lines.append(
                f"| {r['stratum']} | {r['horizon_h']:g} h | 0 | missing | missing | | | | |"
            )
            continue
        lines.append(
            f"| {r['stratum']} | {r['horizon_h']:g} h | {r['n']} ({r['n_vessels']}) | "
            f"{r['disc_coverage']:.3f} [{r['disc_ci'][0]:.3f}, {r['disc_ci'][1]:.3f}]"
            f"{'' if r['disc_within'] else ' ✗'} | {r['kde_coverage']:.3f} [{r['kde_ci'][0]:.3f}, "
            f"{r['kde_ci'][1]:.3f}]{'' if r['kde_within'] else ' ✗'} | {r['disc_area']:,.0f} | "
            f"{r['kde_area']:,.0f} | {r['area_ratio']:.2f} [{r['area_ratio_ci'][0]:.2f}, "
            f"{r['area_ratio_ci'][1]:.2f}] | {r['zero_candidates']} |"
        )
    lines += [
        "",
        f"**Pre-registered verdict:** H1 within tolerance on {verdict['kde_cells_within']}"
        f" of {verdict['n_cells']} cells (disc: {verdict['disc_cells_within']}); H1 smaller "
        f"on {verdict['pooled_horizons_smaller']} of {verdict['n_pooled_horizons']} pooled "
        f"horizons; missing cells: {verdict['missing_cells'] or 'none'} -> "
        f"**{'adopted' if verdict['adopted'] else 'not adopted'}**.",
    ]
    (d / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
