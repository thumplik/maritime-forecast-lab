"""Milestone 7 slice 1: hex prediction sets vs the disc baseline, on a DEV benchmark run.

For the underway cutoffs of a dev-window run, at each horizon:
  * H1 density level set from retrieval v2 re-run with K candidates (similarity weights, only
    candidates whose reference covers the target time), mixed with a broad Gaussian around the
    run's scored v2 central point;
  * the disc baseline: the conformal disc around that same central point, as cells.
Both are calibrated on the first half of the window (chronological split at the median cutoff
time) and evaluated on the second, per stratum (underway:ALL, underway:TRANSITING) and horizon.
The H1 bandwidth is chosen on the calibration half only (smallest mean set area). Coverage =
the true position's cell is in the set. Development record only: dev runs never feed the
README, and the held-out test split is refused.

    uv run --project backend python scripts/hexsets_dev.py \
        --run P-known_noaa_us_gulf_2023-06-07_dev_20261022T175118Z
"""

from __future__ import annotations

import argparse
import json
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from maritime_forecast.config import RUNS_DIR
from maritime_forecast.evaluation import hexsets as hx
from maritime_forecast.geo.geodesic import LocalFrame
from maritime_forecast.models.base import ForecastHorizon, ModelContext, histories_for_cutoffs
from maritime_forecast.models.route_retrieval import V2, RouteRetrieval
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore

