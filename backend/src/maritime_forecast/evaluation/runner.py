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
from maritime_forecast.evaluation import holdout
from maritime_forecast.evaluation.admission import DEFAULT_ADMISSION, admit_by_stratum
from maritime_forecast.evaluation.bootstrap import paired_vessel_bootstrap
from maritime_forecast.evaluation.feasibility import FeasibilityConfig, feasibility_flags
from maritime_forecast.evaluation.truth import truth_sql
from maritime_forecast.geo.geodesic import along_cross_track, distance_m, inverse
from maritime_forecast.geo.navigable_mask import NavigableMask, load_navigable_mask
from maritime_forecast.models.base import ForecastHorizon, ModelContext, histories_for_cutoffs
from maritime_forecast.models.registry import get_model, model_ids, product_model_ids
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.benchmark import (
    HEADLINE_MAX_FAILURE_RATE,
    BenchmarkRun,
    CellResult,
    DroppedCell,
    FeasibilityStats,
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
    # Held-out test periods (evaluation.holdout): explicit confirmation, provenance, re-runs.
    confirm_test: bool = False
    informed_by: tuple[str, ...] = ()
    test_rerun_reason: str | None = None
    min_cell_samples: int = 20
    feasibility: FeasibilityConfig = field(default_factory=FeasibilityConfig)
    samplers: dict[str, SamplerConfig] = field(
        default_factory=lambda: {
            "underway": SamplerConfig(motion_set="underway"),
            "stationary": SamplerConfig(motion_set="stationary"),
        }
    )

    def models(self) -> tuple[str, ...]:
        return self.model_ids or tuple(product_model_ids())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["manifest_path"] = str(self.manifest_path)
        d["model_ids"] = list(self.models())
        d["samplers"] = {k: v.as_dict() for k, v in self.samplers.items()}
        d["feasibility"] = self.feasibility.as_dict()
        return d


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _lit(t: datetime) -> str:
    return f"TIMESTAMPTZ '{t.isoformat()}'"


def select_cutoffs(
    cfg: BenchmarkConfig, store: ParquetStore, manifest: SplitManifest, stats: dict | None = None
) -> pl.DataFrame:
    """Sampled cutoffs whose whole [as_of - history, as_of + max horizon] lies in the split.

    P-region: the test split samples cutoffs in the cutoff region (the test side); other
    splits sample anywhere in the truth region on the train side. The sampler runs on
    candidates whose cutoff report is on the required side and whose window is in the split's
    period; windows whose other context reports leave that side (or, outside test, whose label
    reports enter the buffer or the test side) are then dropped (`region_window_ok`). So
    test membership depends only on context positions, never on where the vessel goes.
    `stats`, if given, receives the effective sampling region and the P-region drop count."""
    periods = [p for p in manifest.periods or [] if p.split == cfg.split]
    if not periods:
        raise ValueError(f"manifest has no {cfg.split!r} period")
    lo = min(p.start for p in periods)
    hi = max(p.end for p in periods)
    max_h = timedelta(hours=max(cfg.horizons_h))
    hist = timedelta(hours=cfg.history_hours)
    rs = manifest.region_split
    if rs is None:
        box = region(cfg.cutoff_region)
        predicate = box.sql_predicate()
    else:
        side = "test" if cfg.split == "test" else "train"
        box = region(cfg.cutoff_region) if side == "test" else default_region("truth_region")
        predicate = f"{box.sql_predicate()} AND ({rs.sql_case()}) = '{side}'"
    if stats is not None:
        stats["sampling_region"] = box.name if rs is None else f"{box.name}:{side}-side"
    files = store.files(cfg.dataset_id, lo - timedelta(hours=3), hi)
    src = "read_parquet([" + ", ".join(f"'{f}'" for f in files) + "], hive_partitioning=false)"
    cand = store.con.execute(cutoff_candidates_sql(src, predicate, _lit(lo), _lit(hi))).pl()
    if rs is None:
        splits = manifest.window_splits(
            cand["vessel_id"], cand["observed_at"] - hist, cand["observed_at"] + max_h
        )
    else:  # time part only; the positions are checked after sampling
        splits = [
            a if a == b else None
            for a, b in zip(
                map(manifest.time_split, cand["observed_at"] - hist),
                map(manifest.time_split, cand["observed_at"] + max_h),
                strict=True,
            )
        ]
    cand = cand.filter(pl.Series(splits, dtype=pl.Utf8) == cfg.split)
    frames = []
    for name, sc in cfg.samplers.items():
        s = sample_cutoffs(cand, SamplerConfig(**{**asdict(sc), "seed": cfg.seed}))
        if cfg.max_cutoffs_per_set is not None and s.height > cfg.max_cutoffs_per_set:
            s = s.sample(cfg.max_cutoffs_per_set, seed=cfg.seed).sort("vessel_id", "observed_at")
        frames.append(s.with_columns(motion_set=pl.lit(name)))
    cutoffs = pl.concat(frames).sort("motion_set", "vessel_id", "observed_at")
    if rs is not None:
        # Labels: through the last target plus the truth bracket (truth may interpolate toward
        # a report up to the bracket gap after it).
        label_end = max_h + timedelta(minutes=cfg.max_bracket_gap_minutes)
        keep = region_window_ok(store, cfg.dataset_id, cutoffs, rs, cfg.split, hist, label_end)
        if stats is not None:
            stats["region_dropped"] = int((~keep).sum())
        cutoffs = cutoffs.filter(keep)
    return cutoffs.with_row_index("cutoff_id").with_columns(
        transiting=(pl.col("motion_set") == "underway") & transiting_expr()
    )


def region_window_ok(store, dataset_id, cutoffs, rs, split, hist, max_h) -> pl.Series:
    """P-region window membership by position: every context report (as_of - hist, as_of] on
    the split's side; outside test, every label report (as_of, as_of + max_h] on the train
    side (no training label from the test waters or the buffer). Pass the label reach as
    `max_h` (horizon plus the truth bracket)."""
    side = "test" if split == "test" else "train"
    lo = cutoffs["observed_at"].min() - hist
    hi = cutoffs["observed_at"].max() + max_h + timedelta(microseconds=1)
    files = store.files(dataset_id, lo, hi)
    src = "read_parquet([" + ", ".join(f"'{f}'" for f in files) + "], hive_partitioning=false)"
    con = store.con
    con.register("_rw_arrow", cutoffs.with_row_index("_i").select("_i", "vessel_id",
                 pl.col("observed_at").alias("as_of")).to_arrow())  # fmt: skip
    con.execute("CREATE OR REPLACE TEMP TABLE _rw AS SELECT * FROM _rw_arrow")
    con.unregister("_rw_arrow")
    labels = "true" if split == "test" else (
        f"coalesce(bool_and(coalesce(({rs.sql_case('r.latitude_deg', 'r.longitude_deg')}) "
        "= 'train', false)) FILTER (WHERE r.observed_at > c.as_of), true)"
    )  # fmt: skip
    ok = con.execute(f"""
        SELECT c._i,
               coalesce(bool_and(coalesce(({rs.sql_case('r.latitude_deg', 'r.longitude_deg')})
                        = '{side}', false)) FILTER (WHERE r.observed_at <= c.as_of), false)
               AND {labels} AS ok
        FROM _rw c JOIN {src} r
          ON r.vessel_id = c.vessel_id
         AND r.observed_at > c.as_of - INTERVAL {int(hist.total_seconds())} SECOND
         AND r.observed_at <= c.as_of + INTERVAL {int(max_h.total_seconds())} SECOND
        GROUP BY c._i ORDER BY c._i""").pl()  # fmt: skip
    con.execute("DROP TABLE IF EXISTS _rw")
    out = np.zeros(cutoffs.height, dtype=bool)
    out[ok["_i"].to_numpy()] = ok["ok"].fill_null(False).to_numpy()
    return pl.Series(out)


def predict_all(cfg, store, cutoffs, grid_steps, ctx: ModelContext):
    """Per model: (status[N], error_code[N], lat[N,S], lon[N,S], warnings count, seconds)."""
    horizon_h = max(cfg.horizons_h)
    n, s = cutoffs.height, grid_steps
    out = {}
    for m in cfg.models():
        model = get_model(m, ctx)
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


def truth_grid(cfg, store, cutoffs, grid_steps, truth_until: datetime | None = None):
    """Truth for every (cutoff, step) of a benchmark; see truth_on_grid."""
    return truth_on_grid(
        store, cfg.dataset_id, cutoffs, grid_steps, cfg.step_minutes,
        cfg.max_bracket_gap_minutes, truth_until,
    )  # fmt: skip


def truth_on_grid(
    store: ParquetStore,
    dataset_id: str,
    cutoffs: pl.DataFrame,
    grid_steps: int,
    step_minutes: float,
    max_bracket_gap_minutes: float,
    truth_until: datetime | None = None,
) -> dict[str, np.ndarray]:
    """Truth for every (cutoff, step): arrays [N,S] of lat, lon, course, evaluable, reason.

    `cutoffs` has cutoff_id (0..N-1), vessel_id, observed_at (the as_of). Step k is at
    as_of + k * step, k = 1..grid_steps. `truth_until` caps the observations truth may use
    (exclusive). Fitting on a non-test split passes that split's period end, so truth never
    brackets with a report from a later split, whatever the purge gap is."""
    step = timedelta(minutes=step_minutes)
    targets = (
        cutoffs.select("cutoff_id", "vessel_id", "observed_at")
        .join(pl.DataFrame({"k": np.arange(1, grid_steps + 1)}), how="cross")
        .with_columns(target_t=pl.col("observed_at") + pl.col("k") * step)
        .drop("observed_at")
    )
    # Start one bracket gap (plus a report's margin for the spike filter's lag) before the
    # first cutoff: an API as_of between reports still finds the report bracketing its first
    # targets, and that report's spike test sees its predecessor, as inside a benchmark.
    lo = cutoffs["observed_at"].min() - timedelta(minutes=max_bracket_gap_minutes + 10)
    hi = cutoffs["observed_at"].max() + grid_steps * step + timedelta(minutes=60)
    if truth_until is not None:
        hi = min(hi, truth_until)
    con = store.con
    obs = store.scan(dataset_id, lo, hi, vessel_ids=cutoffs["vessel_id"].unique().to_list())
    con.register("_bench_obs", obs.to_arrow())
    truth_box = default_region("truth_region")  # truth only from inside the truth region
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _bench_truth_obs AS SELECT vessel_id, observed_at, "
        f"latitude_deg, longitude_deg, cog_deg, sog_knots FROM ({segmented_sql('_bench_obs')}) "
        f"WHERE {truth_box.sql_predicate()}"
    )
    # Materialize the targets: an ASOF join against a registered Arrow object was measured
    # ~1800x slower than against a table (382 s vs 0.2 s for 576k targets).
    con.register("_bench_targets_arrow", targets.to_arrow())
    con.execute("CREATE OR REPLACE TEMP TABLE _bench_targets AS SELECT * FROM _bench_targets_arrow")
    con.unregister("_bench_targets_arrow")
    tsql = truth_sql(
        "_bench_targets",
        "_bench_truth_obs",
        max_bracket_gap_minutes,
        prev_cols=("cog_deg", "sog_knots"),
    )
    t = con.execute(
        "SELECT cutoff_id, k, evaluable, reason, truth_lat, truth_lon, p_lat, p_lon, n_lat, "
        f"n_lon, bracket_gap_s, p_cog_deg, p_sog_knots FROM ({tsql}) ORDER BY cutoff_id, k"
    ).pl()
    con.unregister("_bench_obs")
    con.execute("DROP TABLE IF EXISTS _bench_targets")
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


