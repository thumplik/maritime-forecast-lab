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
from datetime import UTC, datetime

import polars as pl

from maritime_forecast.config import DATA_DIR, RUNS_DIR
from maritime_forecast.evaluation import conformal as cf
from maritime_forecast.evaluation.holdout import read_ledger, record_analysis
from maritime_forecast.provenance import git_sha
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
    args = ap.parse_args()
    runs = RunStore()
    cal_id = args.calibration_run or _latest(args.protocol, args.dataset_id, "calibration")
    ledger = [e for e in read_ledger(args.dataset_id)
              if e["protocol"] == args.protocol and e.get("kind") is None]  # fmt: skip
    test_id = args.test_run or (ledger[-1]["run_id"] if ledger else None)
    if test_id is None or test_id not in {e["run_id"] for e in ledger}:
        raise SystemExit("the test run must be a recorded held-out run (test-access ledger)")
    cal, test = runs.load(cal_id), runs.load(test_id)
    if cal.sampler.get("split") != "calibration" or test.sampler.get("split") != "test":
        raise SystemExit("need a calibration-split run and a test-split run")
    if cal.protocol.manifest_sha256 != test.protocol.manifest_sha256:
        raise SystemExit("calibration and test runs use different split manifests")
    cal_models = {m.model_id: (m.model_version, m.config) for m in cal.models}
    test_models = {m.model_id: (m.model_version, m.config) for m in test.models}
    if any(test_models.get(k) != v for k, v in cal_models.items()):
        raise SystemExit("model versions or configs differ between the calibration and test runs")
    sha = git_sha()
    if sha.endswith("-dirty") or sha == "unknown":
        raise SystemExit("run from a clean, committed tree (the outputs are stamped with its SHA)")

    cfg = cf.ConformalConfig()
    radii = cf.fit_radii(runs.table(cal_id, "records"), cfg)
    out_params = (
        DATA_DIR / "model_params" / "conformal" / f"{args.protocol}__{args.dataset_id}.json"
    )
    out_params.parent.mkdir(parents=True, exist_ok=True)
    out_params.write_text(json.dumps({
        "kind": "conformal_radius", "protocol": args.protocol, "dataset_id": args.dataset_id,
        "calibration_set_id": cal_id, "calibration_files_json_sha256": _digest(cal_id),
        "manifest_sha256": cal.protocol.manifest_sha256, "config": cfg.as_dict(),
        "models": {m.model_id: {"model_version": m.model_version} for m in cal.models},
        "class_source": "backfilled (NOAA; not as-of)",
        "radii": radii.to_dicts(), "git_sha": sha,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=1) + "\n")  # fmt: skip

    test_records = runs.table(test_id, "records")
    failed = cf.failures_by_cell(test_records)
    tables = []
    for level in cfg.levels:
        a = cf.assign_radii(test_records, radii, level)
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
        "git_sha": sha, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=2) + "\n")  # fmt: skip
    record_analysis(args.dataset_id, test_id, "conformal_coverage", cfg.as_dict(), sha, str(out))
    # Headline count over non-overlapping cells: motion:ALL groups at the primary level.
    judged = cov.filter(pl.col("calibrated").is_not_null() & pl.col("group").str.ends_with(":ALL")
                        & (pl.col("level") == cf.PRIMARY_LEVEL))  # fmt: skip
    ok = int(judged["calibrated"].sum())
    print(f"{args.protocol}: radii -> {out_params}; coverage -> {out}/report.md; "
          f"{ok}/{judged.height} motion:ALL cells at {cf.PRIMARY_LEVEL:.0%} within "
          f"±{cfg.tolerance:.0%}")  # fmt: skip


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