BANDWIDTHS_M = (500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
EPS = 0.05
SELECT_N = 300  # calibration samples per cell used to estimate set area for bandwidth choice
N_BOOT = 500


def candidates(model, store, dataset_id, cutoffs, history_hours, horizon_h, step):
    """cutoff_id -> (target_times, lat (k, T), lon (k, T), covered (k, T), weights (k,)), and
    cutoff_id -> error code for every re-run that produced no candidates (never dropped: those
    cutoffs get a broad-only set)."""
    out, failed = {}, {}
    for cid, h in histories_for_cutoffs(store, dataset_id, cutoffs, history_hours):
        f = model.predict(h, ForecastHorizon(h.as_of, horizon_h, step))
        if f.status != "ok" or not f.trajectories:
            failed[cid] = f.error.code if f.error else "no_candidates"
            continue
        tt = f.target_times
        lat = np.array([[p.latitude_deg for p in t.points] for t in f.trajectories])
        lon = np.array([[p.longitude_deg for p in t.points] for t in f.trajectories])
        ends = [t.reference_ends_at for t in f.trajectories]
        cov = np.array([[e is None or t <= e for t in tt] for e in ends])
        w = np.array([t.similarity or 0.0 for t in f.trajectories])
        out[cid] = (tt, lat, lon, cov, w)
    return out, failed


def mixture_for(row, cand, h, broad_m):
    """H1 mixture in the frame centred on the scored central point at this horizon. Without
    candidates (a failed re-run) it is the broad component alone."""
    frame = LocalFrame(row["pred_lat"], row["pred_lon"])
    if cand is None:
        return frame, hx.Mixture(np.zeros((0, 2)), np.zeros(0), h, (0.0, 0.0), broad_m, EPS), 0
    tt, lat, lon, cov, w = cand
    j = tt.index(row["target_time"])
    use = cov[:, j] & (w > 0)
    x, y = frame.to_xy(lat[use, j], lon[use, j])
    mix = hx.Mixture(np.column_stack([x, y]), w[use], h, (0.0, 0.0), broad_m, EPS)
    return frame, mix, int(use.sum())


def mean_area(rows, cand, h, broad, tau, res) -> float:
    sizes = []
    for r in rows:
        fr, mix, _ = mixture_for(r, cand.get(r["cutoff_id"]), h, broad)
        sizes.append(len(hx.level_set_cells(mix, fr, tau, res)))
    return float(np.mean(sizes)) * hx.cell_area_km2(res)


def selection_part(vessel_id: str) -> bool:
    """A third of the calibration vessels choose the bandwidth; the rest fit the thresholds."""
    return zlib.crc32(vessel_id.encode()) % 3 == 0


def vessel_ci(df: pl.DataFrame, col: str) -> tuple[float, float]:
    rng = np.random.default_rng(20230601)
    g = df.group_by("vessel_id").agg(pl.col(col).sum().alias("s"), pl.len().alias("n"))
    s, n = g["s"].to_numpy().astype(float), g["n"].to_numpy().astype(float)
    picks = rng.integers(0, len(g), (N_BOOT, len(g)))
    est = s[picks].sum(1) / n[picks].sum(1)
    return float(np.quantile(est, 0.025)), float(np.quantile(est, 0.975))


def area_ratio_ci(df: pl.DataFrame) -> tuple[float, float, float]:
    """Paired H1/disc mean-area ratio with a vessel-bootstrap 95% CI."""
    rng = np.random.default_rng(20230601)
    g = df.group_by("vessel_id").agg(pl.col("kde_area_km2").sum().alias("a"),
                                     pl.col("disc_area_km2").sum().alias("b"))  # fmt: skip
    a, b = g["a"].to_numpy(), g["b"].to_numpy()
    picks = rng.integers(0, len(g), (N_BOOT, len(g)))
    est = a[picks].sum(1) / b[picks].sum(1)
    return float(a.sum() / b.sum()), float(np.quantile(est, 0.025)), float(np.quantile(est, 0.975))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--level", type=float, default=0.9)
    args = ap.parse_args()
    runs = RunStore()
    run = runs.load(args.run)
    if run.sampler.get("split") != "dev":
        raise SystemExit("development experiment: a dev-split run only (never the hold-out)")
    sha = git_sha()
    rec = runs.table(args.run, "records")
    cut = runs.table(args.run, "cutoffs")
    v2 = rec.filter(
        (pl.col("model_id") == "route_retrieval_v2")
        & pl.col("paired")
        & (pl.col("motion_set") == "underway")
        & (pl.col("status") == "ok")
    )
    if not v2.height:
        raise SystemExit("the run has no paired underway route_retrieval_v2 records")
    ids = v2["cutoff_id"].unique()
    cutoffs = cut.filter(pl.col("cutoff_id").is_in(ids.implode()))
    s = run.sampler
    manifest = SplitManifest.model_validate_json(Path(s["manifest_path"]).read_text())
    dev = [p for p in manifest.periods or [] if p.split == "dev"]
    if not dev or not all(any(p.start <= t < p.end for p in dev) for t in cutoffs["observed_at"]):
        raise SystemExit("every cutoff must lie inside the manifest's dev period")
    boundary = cutoffs["observed_at"].sort()[cutoffs.height // 2]
    store = ParquetStore()
    ctx = ModelContext(store, run.dataset_id, run.protocol.protocol,
                       run.protocol.manifest_sha256, params={"config": {"k": args.k}})  # fmt: skip
    model = RouteRetrieval.from_context(ctx, V2)
    if model.unavailable_reason:
        raise SystemExit(model.unavailable_reason)
    hmax = max(s["horizons_h"])
    print(f"retrieval v2 with k={args.k} on {cutoffs.height} underway cutoffs ...")
    keys = cutoffs.select("cutoff_id", "vessel_id", "observed_at")
    cand, failed = candidates(model, store, run.dataset_id, keys, s["history_hours"], hmax,
                              s["step_minutes"])  # fmt: skip
    rows = v2.join(cutoffs.select("cutoff_id", pl.col("observed_at").alias("as_of")),
                   on="cutoff_id").with_columns(
        target_time=pl.col("as_of") + pl.duration(minutes=(pl.col("horizon_h") * 60).cast(int)),
    ).with_columns(
        # Purge (§6.2): a calibration row's label must be known before the evaluation half.
        half=pl.when(pl.col("target_time") < boundary).then(pl.lit("cal"))
        .when(pl.col("as_of") >= boundary).then(pl.lit("eval")).otherwise(pl.lit("purged")),
    )  # fmt: skip
    strata = {"underway:ALL": pl.lit(True), "underway:TRANSITING": pl.col("transiting")}
    out, fitted, skipped = [], [], []
    for st, cond in strata.items():
        for hz in sorted(rows["horizon_h"].unique().to_list()):
            res = hx.resolution_for(hz)
            area = hx.cell_area_km2(res)
            g = rows.filter(cond & (pl.col("horizon_h") == hz)).to_dicts()
            cal = [r for r in g if r["half"] == "cal"]
            sel = [r for r in cal if selection_part(r["vessel_id"])]
            fit = [r for r in cal if not selection_part(r["vessel_id"])]
            ev = [r for r in g if r["half"] == "eval"]
            if len(sel) < 30 or len(fit) < 50 or not ev:
                skipped.append({"stratum": st, "horizon_h": hz, "n_select": len(sel),
                                "n_fit": len(fit), "n_eval": len(ev),
                                "why": "too few calibration or evaluation samples"})  # fmt: skip
                continue

            def disc_radius(rows_, res=res):
                d = [hx.disc_score(LocalFrame(r["pred_lat"], r["pred_lon"]), r["truth_lat"],
                                   r["truth_lon"], res) for r in rows_]  # fmt: skip
                return hx.conformal_quantile(np.array(d), args.level)

            def tau_for(rows_, h, broad, res=res):
                sc = []
                for r in rows_:
                    fr, mix, _ = mixture_for(r, cand.get(r["cutoff_id"]), h, broad)
                    sc.append(hx.density_score(mix, fr, r["truth_lat"], r["truth_lon"], res))
                return hx.density_threshold(np.array(sc), args.level)

            # Bandwidth: chosen on the selection third only (its own radius and thresholds).
            r_sel = disc_radius(sel)
            best = None
            if r_sel is not None:
                b_sel = max(r_sel / 2, 1000.0)
                sub = sel[:: max(1, len(sel) // SELECT_N)]
                for h in BANDWIDTHS_M:
                    t = tau_for(sel, h, b_sel)
                    if t is not None:
                        a = mean_area(sub, cand, h, b_sel, t, res)
                        if best is None or a < best[1]:
                            best = (h, a)
            radius = disc_radius(fit)
            if best is None or radius is None:
                skipped.append({"stratum": st, "horizon_h": hz, "n_select": len(sel),
                                "n_fit": len(fit), "n_eval": len(ev),
                                "why": "conformal quantile undefined (n too small)"})  # fmt: skip
                continue
            h = best[0]
            broad = max(radius / 2, 1000.0)
            tau = tau_for(fit, h, broad)
            fitted.append({"stratum": st, "horizon_h": hz, "res": res, "n_select": len(sel),
                           "n_fit": len(fit), "disc_radius_m": radius, "bandwidth_m": h,
                           "tau": tau, "broad_m": broad})  # fmt: skip
            for r in ev:
                fr, mix, k = mixture_for(r, cand.get(r["cutoff_id"]), h, broad)
                truth = hx.cell_of(r["truth_lat"], r["truth_lon"], res)
                kde = hx.level_set_cells(mix, fr, tau, res)
                disc = hx.disc_cells(r["pred_lat"], r["pred_lon"], radius, res)
                out.append({
                    "stratum": st, "horizon_h": hz, "cutoff_id": r["cutoff_id"],
                    "vessel_id": r["vessel_id"], "n_candidates": k,
                    "retrieval_error": failed.get(r["cutoff_id"]),
                    "kde_covered": truth in set(kde), "kde_area_km2": len(kde) * area,
                    "disc_covered": truth in set(disc), "disc_area_km2": len(disc) * area,
                })  # fmt: skip
            print(f"{st} {hz:g} h: select {len(sel)}, fit {len(fit)}, eval {len(ev)}, "
                  f"h={h:.0f} m, disc r={radius / 1000:.1f} km")  # fmt: skip
    res_df = pl.DataFrame(out)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = RUNS_DIR / "hexsets" / args.run / stamp
    d.mkdir(parents=True)
    res_df.write_parquet(d / "samples.parquet")
    lines = [f"# Hex sets vs disc, {args.run} (dev; development record)", "",
             f"Level {args.level:.0%}; calibration = cutoffs before {boundary.isoformat()}, "
             f"evaluation = after (labels purged at the boundary). K={args.k}, eps={EPS}, "
             f"git `{sha[:12]}`. Retrieval re-run failures: {len(failed)} (broad-only sets).",
             "",
             "| stratum | h | n eval | disc coverage [CI] | H1 coverage [CI] | disc km² | H1 km² | "
             "H1/disc area [CI] | H1 bandwidth |",
             "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    summary = []
    for f in fitted:
        g = res_df.filter((pl.col("stratum") == f["stratum"])
                          & (pl.col("horizon_h") == f["horizon_h"]))  # fmt: skip
        row = {**f, "n_eval": g.height,
               "disc_coverage": float(g["disc_covered"].mean()),
               "disc_coverage_ci": vessel_ci(g.with_columns(pl.col("disc_covered").cast(int)),
                                             "disc_covered"),
               "kde_coverage": float(g["kde_covered"].mean()),
               "kde_coverage_ci": vessel_ci(g.with_columns(pl.col("kde_covered").cast(int)),
                                            "kde_covered"),
               "disc_area_mean": float(g["disc_area_km2"].mean()),
               "kde_area_mean": float(g["kde_area_km2"].mean()),
               "area_ratio": area_ratio_ci(g)}  # fmt: skip
        summary.append(row)
        lines.append(
            f"| {f['stratum']} | {f['horizon_h']:g} h | {g.height} | "
            f"{row['disc_coverage']:.3f} [{row['disc_coverage_ci'][0]:.3f}, "
            f"{row['disc_coverage_ci'][1]:.3f}] | {row['kde_coverage']:.3f} "
            f"[{row['kde_coverage_ci'][0]:.3f}, {row['kde_coverage_ci'][1]:.3f}] | "
            f"{row['disc_area_mean']:,.0f} | {row['kde_area_mean']:,.0f} | "
            f"{row['area_ratio'][0]:.2f} [{row['area_ratio'][1]:.2f}, {row['area_ratio'][2]:.2f}]"
            f" | {f['bandwidth_m']:.0f} m |"
        )  # fmt: skip
    (d / "report.md").write_text("\n".join(lines) + "\n")
    (d / "summary.json").write_text(json.dumps({
        "run_id": args.run, "dataset_id": run.dataset_id, "protocol": run.protocol.protocol,
        "manifest_sha256": run.protocol.manifest_sha256, "split": "dev",
        "sampler": {k: s.get(k) for k in ("horizons_h", "step_minutes", "history_hours",
                                          "seed", "cutoff_region")},
        "retrieval": {"model": "route_retrieval_v2", "config": model.config, "k": args.k},
        "central_point": "the run's stored route_retrieval_v2 prediction (k=10)",
        "level": args.level, "eps": EPS, "bandwidths_m": BANDWIDTHS_M,
        "boundary": boundary.isoformat(),
        "split_rule": "calibration: label time < boundary; evaluation: as_of >= boundary; "
                      "others purged. Bandwidth chosen on the vessel-hash selection third; "
                      "disc radius and density threshold fitted on the remaining two thirds",
        "n_underway_cutoffs": cutoffs.height,
        "retrieval_failures": {"n": len(failed),
                               "codes": dict(Counter(failed.values()))},  # fmt: skip
        "skipped_cells": skipped, "git_sha": sha, "cells": summary, "created_at": stamp,
    }, indent=1, default=str) + "\n")  # fmt: skip
    print("\n".join(lines))
    print(f"-> {d}")


if __name__ == "__main__":
    main()
