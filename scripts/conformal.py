"""Split-conformal radii (build plan §5 U1, Milestone 4): fit on a calibration-split benchmark run,
measure empirical coverage on a held-out test run's records.

Fitting reads only the calibration run. Coverage reads the test run's stored records: no model
is re-run and nothing is scored again, so it is not another look at the test set beyond the
recorded one (the test run must be in the held-out access ledger).

Outputs
    data/model_params/conformal/<protocol>__<dataset>.json     radii + provenance (committed)
    data/runs/conformal/<test_run_id>/{coverage.parquet, report.md, conformal.json}

Usage:
    uv run --project backend python scripts/conformal.py --protocol P-known
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from maritime_forecast.config import DATA_DIR, RUNS_DIR
from maritime_forecast.evaluation import conformal as cf
from maritime_forecast.evaluation.holdout import config_sha, read_ledger, record_analysis
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.run_store import RunStore

DATASET = "noaa_us_gulf_2023-06-07"


def _latest(protocol: str, dataset_id: str, split: str) -> str:
    runs = RunStore()
    names = sorted(p.name for p in (RUNS_DIR / "benchmarks").glob(f"{protocol}_{dataset_id}_*")
                   if (p / "records.parquet").exists())  # fmt: skip
    for n in reversed(names):
        if runs.load(n).sampler.get("split") == split:
            return n
    raise SystemExit(f"no {split} run for {protocol} on {dataset_id}")


def _digest(run_id: str) -> str:
    return hashlib.sha256(
        (RUNS_DIR / "benchmarks" / run_id / "files.json").read_bytes()
    ).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--protocol", required=True, choices=["P-known", "P-unseen"])
    ap.add_argument("--dataset-id", default=DATASET)
    ap.add_argument("--calibration-run", default=None, help="default: latest calibration run")
    ap.add_argument("--test-run", default=None, help="default: the ledger's run for the protocol")
    ap.add_argument(
        "--params-only",
        action="store_true",
        help="refit and write the radii only; read no test records (no analysis)",
    )
    ap.add_argument(
        "--bins", choices=["stratum", "h3"], default="stratum",
        help="h3: Mondrian bins motion x H3 cell (res 5) before the stratum chain (slice 2, "
        "pre-registered; adoption rule applied against the latest stratum analysis)",
    )  # fmt: skip
    args = ap.parse_args()
    h3 = args.bins == "h3"
    runs = RunStore()
    cal_id = args.calibration_run or _latest(args.protocol, args.dataset_id, "calibration")
    ledger = [e for e in read_ledger(args.dataset_id)
              if e["protocol"] == args.protocol and e.get("kind") is None]  # fmt: skip
    test_id = args.test_run or (ledger[-1]["run_id"] if ledger else None)
    if test_id is None or test_id not in {e["run_id"] for e in ledger}:
        raise SystemExit("the test run must be a recorded held-out run (test-access ledger)")
    cal, test = runs.load(cal_id), runs.load(test_id)
    man = SplitManifest.model_validate_json(Path(cal.sampler["manifest_path"]).read_text())
    if cal.sampler.get("split") != "calibration" or test.sampler.get("split") != "test":
        raise SystemExit("need a calibration-split run and a test-split run")
    if cal.protocol.manifest_sha256 != test.protocol.manifest_sha256:
        raise SystemExit("calibration and test runs use different split manifests")
    cal_models = {m.model_id: (m.model_version, m.config) for m in cal.models}
    test_models = {m.model_id: (m.model_version, m.config) for m in test.models}
    if any(test_models.get(k) != v for k, v in cal_models.items()):
        raise SystemExit("model versions or configs differ between the calibration and test runs")

    # The radii describe forecasts requested as the calibration run requested them (longest
    # horizon, step, history); coverage on test is only meaningful for the same request.
    def request(run):
        return (max(run.sampler["horizons_h"]), run.sampler["step_minutes"],
                run.sampler["history_hours"])  # fmt: skip

    if request(cal) != request(test):
        raise SystemExit(f"calibration request {request(cal)} differs from the test run's "
                         f"{request(test)} (longest horizon h, step min, history h)")  # fmt: skip
    sha = git_sha()
    if sha.endswith("-dirty") or sha == "unknown":
        raise SystemExit("run from a clean, committed tree (the outputs are stamped with its SHA)")

    cfg = cf.ConformalConfig()
    cal_records = runs.table(cal_id, "records")
    if h3:
        cal_records = cf.add_h3(cal_records, runs.table(cal_id, "cutoffs"))
    radii = cf.fit_radii(cal_records, cfg, h3=h3)
    suffix = f"__h3r{cf.H3_RES}" if h3 else ""
    out_params = (
        DATA_DIR / "model_params" / "conformal"
        / f"{args.protocol}__{args.dataset_id}{suffix}.json"
    )  # fmt: skip
    out_params.parent.mkdir(parents=True, exist_ok=True)
    out_params.write_text(json.dumps({
        "kind": "conformal_radius", "protocol": args.protocol, "dataset_id": args.dataset_id,
        "bins": f"h3r{cf.H3_RES}" if h3 else "stratum",
        "calibration_set_id": cal_id, "calibration_files_json_sha256": _digest(cal_id),
        "manifest_sha256": cal.protocol.manifest_sha256, "config": cfg.as_dict(),
        "models": {m.model_id: {"model_version": m.model_version,
                                "config_sha256": config_sha(m.config)} for m in cal.models},
        # Calibration labels reach the calibration end plus the truth bracket: an as_of before
        # this would be served radii fitted on its own future.
        "label_reach_end": (
            max(p.end for p in man.periods if p.split == "calibration")
            + timedelta(minutes=cal.sampler["max_bracket_gap_minutes"])
        ).isoformat(),
        "class_source": "backfilled (NOAA; not as-of)",
        # The request the calibration errors came from. The runner predicts once at its longest
        # horizon and scores shorter ones on that path, and some models (retrieval) depend on
        # the requested horizon: the API serves radii only for forecasts made the same way.
        "request": {"horizon_h": max(cal.sampler["horizons_h"]),
                    "step_minutes": cal.sampler["step_minutes"],
                    "history_hours": cal.sampler["history_hours"]},
        "radii": radii.to_dicts(), "git_sha": sha,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=1) + "\n")  # fmt: skip

    if args.params_only:
        print(f"{args.protocol}: radii -> {out_params} (params only; no test records read)")
        return
    kind = "conformal_coverage_h3" if h3 else "conformal_coverage"
    hashed = {**cfg.as_dict(), "bins": f"h3r{cf.H3_RES}" if h3 else "stratum"}
    # On record BEFORE the test records are read, so a crash can never leave a look unrecorded.
    record_analysis(args.dataset_id, test_id, f"{kind}_started", hashed, sha, "(started)")
    test_records = runs.table(test_id, "records")
    if h3:
        test_records = cf.add_h3(test_records, runs.table(test_id, "cutoffs"))
    failed = cf.failures_by_cell(test_records)
    tables = []
    for level in cfg.levels:
        a = cf.assign_radii(test_records, radii, level, h3=h3)
        week = (pl.col("observed_at").dt.ordinal_day() - 183) // 7  # Jul 2 = day 183 -> week 0
        a = a.with_columns(week=week)
        g = pl.concat([
            a.with_columns(group=pl.col("motion_set") + ":ALL"),
            a.filter(pl.col("transiting")).with_columns(group=pl.lit("underway:TRANSITING")),
            a.with_columns(group=pl.col("s_class")),
            a.filter(pl.col("motion_set") == "underway").with_columns(
                group=pl.lit("underway:ALL:week") + pl.col("week").cast(pl.Utf8)),
        ])  # fmt: skip
        tables.append(cf.coverage_table(g, level, ["group"], cfg))
    cov = (
        pl.concat(tables)
        .join(failed, on=["model_id", "horizon_h"], how="left")
        .with_columns(pl.col("n_model_failed").fill_null(0))
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / "conformal" / test_id / stamp  # never overwrites an earlier analysis
    out.mkdir(parents=True, exist_ok=False)
    cov.write_parquet(out / "coverage.parquet")
    headline = ["underway:ALL", "stationary:ALL", "ALL"]
    cal_n = radii.filter((pl.col("level") == cf.PRIMARY_LEVEL)
                         & pl.col("stratum").is_in(headline))  # fmt: skip
    (out / "report.md").write_text(render(args.protocol, cal_id, test_id, cov, cfg, cal_n))
    (out / "conformal.json").write_text(json.dumps({
        "calibration_run": cal_id, "test_run": test_id, "params": str(out_params),
        "test_files_json_sha256": _digest(test_id), "config": cfg.as_dict(),
        "bins": f"h3r{cf.H3_RES}" if h3 else "stratum",
        "adoption": adoption(test_id, cov) if h3 else None,
        "git_sha": sha, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=2) + "\n")  # fmt: skip
    record_analysis(args.dataset_id, test_id, kind, hashed, sha, str(out))
    if h3:
        print(json.dumps(adoption(test_id, cov), indent=1))
    # Headline count over non-overlapping cells: motion:ALL groups at the primary level.
    judged = cov.filter(pl.col("calibrated").is_not_null() & pl.col("group").str.ends_with(":ALL")
                        & (pl.col("level") == cf.PRIMARY_LEVEL))  # fmt: skip
    ok = int(judged["calibrated"].sum())
    print(f"{args.protocol}: radii -> {out_params}; coverage -> {out}/report.md; "
          f"{ok}/{judged.height} motion:ALL cells at {cf.PRIMARY_LEVEL:.0%} within "
          f"±{cfg.tolerance:.0%}")  # fmt: skip


def adoption(test_id: str, cov_h3: pl.DataFrame) -> dict:
    """The pre-registered rule (implementation log, 2026-10-21), against the latest stratum
    (slice 1) analysis of the same test run: at 90%, at least as many motion:ALL cells within
    tolerance, and v2's mean underway:ALL area smaller at >= 4 of the 6 horizons. Adoption
    needs this to pass for both protocols (checked by the caller across runs)."""
    base = RUNS_DIR / "conformal" / test_id
    slice1 = sorted(d for d in base.glob("2026*")  # complete analyses only
                    if (d / "conformal.json").exists()
                    and json.loads((d / "conformal.json").read_text()).get("bins", "stratum")
                    == "stratum")  # fmt: skip
    if not slice1:
        raise SystemExit("no stratum (slice 1) analysis of this test run to compare against")
    cov_s1 = pl.read_parquet(slice1[-1] / "coverage.parquet")

    def count(c):
        x = c.filter((pl.col("level") == cf.PRIMARY_LEVEL) & pl.col("group").str.ends_with(":ALL")
                     & pl.col("calibrated").is_not_null())  # fmt: skip
        return int(x["calibrated"].sum()), x.height

    def areas(c):
        x = c.filter((pl.col("level") == cf.PRIMARY_LEVEL) & (pl.col("group") == "underway:ALL")
                     & (pl.col("model_id") == "route_retrieval_v2")).sort("horizon_h")  # fmt: skip
        return dict(zip(x["horizon_h"], x["mean_area_km2"], strict=True))

    (k1, n1), (k3, n3) = count(cov_s1), count(cov_h3)
    a1, a3 = areas(cov_s1), areas(cov_h3)
    wins = [h for h in a1 if a3.get(h) is not None and a3[h] < a1[h]]
    return {
        "slice1_analysis": str(slice1[-1]),
        "calibrated_cells_slice1": f"{k1}/{n1}",
        "calibrated_cells_h3": f"{k3}/{n3}",
        "coverage_ok": k3 >= k1,
        "v2_underway_area_km2_slice1": a1,
        "v2_underway_area_km2_h3": a3,
        "area_smaller_at_horizons": wins,
        "area_ok": len(wins) >= 4,
        "passes": k3 >= k1 and len(wins) >= 4,
    }


def render(protocol, cal_id, test_id, cov: pl.DataFrame, cfg, cal_n: pl.DataFrame) -> str:
    L = [f"# Conformal coverage: {protocol}", ""]
    if protocol == "P-unseen":
        L += ["**P-unseen radii come from a 5% vessel bucket** (the calibration split's vessels); "
              "strata are thin and often fall back, so under-coverage is expected and observed. "
              "They are not fitted on the P-known calibration run, which contains P-unseen test "
              "vessels.", ""]  # fmt: skip
    L += [
         f"Radii fitted on calibration run `{cal_id}`; coverage on held-out test run `{test_id}` "
         "(stored records, nothing re-scored).", "",
         f"Nominal levels {list(cfg.levels)}; strata motion x class with fallback to motion:ALL "
         f"then ALL below {cfg.min_n} calibration errors. `calibrated`: |coverage - level| <= "
         f"{cfg.tolerance:.0%}, judged on cells with n >= {cfg.tolerance_min_n}. CIs resample "
         "vessels; `CI∋level` says whether the vessel CI contains the level (descriptive; the "
         "verdict rule above was fixed before test coverage was seen). Area: mean disc area of "
         "the applied radius. `failed`: test records with truth where the model gave no "
         "forecast (not in coverage). Strata use NOAA back-filled vessel class (not as-of). "
         "Per-class strata are in coverage.parquet.", "",
         "Calibration sizes (90% radii rows): "
         + "; ".join(f"{r['model_id']} {r['horizon_h']:g} h {r['stratum']}: n={r['n']}, "
                     f"{r['n_vessels']} vessels"
                     for r in cal_n.filter(pl.col("model_id") == "route_retrieval_v2")
                     .sort("stratum", "horizon_h").iter_rows(named=True)), ""]  # fmt: skip
    for level in cfg.levels:
        L += [f"## Level {level:.0%}", "",
              "| model | h | group | n | vessels | coverage [95% CI] | median r km | "
              "mean area km² | fallback | failed | calibrated | CI∋level |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        shown = (pl.col("group").str.ends_with(":ALL") | (pl.col("group") == "underway:TRANSITING")
                 | pl.col("group").str.contains("week"))  # fmt: skip
        sub = cov.filter((pl.col("level") == level) & shown)
        for r in sub.sort("group", "model_id", "horizon_h").iter_rows(named=True):
            cal = {True: "yes", False: "**no**", None: "—"}[r["calibrated"]]
            L.append(
                f"| {r['model_id']} | {r['horizon_h']:g} | {r['group']} | {r['n']:,} | "
                f"{r['n_vessels']:,} | {r['coverage']:.3f} [{r['ci_low']:.3f}, {r['ci_high']:.3f}] "
                f"| {r['median_radius_km']:.2f} | {r['mean_area_km2']:.1f} | "
                f"{r['fallback_share']:.0%} | {r['n_model_failed']} | {cal} | "
                f"{'yes' if r['ci_contains_level'] else 'no'} |"
            )
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
