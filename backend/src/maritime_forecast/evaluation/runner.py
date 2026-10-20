"""Evaluator runner (build plan §6.1-§6.5): an unattended, paired benchmark of registered models.

Steps
  1. Cutoff candidates in the cutoff region from CAUSAL features (data.cutoffs), restricted
     to windows the protocol manifest puts wholly in the requested split (window_split).
  2. Underway and stationary cutoff sets via the seeded sampler; transiting flag.
  3. Each model predicts from batched causal histories (models.base.histories_for_cutoffs).
  4. Truth at every grid step from the truth region (evaluation.truth); never bridged.
  5. Paired scoring: a (cutoff, horizon) sample counts only if truth is evaluable and EVERY
     model produced a forecast; failures are counted per model, never silently dropped.
  6. Per (model, horizon, stratum) cell: median/mean error with paired vessel-bootstrap CIs,
     p90, |along|/|cross|-track medians, ADE, failure rate, per-stratum admission (§6.4).
  7. Everything is written to the immutable run store as one BenchmarkRun.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from maritime_forecast.config import default_region, region
from maritime_forecast.data.cutoffs import (
    SamplerConfig,
    cutoff_candidates_sql,
    sample_cutoffs,
    transiting_expr,
)
from maritime_forecast.data.segment import segmented_sql
from maritime_forecast.evaluation.admission import DEFAULT_ADMISSION, admit_by_stratum
from maritime_forecast.evaluation.bootstrap import paired_vessel_bootstrap
from maritime_forecast.evaluation.truth import truth_sql
from maritime_forecast.geo.geodesic import along_cross_track, distance_m, inverse
from maritime_forecast.models.base import ForecastHorizon, histories_for_cutoffs
from maritime_forecast.models.registry import get_model, model_ids
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.benchmark import (
    HEADLINE_MAX_FAILURE_RATE,
    BenchmarkRun,
    CellResult,
    DroppedCell,
    Stat,
)
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore

RUNNER_VERSION = "runner-v1"
BASELINES = ("persistence_v1", "constant_velocity_v1", "damped_velocity_v1")
# Skill is undefined when the best baseline is ~exact (e.g. persistence on moored vessels).
MIN_SKILL_BASELINE_M = 10.0


@dataclass(frozen=True)
class BenchmarkConfig:
    dataset_id: str
    manifest_path: Path
    model_ids: tuple[str, ...] = ()
    horizons_h: tuple[float, ...] = (1, 2, 3, 6, 12, 24)
    step_minutes: int = 10
    history_hours: float = 24
    split: str = "test"
    cutoff_region: str = "houston_galveston"
    seed: int = 20230601
    n_boot: int = 1000
    max_bracket_gap_minutes: float = 30
    max_cutoffs_per_set: int | None = None  # for smoke runs; None = all sampled cutoffs
    min_cell_samples: int = 20
    samplers: dict[str, SamplerConfig] = field(
        default_factory=lambda: {
            "underway": SamplerConfig(motion_set="underway"),
            "stationary": SamplerConfig(motion_set="stationary"),
        }
    )

    def models(self) -> tuple[str, ...]:
        return self.model_ids or tuple(model_ids())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["manifest_path"] = str(self.manifest_path)
        d["model_ids"] = list(self.models())
        d["samplers"] = {k: v.as_dict() for k, v in self.samplers.items()}
        return d


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _lit(t: datetime) -> str:
    return f"TIMESTAMPTZ '{t.isoformat()}'"


def select_cutoffs(
    cfg: BenchmarkConfig, store: ParquetStore, manifest: SplitManifest
) -> pl.DataFrame:
    """Sampled cutoffs whose whole [as_of - history, as_of + max horizon] lies in the split."""
    periods = [p for p in manifest.periods or [] if p.split == cfg.split]
    if not periods:
        raise ValueError(f"manifest has no {cfg.split!r} period")
    lo = min(p.start for p in periods)
    hi = max(p.end for p in periods)
    max_h = timedelta(hours=max(cfg.horizons_h))
    box = region(cfg.cutoff_region)
    files = store.files(cfg.dataset_id, lo - timedelta(hours=3), hi)
    src = "read_parquet([" + ", ".join(f"'{f}'" for f in files) + "], hive_partitioning=false)"
    cand = store.con.execute(
        cutoff_candidates_sql(src, box.sql_predicate(), _lit(lo), _lit(hi))
    ).pl()
    hist = timedelta(hours=cfg.history_hours)
    keep = [
        manifest.window_split(v, t - hist, t + max_h) == cfg.split
        for v, t in zip(cand["vessel_id"], cand["observed_at"], strict=True)
    ]
    cand = cand.filter(pl.Series(keep, dtype=pl.Boolean))
    frames = []
    for name, sc in cfg.samplers.items():
        s = sample_cutoffs(cand, SamplerConfig(**{**asdict(sc), "seed": cfg.seed}))
        if cfg.max_cutoffs_per_set is not None and s.height > cfg.max_cutoffs_per_set:
            s = s.sample(cfg.max_cutoffs_per_set, seed=cfg.seed).sort("vessel_id", "observed_at")
        frames.append(s.with_columns(motion_set=pl.lit(name)))
    cutoffs = pl.concat(frames).sort("motion_set", "vessel_id", "observed_at")
    return cutoffs.with_row_index("cutoff_id").with_columns(
        transiting=(pl.col("motion_set") == "underway") & transiting_expr()
    )


def predict_all(cfg, store, cutoffs, grid_steps):
    """Per model: (status[N], error_code[N], lat[N,S], lon[N,S], warnings count, seconds)."""
    horizon_h = max(cfg.horizons_h)
    n, s = cutoffs.height, grid_steps
    out = {}
    for m in cfg.models():
        model = get_model(m, store, cfg.dataset_id)
        status = np.empty(n, dtype=object)
        code = np.full(n, None, dtype=object)
        lat = np.full((n, s), np.nan)
        lon = np.full((n, s), np.nan)
        t0 = time.perf_counter()
        for cid, h in histories_for_cutoffs(store, cfg.dataset_id, cutoffs, cfg.history_hours):
            f = model.predict(h, ForecastHorizon(h.as_of, horizon_h, cfg.step_minutes))
            status[cid] = f.status
            if f.status == "ok":
                lat[cid] = [p.latitude_deg for p in f.points]
                lon[cid] = [p.longitude_deg for p in f.points]
            else:
                code[cid] = f.error.code if f.error else f.status
        out[m] = {
            "status": status,
            "code": code,
            "lat": lat,
            "lon": lon,
            "seconds": time.perf_counter() - t0,
        }
    return out


def truth_grid(cfg, store, cutoffs, grid_steps):
    """Truth for every (cutoff, step): arrays [N,S] of lat, lon, course, evaluable, reason."""
    step = timedelta(minutes=cfg.step_minutes)
    targets = (
        cutoffs.select("cutoff_id", "vessel_id", "observed_at")
        .join(pl.DataFrame({"k": np.arange(1, grid_steps + 1)}), how="cross")
        .with_columns(target_t=pl.col("observed_at") + pl.col("k") * step)
        .drop("observed_at")
    )
    lo = cutoffs["observed_at"].min()
    hi = cutoffs["observed_at"].max() + timedelta(hours=max(cfg.horizons_h), minutes=60)
    con = store.con
    obs = store.scan(cfg.dataset_id, lo, hi, vessel_ids=cutoffs["vessel_id"].unique().to_list())
    con.register("_bench_obs", obs.to_arrow())
    truth_box = default_region("truth_region")  # truth only from inside the truth region
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _bench_truth_obs AS SELECT vessel_id, observed_at, "
        f"latitude_deg, longitude_deg, cog_deg, sog_knots FROM ({segmented_sql('_bench_obs')}) "
        f"WHERE {truth_box.sql_predicate()}"
    )
    con.register("_bench_targets", targets.to_arrow())
    tsql = truth_sql(
        "_bench_targets",
        "_bench_truth_obs",
        cfg.max_bracket_gap_minutes,
        prev_cols=("cog_deg", "sog_knots"),
    )
    t = con.execute(
        "SELECT cutoff_id, k, evaluable, reason, truth_lat, truth_lon, p_lat, p_lon, n_lat, "
        f"n_lon, bracket_gap_s, p_cog_deg, p_sog_knots FROM ({tsql}) ORDER BY cutoff_id, k"
    ).pl()
    con.unregister("_bench_obs")
    con.unregister("_bench_targets")
    n = cutoffs.height
    shape = (n, grid_steps)
    f = lambda c: t[c].fill_null(np.nan).to_numpy().astype(float).reshape(shape)  # noqa: E731
    d, course = inverse(f("p_lat"), f("p_lon"), f("n_lat"), f("n_lon"))
    # True course for along/cross-track: the bracket's bearing when the reports differ; when a
    # report falls exactly on the target time (bracket of one report), that report's COG if the
    # vessel is moving. A vessel stationary at the target has no defined course (NaN).
    exact = f("bracket_gap_s") == 0
    moving_cog = np.where(f("p_sog_knots") >= 0.5, f("p_cog_deg"), np.nan)
    true_course = np.where(d > 1.0, course, np.where(exact, moving_cog, np.nan))
    return {
        "lat": f("truth_lat"),
        "lon": f("truth_lon"),
        "course": true_course,
        "evaluable": t["evaluable"].fill_null(False).to_numpy().reshape(shape),
        "reason": t["reason"].to_numpy().reshape(shape),
    }


def _finite_or_none(x: float) -> float | None:
    """NaN (e.g. along-track with no defined true course) is recorded as null, not NaN."""
    return None if x is None or not np.isfinite(x) else float(x)


def _nanmedian(a: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else None


def _strata(row: dict) -> list[str]:
    ms, cls = row["motion_set"], row["vessel_class"] or "unknown"
    out = [f"{ms}:ALL", f"{ms}:{cls}"]
    if row["transiting"]:
        out.append("underway:TRANSITING")
    return out


def run_benchmark(
    cfg: BenchmarkConfig, store: ParquetStore, runs: RunStore, run_id: str | None = None
) -> BenchmarkRun:
    started = datetime.now(UTC).replace(microsecond=0)
    manifest = SplitManifest.model_validate_json(Path(cfg.manifest_path).read_text())
    if manifest.dataset_id != cfg.dataset_id:
        raise ValueError(f"manifest is for {manifest.dataset_id}, not {cfg.dataset_id}")
    if manifest.region_split is not None:
        raise NotImplementedError("P-region benchmarks need context positions; not in slice 2")
    run_id = run_id or f"{manifest.protocol}_{cfg.dataset_id}_{started:%Y%m%dT%H%M%SZ}"
    for m in cfg.models():  # fail before creating a run, not halfway through it
        caps = get_model(m, store, cfg.dataset_id).capabilities()
        if caps.max_horizon_hours < max(cfg.horizons_h):
            raise ValueError(
                f"{m} supports horizons up to {caps.max_horizon_hours} h; "
                f"requested {max(cfg.horizons_h)} h"
            )
    runs.create(run_id)
    base_record = dict(
        run_id=run_id,
        created_at=started,
        git_sha=git_sha(),
        dataset_id=cfg.dataset_id,
        data_provenance=_provenance(cfg),
        protocol={
            "protocol": manifest.protocol,
            "manifest_version": manifest.version,
            "dataset_id": manifest.dataset_id,
            "manifest_sha256": _sha256_file(Path(cfg.manifest_path)),
        },
        models=[
            {"model_id": m, "model_version": get_model(m).capabilities().model_version}
            for m in cfg.models()
        ],
        horizons_minutes=[h * 60 for h in cfg.horizons_h],
    )
    try:
        return _run(cfg, store, runs, run_id, manifest, base_record)
    except Exception as e:
        # A failed run is recorded and finalized too; never an orphan directory.
        failed = BenchmarkRun(
            **base_record,
            status="failed",
            error=f"{type(e).__name__}: {e}",
            sampler={**cfg.as_dict(), "runner_version": RUNNER_VERSION},
        )
        runs.finalize(run_id, failed)
        raise


def _provenance(cfg: BenchmarkConfig) -> str:
    ds_manifest = Path(cfg.manifest_path).parent.parent / f"{cfg.dataset_id}.canonical.json"
    if ds_manifest.exists():
        return DatasetManifest.model_validate_json(ds_manifest.read_text()).mapping_id
    return cfg.dataset_id


def _run(cfg, store, runs, run_id, manifest, base_record) -> BenchmarkRun:
    cutoffs = select_cutoffs(cfg, store, manifest)
    step_min = cfg.step_minutes
    grid_steps = round(max(cfg.horizons_h) * 60 / step_min)
    h_steps = {h: round(h * 60 / step_min) for h in cfg.horizons_h}
    preds = predict_all(cfg, store, cutoffs, grid_steps)
    truth = truth_grid(cfg, store, cutoffs, grid_steps)
    models = list(cfg.models())

    # -- per-model errors on the grid ------------------------------------------------------
    ok = {m: (preds[m]["status"] == "ok") for m in models}
    all_ok = np.logical_and.reduce([ok[m] for m in models])
    err, along, cross = {}, {}, {}
    for m in models:
        err[m] = distance_m(preds[m]["lat"], preds[m]["lon"], truth["lat"], truth["lon"])
        a, c = along_cross_track(
            preds[m]["lat"], preds[m]["lon"], truth["lat"], truth["lon"], truth["course"]
        )
        along[m], cross[m] = a, c
    paired_grid = truth["evaluable"] & all_ok[:, None]

    # -- per (cutoff, horizon) records -------------------------------------------------------
    rec_frames = []
    not_eval: dict[str, dict[str, int]] = {}
    cut = cutoffs.select(
        "cutoff_id",
        "vessel_id",
        "observed_at",
        "motion_set",
        "vessel_class",
        "transiting",
        "sog_mean_10m",
    )
    for h, k in h_steps.items():
        j = k - 1
        ev = truth["evaluable"][:, j]
        reasons = (
            pl.Series("reason", truth["reason"][:, j].tolist(), dtype=pl.Utf8)
            .drop_nulls()
            .value_counts()
        )
        nv = {r["reason"]: int(r["count"]) for r in reasons.to_dicts()}
        nv["model_failed"] = int((ev & ~all_ok).sum())  # truth existed, a model did not deliver
        not_eval[str(h * 60)] = dict(sorted(nv.items()))
        steps_mask = paired_grid[:, :k]
        for m in models:
            ade = np.where(
                steps_mask.any(axis=1),
                np.nansum(np.where(steps_mask, err[m][:, :k], 0), axis=1)
                / np.maximum(steps_mask.sum(axis=1), 1),
                np.nan,
            )
            rec_frames.append(
                cut.with_columns(
                    horizon_h=pl.lit(float(h)),
                    model_id=pl.lit(m),
                    status=pl.Series(preds[m]["status"].astype(str).tolist(), dtype=pl.Utf8),
                    error_code=pl.Series([c for c in preds[m]["code"]], dtype=pl.Utf8),
                    evaluable=pl.Series(ev),
                    reason=pl.Series(truth["reason"][:, j].tolist(), dtype=pl.Utf8),
                    paired=pl.Series(ev & all_ok),
                    error_m=pl.Series(np.where(ev & ok[m], err[m][:, j], np.nan)).fill_nan(None),
                    along_m=pl.Series(np.where(ev & ok[m], along[m][:, j], np.nan)).fill_nan(None),
                    cross_m=pl.Series(np.where(ev & ok[m], cross[m][:, j], np.nan)).fill_nan(None),
                    ade_m=pl.Series(np.where(ev & all_ok, ade, np.nan)).fill_nan(None),
                )
            )
    records = pl.concat(rec_frames)

    # -- admission per stratum (truth availability; model independent) ---------------------
    strata_rows = []
    base = records.filter(pl.col("model_id") == models[0])
    for r in base.select(
        "cutoff_id",
        "horizon_h",
        "vessel_id",
        "evaluable",
        "motion_set",
        "vessel_class",
        "transiting",
        "sog_mean_10m",
    ).iter_rows(named=True):
        for s in _strata(r):
            strata_rows.append({**r, "stratum": s})
    strat = pl.DataFrame(strata_rows)
    admission = {
        (r["stratum"], r["horizon_h"]): r["status"] for r in admit_by_stratum(strat).to_dicts()
    }

    # -- cells -------------------------------------------------------------------------------
    paired_keys = strat.join(
        records.filter(pl.col("model_id") == models[0]).select("cutoff_id", "horizon_h", "paired"),
        on=["cutoff_id", "horizon_h"],
    ).sort("stratum", "horizon_h", "cutoff_id")
    as_of = cutoffs["observed_at"].to_list()
    registered = set(model_ids())
    baselines = [b for b in BASELINES if b in registered]
    have_all_baselines = bool(baselines) and all(b in models for b in baselines)
    results: list[CellResult] = []
    dropped: list[DroppedCell] = []
    for (stratum, h), g in paired_keys.group_by("stratum", "horizon_h", maintain_order=True):
        pg = g.filter("paired")
        ev_ids = g.filter("evaluable")["cutoff_id"].to_numpy()
        n_failed = {m: int((~ok[m][ev_ids]).sum()) for m in models}
        if len(ev_ids) < cfg.min_cell_samples or pg.height == 0:
            dropped.append(
                DroppedCell(
                    stratum=stratum,
                    horizon_minutes=h * 60,
                    n_cutoffs=g.height,
                    n_evaluable=len(ev_ids),
                    n_paired=pg.height,
                    n_failed=n_failed,
                )
            )
            continue
        ids = pg["cutoff_id"].to_numpy()
        vids = pg["vessel_id"].to_numpy()
        keys = sorted(f"{v}|{as_of[c].isoformat()}|{h}" for c, v in zip(ids, vids, strict=True))
        sha = hashlib.sha256("\n".join(keys).encode()).hexdigest()
        n_vessels = len(np.unique(vids))
        j = h_steps[h] - 1
        boot = paired_vessel_bootstrap(
            {m: err[m][ids, j] for m in models}, vids, n_boot=cfg.n_boot, seed=cfg.seed
        )
        adm = admission.get((stratum, h), "experimental")
        rates = {m: n_failed[m] / (pg.height + n_failed[m]) for m in models}
        # Headline is a property of the CELL: the paired set was shaped by every model's
        # failures, so every model must be within tolerance, and the paired set must itself
        # meet the admission size minimums.
        headline = (
            adm == "first_class"
            and max(rates.values()) <= HEADLINE_MAX_FAILURE_RATE
            and pg.height >= DEFAULT_ADMISSION.min_evaluable
            and n_vessels >= DEFAULT_ADMISSION.min_vessels
        )
        best = None
        if have_all_baselines:
            best = min(baselines, key=lambda b: boot[b]["median"][0])
            if boot[best]["median"][0] < MIN_SKILL_BASELINE_M:
                best = None
        pmask = paired_grid[ids, : j + 1]
        n_decomposed = int(np.isfinite(truth["course"][ids, j]).sum())
        for m in models:
            e = err[m][ids, j]
            ade_m = np.where(pmask, err[m][ids, : j + 1], 0).sum(axis=1) / np.maximum(
                pmask.sum(axis=1), 1
            )
            skill = None
            if best is not None:
                ratio = 1 - boot[m]["median_boots"] / boot[best]["median_boots"]
                lo, hi = np.quantile(ratio, [0.025, 0.975])
                skill = Stat(
                    estimate=1 - boot[m]["median"][0] / boot[best]["median"][0],
                    ci_low=float(lo),
                    ci_high=float(hi),
                )
            results.append(
                CellResult(
                    model_id=m,
                    horizon_minutes=h * 60,
                    stratum=stratum,
                    admission=adm,
                    n_evaluable=len(ev_ids),
                    n_paired=pg.height,
                    n_vessels=n_vessels,
                    sample_set_sha256=sha,
                    n_failed=n_failed[m],
                    failure_rate=rates[m],
                    headline=headline,
                    median_error_m=Stat(
                        **dict(
                            zip(("estimate", "ci_low", "ci_high"), boot[m]["median"], strict=True)
                        )
                    ),
                    mean_error_m=Stat(
                        **dict(zip(("estimate", "ci_low", "ci_high"), boot[m]["mean"], strict=True))
                    ),
                    p90_error_m=float(np.quantile(e, 0.9)),
                    median_abs_along_track_m=_nanmedian(np.abs(along[m][ids, j])),
                    median_abs_cross_track_m=_nanmedian(np.abs(cross[m][ids, j])),
                    n_decomposed=n_decomposed,
                    median_ade_m=_nanmedian(ade_m),
                    best_baseline_id=best,
                    skill_vs_best_baseline=skill,
                )
            )

    # -- record ------------------------------------------------------------------------------
    run = BenchmarkRun(
        **base_record,
        status="succeeded",
        sampler={
            **cfg.as_dict(),
            "runner_version": RUNNER_VERSION,
            "admission": DEFAULT_ADMISSION.as_dict(),
            "n_cutoffs": cutoffs.height,
            "model_seconds": {m: round(preds[m]["seconds"], 2) for m in models},
        },
        not_evaluable=not_eval,
        results=results,
        dropped_cells=dropped,
    )
    runs.write_table(run_id, "cutoffs", cutoffs)
    runs.write_table(run_id, "records", records)
    runs.write_text(run_id, "report.md", render_report(run))
    runs.finalize(run_id, run)
    return run


def render_report(run: BenchmarkRun) -> str:
    def km(x: float) -> str:
        return f"{x / 1000:.2f}"

    L = [
        f"# Benchmark `{run.run_id}`",
        "",
        f"Protocol **{run.protocol.protocol}** "
        f"(manifest sha256 `{run.protocol.manifest_sha256[:12]}…`), "
        f"dataset `{run.dataset_id}` ({run.data_provenance}), git `{run.git_sha[:12]}`, "
        f"{run.sampler['n_cutoffs']:,} cutoffs, seed {run.sampler['seed']}.",
        "",
        "Paired: every model is scored on the same samples in each cell. Errors are median km; "
        "brackets are 95% per-model vessel-bootstrap CIs from shared resamples (paired contrasts "
        "are in `skill_vs_best_baseline`). Cells marked * are experimental (§6.4), too small, or "
        f"have model failure rates above {HEADLINE_MAX_FAILURE_RATE:.0%}: not headline.",
        "",
    ]
    models = [m.model_id for m in run.models]
    for stratum in ("underway:ALL", "underway:TRANSITING", "stationary:ALL"):
        cells = [r for r in run.results if r.stratum == stratum]
        if not cells:
            continue
        L += [
            f"## {stratum}",
            "",
            "| horizon | n (vessels) | " + " | ".join(models) + " |",
            "|---|---|" + "---|" * len(models),
        ]
        for h in sorted({r.horizon_minutes for r in cells}):
            by = {r.model_id: r for r in cells if r.horizon_minutes == h}
            any_r = next(iter(by.values()))
            row = []
            for m in models:
                r = by[m]
                mark = "" if r.headline else "*"
                fail = f", fail {r.failure_rate:.1%}" if r.n_failed else ""
                row.append(
                    f"{km(r.median_error_m.estimate)} [{km(r.median_error_m.ci_low)}, "
                    f"{km(r.median_error_m.ci_high)}]{mark}{fail}"
                )
            L.append(
                f"| {h / 60:g} h | {any_r.n_paired:,} ({any_r.n_vessels}) | "
                + " | ".join(row)
                + " |"
            )
        L.append("")
    L += [
        "## Not evaluable by horizon",
        "",
        "| horizon | "
        + " | ".join(sorted({k for v in run.not_evaluable.values() for k in v}))
        + " |",
    ]
    keys = sorted({k for v in run.not_evaluable.values() for k in v})
    L.append("|---|" + "---|" * len(keys))
    for h, v in run.not_evaluable.items():
        L.append(f"| {float(h) / 60:g} h | " + " | ".join(f"{v.get(k, 0):,}" for k in keys) + " |")
    L += ["", "## Runtime (s)", ""] + [
        f"- {m}: {s}" for m, s in run.sampler["model_seconds"].items()
    ]
    return "\n".join(L) + "\n"
