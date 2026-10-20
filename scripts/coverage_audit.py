"""Milestone 0 coverage audit (build plan §7 M0, §6.3, §6.4).

Measures, on canonical NOAA data:
  * inter-report interval distribution, class mix, motion-state mix;
  * fraction of sampled underway cutoffs in the cutoff region with valid truth at each horizon,
    using truth from (a) the cutoff region only and (b) the full truth region;
  * how class and speed mix of evaluable cutoffs shifts with horizon (survivorship);
  * persistence and constant-velocity errors on the evaluable sets, with vessel-bootstrap CIs;
  * the §6.4 horizon admission decision.

Output: data/runs/coverage_audit_<UTC timestamp>/{summary.json, report.md, cutoffs.parquet,
        records.parquet}

Usage:
    uv run --project backend python scripts/coverage_audit.py [--dataset-id noaa_us_gulf_2023-06]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

from maritime_forecast.config import PROCESSED_DIR, RUNS_DIR, default_region
from maritime_forecast.data.cutoffs import SamplerConfig, sample_cutoffs, transiting_expr
from maritime_forecast.data.segment import SEGMENT_VERSION, SegmentConfig, segmented_sql
from maritime_forecast.evaluation.admission import DEFAULT_ADMISSION, admit_by_stratum
from maritime_forecast.evaluation.bootstrap import vessel_bootstrap_ci
from maritime_forecast.evaluation.paired import paired
from maritime_forecast.evaluation.truth import truth_sql
from maritime_forecast.geo.geodesic import along_cross_track, distance_m, inverse
from maritime_forecast.models.baselines import constant_velocity, persistence
from maritime_forecast.provenance import git_sha

HORIZONS_H = [1, 2, 3, 6, 12, 24]
ADMIT_MIN_FRACTION = 0.5  # §6.4 default
# §6.4 "not dominated by slow vessels": evaluable median SOG must stay within this fraction of
# the underway population's median SOG, and the share of any single class must not grow by
# more than this many percentage points.
ADMIT_MAX_SOG_DROP = 0.25
ADMIT_MAX_CLASS_SHIFT_PP = 15.0


def build_segmented(
    con: duckdb.DuckDBPyConnection, dataset_glob: str, out_dir: Path, n_buckets: int = 16
) -> None:
    """Segment vessel-hash buckets one at a time so each fits in memory (no disk spill)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for b in range(n_buckets):
        src = (
            f"(SELECT * FROM read_parquet('{dataset_glob}', hive_partitioning=false) "
            f"WHERE hash(vessel_id) % {n_buckets} = {b})"
        )
        out = out_dir / f"bucket-{b:02d}.parquet"
        tmp = out.with_suffix(".tmp")
        con.execute(
            f"COPY ({segmented_sql(src, SegmentConfig())}) TO '{tmp}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
        tmp.replace(out)
        print(f"  segmented bucket {b + 1}/{n_buckets}", flush=True)


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset-id", default="noaa_us_gulf_2023-06")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--rebuild-segments", action="store_true")
    args = ap.parse_args()

    cut_box = default_region("cutoff_region")
    truth_box = default_region("truth_region")
    canon_glob = str(PROCESSED_DIR / "canonical" / args.dataset_id / "*" / "*.parquet")
    seg_path = PROCESSED_DIR / "segmented" / args.dataset_id
    run_dir = RUNS_DIR / f"coverage_audit_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    run_dir.mkdir(parents=True)

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET preserve_insertion_order=false")
    if args.rebuild_segments or not seg_path.exists():
        print("segmenting ...", flush=True)
        build_segmented(con, canon_glob, seg_path)
    con.execute(f"CREATE VIEW seg AS SELECT * FROM read_parquet('{seg_path}/*.parquet')")
    t_min, t_max = con.execute("SELECT min(observed_at), max(observed_at) FROM seg").fetchone()
    print(f"data span {t_min} .. {t_max}", flush=True)

    summary: dict = {
        "dataset_id": args.dataset_id,
        "segment_version": SEGMENT_VERSION,
        "segment_config": SegmentConfig().as_dict(),
        "cutoff_region": cut_box.as_dict(),
        "truth_region": truth_box.as_dict(),
        "data_span": [str(t_min), str(t_max)],
        "git_sha": git_sha(),
        "horizons_h": HORIZONS_H,
    }

    # ---- 1. Descriptive statistics in the cutoff region -------------------------------
    in_cut = cut_box.sql_predicate()
    q = lambda sql: con.execute(sql).pl()  # noqa: E731
    desc = {}
    desc["totals_cutoff_region"] = q(
        f"SELECT count(*) AS reports, count(DISTINCT vessel_id) AS vessels FROM seg WHERE {in_cut}"
    ).to_dicts()[0]
    desc["totals_truth_region"] = q(
        "SELECT count(*) AS reports, count(DISTINCT vessel_id) AS vessels FROM seg"
    ).to_dicts()[0]
    desc["interval_quantiles_s"] = q(
        f"""SELECT vessel_class,
              count(*) AS n,
              quantile_cont(dt_prev_s, 0.1) AS p10, quantile_cont(dt_prev_s, 0.5) AS p50,
              quantile_cont(dt_prev_s, 0.9) AS p90, quantile_cont(dt_prev_s, 0.99) AS p99,
              avg(CASE WHEN dt_prev_s > 1800 THEN 1 ELSE 0 END) AS frac_gt_30min
            FROM seg WHERE {in_cut} AND dt_prev_s IS NOT NULL
            GROUP BY ROLLUP (vessel_class) ORDER BY n DESC"""
    ).to_dicts()
    desc["class_mix"] = q(
        f"""SELECT vessel_class, count(DISTINCT vessel_id) AS vessels, count(*) AS reports
            FROM seg WHERE {in_cut} GROUP BY 1 ORDER BY vessels DESC"""
    ).to_dicts()
    desc["motion_mix_reports"] = q(
        f"""SELECT vessel_class,
              avg(CASE WHEN sog_mean_10m < 0.5 THEN 1 ELSE 0 END) AS stationary,
              avg(CASE WHEN sog_mean_10m >= 0.5 AND sog_mean_10m < 2 THEN 1 ELSE 0 END) AS slow,
              avg(CASE WHEN sog_mean_10m >= 2 THEN 1 ELSE 0 END) AS underway,
              avg(CASE WHEN sog_mean_10m IS NULL THEN 1 ELSE 0 END) AS unknown,
              count(*) AS reports
            FROM seg WHERE {in_cut} GROUP BY ROLLUP (vessel_class) ORDER BY reports DESC"""
    ).to_dicts()
    # System-wide feed outages: hours whose Gulf report volume is < 25% of the median hour.
    hourly = q("SELECT date_trunc('hour', observed_at) AS hour, count(*) AS n FROM seg GROUP BY 1")
    med = float(hourly["n"].median())
    desc["outage_hours"] = (
        hourly.filter(pl.col("n") < 0.25 * med)
        .sort("hour")
        .with_columns(frac_of_median=pl.col("n") / med)
        .to_dicts()
    )
    desc["median_reports_per_hour"] = med
    summary["descriptive"] = desc

    # ---- 2. Cutoff sampling ------------------------------------------------------------
    # Cutoffs must leave room for the longest horizon inside the data span, otherwise
    # "missing truth" would conflate coverage loss with the end of the dataset.
    last_cutoff = t_max - timedelta(hours=max(HORIZONS_H))
    cand = q(
        f"""SELECT vessel_id, observed_at, latitude_deg, longitude_deg, sog_knots, cog_deg,
                   vessel_class, transceiver_class, segment_no, sog_mean_10m, n_reports_3h,
                   sog_mean_30m, sog_min_30m
            FROM seg
            WHERE {in_cut} AND observed_at <= TIMESTAMPTZ '{last_cutoff.isoformat()}'
              AND transceiver_class IN ('A', 'B')"""
    )
    samplers = {
        "underway": SamplerConfig(motion_set="underway"),
        "stationary": SamplerConfig(motion_set="stationary"),
    }
    cut_frames = []
    for name, cfg in samplers.items():
        s = sample_cutoffs(cand, cfg).with_columns(motion_set=pl.lit(name))
        cut_frames.append(s)
        print(f"{name}: {s.height} cutoffs, {s['vessel_id'].n_unique()} vessels", flush=True)
    cutoffs = (
        pl.concat(cut_frames)
        .with_row_index("cutoff_id")
        .with_columns(transiting=(pl.col("motion_set") == "underway") & transiting_expr())
    )
    summary["sampler"] = {k: v.as_dict() for k, v in samplers.items()}
    summary["last_cutoff"] = str(last_cutoff)
    cutoffs.write_parquet(run_dir / "cutoffs.parquet")

    # ---- 3. Truth by horizon, two truth sources ----------------------------------------
    targets = (
        cutoffs.select("cutoff_id", "vessel_id", "observed_at")
        .join(pl.DataFrame({"horizon_h": HORIZONS_H}), how="cross")
        .with_columns(target_t=pl.col("observed_at") + pl.duration(hours=pl.col("horizon_h")))
        .drop("observed_at")
    )
    con.register("targets_df", targets.to_arrow())
    con.execute("CREATE TABLE targets AS SELECT * FROM targets_df ORDER BY vessel_id, target_t")
    con.execute(
        "CREATE TABLE obs_truth AS SELECT vessel_id, observed_at, latitude_deg, longitude_deg "
        f"FROM seg WHERE {truth_box.sql_predicate()} "
        "AND vessel_id IN (SELECT DISTINCT vessel_id FROM targets)"
    )
    con.execute(f"CREATE TABLE obs_cut AS SELECT * FROM obs_truth WHERE {in_cut}")
    print("truth matching ...", flush=True)
    rec = {}
    for src_name, tbl in [("truth_region", "obs_truth"), ("cutoff_region_only", "obs_cut")]:
        rec[src_name] = q(
            f"SELECT cutoff_id, horizon_h, evaluable, reason, truth_lat, truth_lon, "
            f"p_lat, p_lon, n_lat, n_lon FROM ({truth_sql('targets', tbl)})"
        )
    recs = (
        rec["truth_region"]
        .join(
            rec["cutoff_region_only"].select(
                "cutoff_id", "horizon_h", pl.col("evaluable").alias("evaluable_cut_only")
            ),
            on=["cutoff_id", "horizon_h"],
        )
        .join(cutoffs, on="cutoff_id")
        .sort("cutoff_id", "horizon_h")
    )

    print("baselines ...", flush=True)
    # ---- 4. Baseline errors -----------------------------------------------------------
    lat0, lon0 = recs["latitude_deg"].to_numpy(), recs["longitude_deg"].to_numpy()
    sog = recs["sog_knots"].fill_null(np.nan).to_numpy().astype(float)
    cog = recs["cog_deg"].fill_null(np.nan).to_numpy().astype(float)
    tl = recs["truth_lat"].fill_null(np.nan).to_numpy().astype(float)
    tn = recs["truth_lon"].fill_null(np.nan).to_numpy().astype(float)
    hz = recs["horizon_h"].to_numpy()
    p_lat, p_lon = persistence(lat0, lon0, [0.0])
    cv_lat, cv_lon = np.full(len(hz), np.nan), np.full(len(hz), np.nan)
    for h in HORIZONS_H:
        m = hz == h
        la, lo = constant_velocity(lat0[m], lon0[m], sog[m], cog[m], [h * 3600.0])
        cv_lat[m], cv_lon[m] = la[:, 0], lo[:, 0]
    # True course at target: bearing between the bracketing reports (NaN if they coincide).
    pb = recs.select(*(pl.col(c).fill_null(np.nan) for c in ["p_lat", "p_lon", "n_lat", "n_lon"]))
    d_br, true_course = inverse(*(pb[c].to_numpy().astype(float) for c in pb.columns))
    true_course = np.where(d_br > 1.0, true_course, np.nan)
    errs = {
        "persistence": distance_m(p_lat[:, 0], p_lon[:, 0], tl, tn),
        "constant_velocity": distance_m(cv_lat, cv_lon, tl, tn),
    }
    along, cross = along_cross_track(cv_lat, cv_lon, tl, tn, true_course)
    recs = recs.with_columns(
        err_persistence_m=pl.Series(errs["persistence"]),
        err_cv_m=pl.Series(errs["constant_velocity"]),
        cv_along_m=pl.Series(along),
        cv_cross_m=pl.Series(cross),
    ).with_columns(
        pl.when(pl.col("evaluable")).then(pl.col(c)).otherwise(None).fill_nan(None).alias(c)
        for c in ["err_persistence_m", "err_cv_m", "cv_along_m", "cv_cross_m"]
    )
    # Paired scoring: a sample where any model failed is dropped for all models and counted.
    recs = paired(recs, ["err_persistence_m", "err_cv_m"])
    recs.write_parquet(run_dir / "records.parquet")

    # ---- 5. Coverage, survivorship, admission -----------------------------------------
    coverage = (
        recs.group_by("motion_set", "horizon_h")
        .agg(
            n=pl.len(),
            frac_truth_region=pl.col("evaluable").mean(),
            frac_cutoff_region_only=pl.col("evaluable_cut_only").mean(),
            no_report_after=(pl.col("reason") == "no_report_after").fill_null(False).mean(),
            truth_gap=(pl.col("reason") == "truth_gap").fill_null(False).mean(),
        )
        .sort("motion_set", "horizon_h")
    )
    summary["coverage"] = coverage.to_dicts()
    summary["coverage_by_class_underway"] = (
        recs.filter(pl.col("motion_set") == "underway")
        .group_by("vessel_class", "horizon_h")
        .agg(
            n=pl.len(),
            frac_truth_region=pl.col("evaluable").mean(),
            frac_cutoff_region_only=pl.col("evaluable_cut_only").mean(),
        )
        .sort("vessel_class", "horizon_h")
        .to_dicts()
    )

    uw = recs.filter(pl.col("motion_set") == "underway")
    base = uw.filter(pl.col("horizon_h") == HORIZONS_H[0])
    base_sog = float(base["sog_mean_10m"].median())
    base_mix = (
        base.group_by("vessel_class").len().with_columns(share=pl.col("len") / pl.col("len").sum())
    )
    surv_rows, admission = [], []
    for h in HORIZONS_H:
        ev = uw.filter((pl.col("horizon_h") == h) & pl.col("evaluable"))
        frac = ev.height / max(base.height, 1)
        med_sog = float(ev["sog_mean_10m"].median()) if ev.height else float("nan")
        mix = (
            ev.group_by("vessel_class")
            .len()
            .with_columns(share=pl.col("len") / pl.col("len").sum())
        )
        shift = base_mix.join(mix, on="vessel_class", how="full", suffix="_ev").with_columns(
            pl.col("share").fill_null(0), pl.col("share_ev").fill_null(0)
        )
        max_shift_pp = float(((shift["share_ev"] - shift["share"]).abs().max() or 0) * 100)
        surv_rows.append(
            {
                "horizon_h": h,
                "evaluable": ev.height,
                "frac": frac,
                "median_sog_kn": med_sog,
                "class_share": {r["vessel_class"]: r["share"] for r in mix.to_dicts()},
                "max_class_shift_pp": max_shift_pp,
            }
        )
        ok_frac = frac >= ADMIT_MIN_FRACTION
        ok_sog = med_sog >= (1 - ADMIT_MAX_SOG_DROP) * base_sog
        ok_mix = max_shift_pp <= ADMIT_MAX_CLASS_SHIFT_PP
        admission.append(
            {
                "horizon_h": h,
                "status": "first_class" if (ok_frac and ok_sog and ok_mix) else "experimental",
                "coverage_ok": ok_frac,
                "speed_ok": ok_sog,
                "class_mix_ok": ok_mix,
            }
        )
    summary["survivorship"] = {"underway_median_sog_kn": base_sog, "by_horizon": surv_rows}
    summary["admission_rule"] = {
        "min_fraction": ADMIT_MIN_FRACTION,
        "max_median_sog_drop": ADMIT_MAX_SOG_DROP,
        "max_class_shift_pp": ADMIT_MAX_CLASS_SHIFT_PP,
    }
    summary["admission_pooled"] = admission

    # Per-stratum admission (build plan v0.3 §6.4): motion set x class, plus the transiting
    # stratum and the pooled underway set for reference.
    strat = pl.concat(
        [
            recs.with_columns(stratum=pl.col("motion_set") + ":" + pl.col("vessel_class")),
            recs.filter(pl.col("motion_set") == "underway").with_columns(
                stratum=pl.lit("underway:ALL")
            ),
            recs.filter(pl.col("transiting")).with_columns(stratum=pl.lit("underway:TRANSITING")),
        ]
    )
    summary["admission_config"] = DEFAULT_ADMISSION.as_dict()
    summary["admission"] = admit_by_stratum(strat).to_dicts()

    # ---- 6. Baseline error tables -----------------------------------------------------
    err_rows = []
    for (ms, h, cls), g in (
        pl.concat(
            [
                recs,
                recs.with_columns(vessel_class=pl.lit("ALL")),
                recs.filter(pl.col("transiting")).with_columns(vessel_class=pl.lit("TRANSITING")),
            ]
        )
        .filter(pl.col("evaluable"))
        .group_by("motion_set", "horizon_h", "vessel_class")
    ):
        pg = g.filter(pl.col("paired"))
        if pg.height < 20:
            continue
        row = {
            "motion_set": ms,
            "horizon_h": h,
            "vessel_class": cls,
            "n_evaluable": g.height,
            "n": pg.height,  # paired: identical samples for every model
            "vessels": pg["vessel_id"].n_unique(),
            "failed": {
                "persistence": int(g["err_persistence_m_failed"].sum()),
                "cv": int(g["err_cv_m_failed"].sum()),
            },
        }
        ids = pg["vessel_id"].to_numpy()
        for col, name in [("err_persistence_m", "persistence"), ("err_cv_m", "cv")]:
            v = pg[col].to_numpy().astype(float) / 1000
            assert not np.isnan(v).any(), "paired set must contain no failures"
            for stat_name, fn in [("mean", np.mean), ("median", np.median)]:
                est, lo, hi = vessel_bootstrap_ci(v, ids, fn, n_boot=args.n_boot)
                row[f"{name}_{stat_name}_km"] = [est, lo, hi]
            row[f"{name}_p90_km"] = float(np.quantile(v, 0.9))
        # Along/cross-track is undefined where the true course is (truth stationary): nan-aware.
        row["cv_abs_along_median_km"] = float(np.nanmedian(np.abs(pg["cv_along_m"])) / 1000)
        row["cv_abs_cross_median_km"] = float(np.nanmedian(np.abs(pg["cv_cross_m"])) / 1000)
        err_rows.append(row)
    err_rows.sort(
        key=lambda r: (
            r["motion_set"],
            r["vessel_class"] != "ALL",
            r["vessel_class"] != "TRANSITING",
            r["vessel_class"],
            r["horizon_h"],
        )
    )
    summary["baseline_errors"] = err_rows

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (run_dir / "report.md").write_text(render_report(summary, run_dir.name))
    print(f"run: {run_dir}")