def _rate(flags: np.ndarray | None, ids: np.ndarray, j: int) -> float | None:
    return None if flags is None else float(flags[ids, j].mean())


def _nanmedian(a: np.ndarray) -> float | None:
    a = a[np.isfinite(a)]
    return float(np.median(a)) if a.size else None


def _strata(row: dict) -> list[str]:
    ms, cls = row["motion_set"], row["vessel_class"] or "unknown"
    out = [f"{ms}:ALL", f"{ms}:{cls}"]
    if row["transiting"]:
        out.append("underway:TRANSITING")
    return out


def benchmark_context(
    cfg: BenchmarkConfig, store: ParquetStore
) -> tuple[SplitManifest, ModelContext]:
    """The split manifest and the protocol-scoped model context a benchmark runs under."""
    manifest = SplitManifest.model_validate_json(Path(cfg.manifest_path).read_text())
    if manifest.dataset_id != cfg.dataset_id:
        raise ValueError(f"manifest is for {manifest.dataset_id}, not {cfg.dataset_id}")
    ctx = ModelContext(
        store=store,
        dataset_id=cfg.dataset_id,
        protocol=manifest.protocol,
        manifest_sha256=_sha256_file(Path(cfg.manifest_path)),
    )
    return manifest, ctx


def navigable_mask(cfg: BenchmarkConfig, ctx: ModelContext) -> tuple[NavigableMask, dict] | None:
    """The protocol's fitted navigable mask, or None if it was never built. Raises if it was
    fitted for another manifest or on the split being scored."""
    found = load_navigable_mask(ctx.protocol, cfg.dataset_id, ctx.manifest_sha256)
    if found is not None and found[1]["traffic"]["split"] != "train":
        raise ValueError(
            f"the navigable mask was fitted on the {found[1]['traffic']['split']!r} split; "
            "only train-split masks are used"
        )
    return found


def preflight(cfg: BenchmarkConfig, ctx: ModelContext) -> None:
    """Refuse a benchmark that could not produce an honest run (raises ValueError)."""
    navigable_mask(cfg, ctx)
    known = set(model_ids())
    unknown = [m for m in cfg.models() if m not in known]
    if unknown:
        raise ValueError(f"unknown model(s) {unknown}")
    for m in cfg.models():
        model = get_model(m, ctx)
        caps = model.capabilities()
        fitted_on = model.config.get("fit_split")
        if fitted_on is not None and fitted_on == cfg.split:
            raise ValueError(
                f"{m} was fitted on the {cfg.split!r} split; scoring it there would be in-sample"
            )
        if model.config.get("diagnostic_only"):
            raise ValueError(
                f"{m} is configured as a diagnostic variant (e.g. a rolling retrieval pool); "
                "it is never scored in a benchmark"
            )
        if not caps.installed:
            raise ValueError(f"{m} is not installed/fitted for {ctx.protocol} on {cfg.dataset_id}")
        if caps.max_horizon_hours < max(cfg.horizons_h):
            raise ValueError(
                f"{m} supports horizons up to {caps.max_horizon_hours} h; "
                f"requested {max(cfg.horizons_h)} h"
            )


def run_benchmark(
    cfg: BenchmarkConfig, store: ParquetStore, runs: RunStore, run_id: str | None = None
) -> BenchmarkRun:
    started = datetime.now(UTC).replace(microsecond=0)
    manifest, ctx = benchmark_context(cfg, store)
    split_tag = "" if cfg.split == "test" else f"_{cfg.split}"
    if cfg.cutoff_region != "houston_galveston":  # name runs started elsewhere (the default is
        split_tag += f"_{cfg.cutoff_region}"  # unnamed, so existing ids are unchanged)
    run_id = run_id or f"{manifest.protocol}_{cfg.dataset_id}{split_tag}_{started:%Y%m%dT%H%M%SZ}"
    preflight(cfg, ctx)  # fail before creating a run, not halfway through it
    model_refs = [
        {
            "model_id": m,
            "model_version": get_model(m, ctx).capabilities().model_version,
            "config": get_model(m, ctx).config,
        }
        for m in cfg.models()
    ]
    sha = git_sha()
    holdout.check_test_access(manifest, cfg.split, cfg.confirm_test, sha, model_refs,
                              cfg.test_rerun_reason)  # fmt: skip
    runs.create(run_id)
    if holdout.is_holdout(manifest) and cfg.split == "test":  # recorded before it is scored
        holdout.record_test_access(manifest, run_id, sha, model_refs, list(cfg.informed_by),
                                   cfg.test_rerun_reason)  # fmt: skip
    base_record = dict(
        run_id=run_id,
        created_at=started,
        git_sha=sha,
        dataset_id=cfg.dataset_id,
        data_provenance=_provenance(cfg),
        protocol={
            "protocol": manifest.protocol,
            "manifest_version": manifest.version,
            "dataset_id": manifest.dataset_id,
            "manifest_sha256": _sha256_file(Path(cfg.manifest_path)),
        },
        models=model_refs,
        horizons_minutes=[h * 60 for h in cfg.horizons_h],
    )
    try:
        return _run(cfg, store, runs, run_id, manifest, base_record, ctx)
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