def render_report(s: dict, run_id: str) -> str:
    L = [
        f"# Coverage audit run `{run_id}`",
        "",
        f"Dataset `{s['dataset_id']}`, span "
        f"{s['data_span'][0]} – {s['data_span'][1]}, git `{s['git_sha']}`.",
        "",
    ]
    d = s["descriptive"]
    L += [
        "## Totals",
        "",
        f"- Cutoff region: {d['totals_cutoff_region']['reports']:,} reports, "
        f"{d['totals_cutoff_region']['vessels']:,} vessels",
        f"- Truth region: {d['totals_truth_region']['reports']:,} reports, "
        f"{d['totals_truth_region']['vessels']:,} vessels",
        "",
    ]
    L += [
        f"## Feed outages (hours < 25% of median {d['median_reports_per_hour']:,.0f} reports/h)",
        "",
    ]
    L += [
        f"- {r['hour']}: {r['n']:,} reports ({pct(r['frac_of_median'])})" for r in d["outage_hours"]
    ] or ["- none"]
    L += [
        "",
        "## Inter-report interval (cutoff region, seconds)",
        "",
        "| class | n | p10 | p50 | p90 | p99 | >30 min |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in d["interval_quantiles_s"]:
        L.append(
            f"| {r['vessel_class'] or 'ALL'} | {r['n']:,} | {r['p10']:.0f} | {r['p50']:.0f} "
            f"| {r['p90']:.0f} | {r['p99']:.0f} | {pct(r['frac_gt_30min'])} |"
        )
    L += ["", "## Class mix (cutoff region)", "", "| class | vessels | reports |", "|---|---|---|"]
    for r in d["class_mix"]:
        L.append(f"| {r['vessel_class']} | {r['vessels']:,} | {r['reports']:,} |")
    L += [
        "",
        "## Motion-state mix of reports (cutoff region, trailing 10-min mean SOG)",
        "",
        "| class | stationary <0.5 kn | slow 0.5–2 | underway ≥2 | unknown | reports |",
        "|---|---|---|---|---|---|",
    ]
    for r in d["motion_mix_reports"]:
        L.append(
            f"| {r['vessel_class'] or 'ALL'} | {pct(r['stationary'])} | {pct(r['slow'])} | "
            f"{pct(r['underway'])} | {pct(r['unknown'])} | {r['reports']:,} |"
        )
    L += [
        "",
        "## Ground-truth coverage by horizon",
        "",
        "| set | h | cutoffs | valid truth (Gulf) | valid truth (cutoff box only) | "
        "no report after target | gap at target |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in s["coverage"]:
        L.append(
            f"| {r['motion_set']} | {r['horizon_h']} | {r['n']:,} | "
            f"{pct(r['frac_truth_region'])} | {pct(r['frac_cutoff_region_only'])} | "
            f"{pct(r['no_report_after'])} | {pct(r['truth_gap'])} |"
        )
    L += [
        "",
        "### Underway coverage by class (Gulf truth / cutoff-box-only truth)",
        "",
        "| class | " + " | ".join(f"{h} h" for h in s["horizons_h"]) + " |",
        "|---|" + "---|" * len(s["horizons_h"]),
    ]
    by_cls: dict = {}
    for r in s["coverage_by_class_underway"]:
        by_cls.setdefault(r["vessel_class"], {})[r["horizon_h"]] = r
    for cls, hs in sorted(by_cls.items(), key=lambda kv: -kv[1][s["horizons_h"][0]]["n"]):
        cells = [
            f"{pct(hs[h]['frac_truth_region'])} / {pct(hs[h]['frac_cutoff_region_only'])}"
            if h in hs
            else "–"
            for h in s["horizons_h"]
        ]
        L.append(f"| {cls} (n={hs[s['horizons_h'][0]]['n']}) | " + " | ".join(cells) + " |")
    sv = s["survivorship"]
    L += [
        "",
        f"## Survivorship (underway; population median SOG {sv['underway_median_sog_kn']:.1f} kn)",
        "",
        "| h | evaluable | median SOG kn | max class-share shift | class shares |",
        "|---|---|---|---|---|",
    ]
    for r in sv["by_horizon"]:
        shares = ", ".join(
            f"{k} {100 * v:.0f}%"
            for k, v in sorted(r["class_share"].items(), key=lambda kv: -kv[1])[:5]
        )
        L.append(
            f"| {r['horizon_h']} | {r['evaluable']:,} ({pct(r['frac'])}) | "
            f"{r['median_sog_kn']:.1f} | {r['max_class_shift_pp']:.1f} pp | {shares} |"
        )
    L += [
        "",
        "## Horizon admission per stratum (build plan v0.3 §6.4)",
        "",
        "`first` = first-class. Otherwise experimental, with the failing checks: "
        "`cov` coverage < 50%, `spd` evaluable mean SOG > 25% below the stratum's, "
        "`n` fewer than 100 evaluable cutoffs or 20 vessels. Cells show the valid-truth share.",
        "",
        "| stratum | cutoffs | " + " | ".join(f"{h} h" for h in s["horizons_h"]) + " |",
        "|---|---|" + "---|" * len(s["horizons_h"]),
    ]
    by_stratum: dict = {}
    for r in s["admission"]:
        by_stratum.setdefault(r["stratum"], {})[r["horizon_h"]] = r

    def adm_cell(r: dict) -> str:
        if r["status"] == "first_class":
            return f"first {pct(r['frac'])}"
        fails = [
            k
            for k, ok in [("cov", r["coverage_ok"]), ("spd", r["speed_ok"]), ("n", r["size_ok"])]
            if not ok
        ]
        return f"exp ({','.join(fails)}) {pct(r['frac'])}"

    order = sorted(
        by_stratum, key=lambda k: (not k.endswith(":ALL"), not k.endswith(":TRANSITING"), k)
    )
    for k in order:
        hs = by_stratum[k]
        n = next(iter(hs.values()))["n_cutoffs"]
        L.append(
            f"| {k} | {n:,} | "
            + " | ".join(adm_cell(hs[h]) if h in hs else "–" for h in s["horizons_h"])
            + " |"
        )
    L += [
        "",
        "Pooled rule (v0.2, for reference only): "
        + ", ".join(f"{r['horizon_h']} h {r['status']}" for r in s["admission_pooled"]),
    ]
    L += [
        "",
        "## Baseline errors (km; mean/median with 95% vessel-bootstrap CI)",
        "",
        "Paired: every model is scored on the same samples. `CV failed` = evaluable samples "
        "where CV could not predict (missing COG/SOG); they are excluded for all models.",
        "",
        "| set | class | h | n paired | CV failed | vessels | persistence median | CV median | "
        "CV mean | CV p90 | CV |along| med | CV |cross| med |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ci = lambda x: f"{x[0]:.2f} [{x[1]:.2f}, {x[2]:.2f}]"  # noqa: E731
    for r in s["baseline_errors"]:
        if r["vessel_class"] not in ("ALL", "TRANSITING") and r["horizon_h"] not in (1, 3, 6):
            continue
        L.append(
            f"| {r['motion_set']} | {r['vessel_class']} | {r['horizon_h']} | {r['n']:,} | "
            f"{r['failed']['cv']:,} | {r['vessels']} | {ci(r['persistence_median_km'])} | "
            f"{ci(r['cv_median_km'])} | "
            f"{ci(r['cv_mean_km'])} | {r['cv_p90_km']:.2f} | {r['cv_abs_along_median_km']:.2f}"
            f" | {r['cv_abs_cross_median_km']:.2f} |"
        )
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