def _run(cfg, store, runs, run_id, manifest, base_record, ctx) -> BenchmarkRun:
    select_stats: dict = {}
    cutoffs = select_cutoffs(cfg, store, manifest, select_stats)
    step_min = cfg.step_minutes
    grid_steps = round(max(cfg.horizons_h) * 60 / step_min)
    h_steps = {h: round(h * 60 / step_min) for h in cfg.horizons_h}
    preds = predict_all(cfg, store, cutoffs, grid_steps, ctx)
    # Non-test splits: truth never reads past the split's own period (a later split's data).
    ends = [p.end for p in manifest.periods or [] if p.split == cfg.split]
    truth = truth_grid(cfg, store, cutoffs, grid_steps,
                       max(ends) if ends and cfg.split != "test" else None)  # fmt: skip
    models = list(cfg.models())

    # -- feasibility flags, cumulative over the grid (a flag at step k: anywhere in 1..k) ---
    found = navigable_mask(cfg, ctx)
    mask = found[0] if found else None
    n = cutoffs.height
    anchor = [cutoffs[c].cast(pl.Float64).fill_null(np.nan).to_numpy()
              for c in ("latitude_deg", "longitude_deg", "sog_knots")]  # fmt: skip
    feas = {}
    for m in models:
        f = feasibility_flags(
            *anchor, np.full(n, step_min * 60.0), preds[m]["lat"], preds[m]["lon"],
            step_min * 60.0, mask, cfg.feasibility,
        )  # fmt: skip
        feas[m] = {k: None if v is None else np.logical_or.accumulate(v, axis=1)
                   for k, v in f.items()}  # fmt: skip
    # Truth on "land" at each step, and cumulatively (any evaluable truth step 1..k): the
    # cumulative form is the mask's own error, comparable to the cumulative model rates.
    truth_bad = (
        mask.is_infeasible(truth["lat"], truth["lon"]) & truth["evaluable"] if mask else None
    )
    truth_bad_cum = np.logical_or.accumulate(truth_bad, axis=1) if mask else None

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
                    pred_lat=pl.Series(np.where(ok[m], preds[m]["lat"][:, j], np.nan)).fill_nan(
                        None
                    ),
                    pred_lon=pl.Series(np.where(ok[m], preds[m]["lon"][:, j], np.nan)).fill_nan(
                        None
                    ),
                    truth_lat=pl.Series(np.where(ev, truth["lat"][:, j], np.nan)).fill_nan(None),
                    truth_lon=pl.Series(np.where(ev, truth["lon"][:, j], np.nan)).fill_nan(None),
                    **{
                        f"feas_{k}": pl.Series(
                            v[:, j] if v is not None else np.zeros(n, bool), dtype=pl.Boolean
                        ).zip_with(
                            pl.Series(ok[m] & (v is not None)),
                            pl.Series([None] * n, dtype=pl.Boolean),
                        )
                        for k, v in feas[m].items()
                    },
                    truth_infeasible=(
                        pl.Series(truth_bad[:, j]).zip_with(
                            pl.Series(ev), pl.Series([None] * n, dtype=pl.Boolean)
                        )
                        if truth_bad is not None
                        else pl.Series([None] * n, dtype=pl.Boolean)
                    ),
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
            fs = feas[m]
            feasibility = FeasibilityStats(
                land_point_rate=_rate(fs["land_point"], ids, j),
                land_crossing_rate=_rate(fs["land_crossing"], ids, j),
                implausible_speed_rate=_rate(fs["speed"], ids, j),
                implausible_accel_rate=_rate(fs["accel"], ids, j),
                truth_on_land_rate=_rate(truth_bad_cum, ids, j),
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
                    feasibility=feasibility,
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
            **select_stats,
            "model_seconds": {m: round(preds[m]["seconds"], 2) for m in models},
        },
        not_evaluable=not_eval,
        results=results,
        dropped_cells=dropped,
        feasibility={
            **cfg.feasibility.as_dict(),
            "mask": found[1]["source_id"] if found else None,
            "mask_sha256": found[1]["mask_sha256"] if found else None,
        },
        warnings=(
            [f"smoke run: at most {cfg.max_cutoffs_per_set} cutoffs per motion set; "
             "not a full benchmark"]
            if cfg.max_cutoffs_per_set is not None else []
        ) + (
            [] if found else [f"no navigable mask for {manifest.protocol}: land feasibility "
                              "not scored (run scripts/build_navigable_mask.py)"]
        ),
    )  # fmt: skip
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
        f"dataset `{run.dataset_id}` ({run.data_provenance}), split **{run.sampler['split']}**, "
        f"git `{run.git_sha[:12]}`, "
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
    mask = (run.feasibility or {}).get("mask")
    L += (
        [
            "## Feasibility (paired samples, anywhere up to the horizon)",
            "",
            f"Mask: `{mask}`. Cells: % with a forecast point on infeasible land / % whose path "
            "crosses it / % with an implausible speed. `truth` is the % of the TRUE positions the "
            "mask puts on land: the mask's own error, a floor under any land rate.",
            "",
        ]
        if mask
        else ["## Feasibility", "", "No navigable mask: land feasibility not scored.", ""]
    )
    for stratum in ("underway:ALL", "underway:TRANSITING"):
        cells = [r for r in run.results if r.stratum == stratum and r.feasibility]
        if not cells or not mask:
            continue
        L += [f"### {stratum}", "", "| horizon | truth | " + " | ".join(models) + " |",
              "|---|---|" + "---|" * len(models)]  # fmt: skip
        for h in sorted({r.horizon_minutes for r in cells}):
            by = {r.model_id: r.feasibility for r in cells if r.horizon_minutes == h}
            t = next(iter(by.values())).truth_on_land_rate
            row = [f"{by[m].land_point_rate:.1%} / {by[m].land_crossing_rate:.1%} / "
                   f"{by[m].implausible_speed_rate:.1%}" for m in models]  # fmt: skip
            L.append(f"| {h / 60:g} h | {t:.1%} | " + " | ".join(row) + " |")
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
