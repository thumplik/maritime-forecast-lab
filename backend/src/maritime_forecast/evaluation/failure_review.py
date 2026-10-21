"""Failure review of a kept benchmark run for `route_retrieval_v1` (build plan §7 Milestone 3:
"failure review by category").

The run's records already hold every paired error. This module re-runs retrieval on the run's
cutoffs to recover what the records do not hold (candidate paths, held references, match
fallbacks), checks that it reproduces the run's predictions exactly, and flags each paired
(cutoff, horizon) sample with the categories below. The flags are NOT exclusive.

Categories (definitions fixed before the review was run; thresholds are module constants).
Those marked [truth] SELECT ON TRUTH: their per-category errors describe a truth-conditioned
subset, never a model's accuracy.

  references_exhausted  The central reference's segment ended before this target time, so the
                        forecast holds its last position.
  branch_disagreement   At least two candidates covering this step are farther than
                        max(2 km, 25% of the central path's displacement) from the central
                        prediction: the references split (a junction, or different speeds).
  wrong_branch          [truth] DIAGNOSTIC ONLY: branch_disagreement, retrieval error > 2 km,
                        and some candidate was within half of it. Says the right path was among
                        the candidates; it is an oracle over candidates and never an accuracy.
                        Reported next to `wrong_branch_control`: the same oracle over the same
                        candidates rotated about the anchor by a random angle (seeded), which
                        keeps their spread and distances but not their direction. Only the
                        excess of wrong_branch over its control is evidence.
  departure             [truth] Stationary set: the true position is > 1 km from the cutoff
                        position.
  false_departure       [truth] Stationary set: the vessel stayed (true displacement <= 0.5 km)
                        but the retrieval forecast moved > 1 km. Every such sample loses to
                        persistence by construction; only its share and size are findings.
  sparse_history        Fewer than 3 context steps (30 min of the target's own track without a
                        gap), or the last report is older than as_of by more than the stale-warning
                        limit of the kinematic baselines (STALE_WARNING_MINUTES, 30 min).
  new_geography         No eligible reference within the first match radius (1 km).
  timing_dominated      [truth] Retrieval error > 1 km and |along-track| > |cross-track|: mostly
                        wrong speed or timing along the right line, rather than the wrong line.

"Weather-driven turns" (§7) cannot be assessed: there is no archived weather in the dataset.

Per sample, retrieval is compared with the cell's best baseline as the RUN recorded it
(`CellResult.best_baseline_id`, the reference of its skill). Where the run has none (the best
baseline's median error is under 10 m, e.g. moored vessels at short horizons), the comparison is
with persistence for the size of the difference only, and no loss is scored: a "loss" decided
by metre-level noise would mean nothing. A "loss" is a sample where retrieval's error exceeds
the reference baseline's.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from maritime_forecast.geo.geodesic import LocalFrame, direct, distance_m, inverse
from maritime_forecast.models.base import ForecastHorizon, histories_for_cutoffs
from maritime_forecast.models.constant_velocity import STALE_WARNING_MINUTES
from maritime_forecast.models.route_retrieval import WARN_RADIUS_FALLBACK, context_xy

MODEL = "route_retrieval_v1"
BASELINES = ("persistence_v1", "constant_velocity_v1", "damped_velocity_v1")
SPREAD_MIN_M = 2000.0
SPREAD_FRAC = 0.25
WRONG_BRANCH_MIN_ERR_M = 2000.0
DEPART_M = 1000.0
STAY_M = 500.0
SPARSE_STEPS = 3
TIMING_MIN_ERR_M = 1000.0
CONTROL_SEED = 20230601
NO_REFERENCE_FALLBACK = "persistence_v1"
CATEGORIES = (
    "references_exhausted", "branch_disagreement", "wrong_branch", "wrong_branch_control",
    "departure", "false_departure", "sparse_history", "new_geography", "timing_dominated",
)  # fmt: skip
TRUTH_SELECTED = {
    "wrong_branch", "wrong_branch_control", "departure", "false_departure", "timing_dominated",
}  # fmt: skip
STRATA = {
    "underway:ALL": pl.col("motion_set") == "underway",
    "underway:TRANSITING": (pl.col("motion_set") == "underway") & pl.col("transiting"),
    "stationary:ALL": pl.col("motion_set") == "stationary",
}


@dataclass(frozen=True)
class StepDiagnostics:
    """Model-side facts for one (cutoff, horizon), from the forecast and the history only."""

    lat: float
    lon: float
    held: bool
    n_far: int
    n_covered: int
    cand_lat: np.ndarray  # covered candidates at this step (for the diagnostic oracle)
    cand_lon: np.ndarray


def step_diagnostics(forecast, step: int, anchor_lat: float, anchor_lon: float) -> StepDiagnostics:
    """Diagnostics at grid index `step` of an ok retrieval forecast."""
    t = forecast.target_times[step]
    p = forecast.points[step]
    central = next((c for c in forecast.trajectories
                    if c.trajectory_id == forecast.central_trajectory_id), None)  # fmt: skip
    # No central reference (e.g. v2 holding a stationary target): nothing was held from one.
    held = (
        central is not None
        and central.reference_ends_at is not None
        and (central.reference_ends_at < t)
    )
    cov = [
        c for c in forecast.trajectories if c.reference_ends_at is None or c.reference_ends_at >= t
    ]
    clat = np.array([c.points[step].latitude_deg for c in cov])
    clon = np.array([c.points[step].longitude_deg for c in cov])
    disp = float(distance_m(anchor_lat, anchor_lon, p.latitude_deg, p.longitude_deg))
    thr = max(SPREAD_MIN_M, SPREAD_FRAC * disp)
    far = distance_m(clat, clon, p.latitude_deg, p.longitude_deg) > thr if len(cov) else []
    return StepDiagnostics(p.latitude_deg, p.longitude_deg, held, int(np.sum(far)), len(cov),
                           clat, clon)  # fmt: skip


def rerun_diagnostics(model, store, dataset_id, cutoffs: pl.DataFrame, horizons_h, step_minutes,
                      history_hours) -> pl.DataFrame:  # fmt: skip
    """One row per (cutoff, horizon) of an ok retrieval forecast: model-side diagnostics and
    the candidates' positions (for the diagnostic oracle, applied later against truth)."""
    rows = []
    max_h = max(horizons_h)
    for cid, h in histories_for_cutoffs(store, dataset_id, cutoffs, history_hours):
        f = model.predict(h, ForecastHorizon(h.as_of, max_h, step_minutes))
        if f.status != "ok":
            continue
        last = h.last()
        frame = LocalFrame(last["latitude_deg"], last["longitude_deg"])
        ix = model.cfg.index
        n_ctx = len(context_xy(h, frame, last["observed_at"].timestamp(), SPARSE_STEPS,
                               ix.step_minutes * 60.0, ix.segment.gap_minutes * 60.0))  # fmt: skip
        age_min = (h.as_of - last["observed_at"]).total_seconds() / 60
        fallback = any(w.startswith(WARN_RADIUS_FALLBACK) for w in f.warnings)
        for hh in horizons_h:
            k = round(hh * 60 / step_minutes) - 1
            d = step_diagnostics(f, k, last["latitude_deg"], last["longitude_deg"])
            rows.append({
                "cutoff_id": cid, "horizon_h": float(hh), "rr_lat": d.lat, "rr_lon": d.lon,
                "held": d.held, "n_far": d.n_far, "n_covered": d.n_covered,
                "cand_lat": d.cand_lat.tolist(), "cand_lon": d.cand_lon.tolist(),
                "context_steps": n_ctx, "anchor_age_min": age_min, "radius_fallback": fallback,
                "max_similarity": max(c.similarity for c in f.trajectories),
            })  # fmt: skip
    return pl.DataFrame(rows)


def best_baselines(run) -> dict[tuple[str, float], str | None]:
    """(stratum, horizon h) -> the run's best baseline for its skill, or None where it has none."""
    return {
        (c.stratum, c.horizon_minutes / 60): c.best_baseline_id
        for c in run.results
        if c.model_id == MODEL and c.stratum in STRATA
    }


def _rotated_oracle(cand_lat, cand_lon, a_lat, a_lon, t_lat, t_lon, rng) -> float:
    """Control for wrong_branch: the closest candidate to truth after rotating every candidate
    about the anchor by one random angle (spread and distances kept, direction lost)."""
    if not len(cand_lat):
        return np.nan
    d, brg = inverse(a_lat, a_lon, np.asarray(cand_lat), np.asarray(cand_lon))
    la, lo = direct(a_lat, a_lon, brg + rng.uniform(0.0, 360.0), d)
    return float(np.min(distance_m(la, lo, t_lat, t_lon)))


def flag_samples(records: pl.DataFrame, cutoffs: pl.DataFrame, diag: pl.DataFrame,
                 best: dict[tuple[str, float], str | None]) -> pl.DataFrame:  # fmt: skip
    """Paired retrieval samples with their best-baseline error and category flags."""
    rr = records.filter(pl.col("paired") & (pl.col("model_id") == MODEL)).select(
        "cutoff_id", "horizon_h", "motion_set", "vessel_class", "transiting", "error_m",
        "along_m", "cross_m", "pred_lat", "pred_lon", "truth_lat", "truth_lon",
    )  # fmt: skip
    base = records.filter(pl.col("paired") & pl.col("model_id").is_in(BASELINES)).select(
        "cutoff_id", "horizon_h", "model_id", pl.col("error_m").alias("base_err")
    )
    anchor = cutoffs.select("cutoff_id", pl.col("latitude_deg").alias("a_lat"),
                            pl.col("longitude_deg").alias("a_lon"))  # fmt: skip
    df = rr.join(anchor, on="cutoff_id").join(diag, on=["cutoff_id", "horizon_h"], how="left")
    if df["rr_lat"].null_count():
        raise ValueError("re-run did not produce a forecast for every paired retrieval sample")
    if not (np.allclose(df["rr_lat"], df["pred_lat"], atol=1e-9, rtol=0)
            and np.allclose(df["rr_lon"], df["pred_lon"], atol=1e-9, rtol=0)):  # fmt: skip
        raise ValueError("re-run does not reproduce the run's retrieval predictions")
    frames = []
    for name, expr in STRATA.items():
        s = df.filter(expr)
        ref = pl.DataFrame(
            [{"horizon_h": float(h), "base_model": m or NO_REFERENCE_FALLBACK,
              "has_reference": m is not None} for (n, h), m in best.items() if n == name]
        )  # fmt: skip
        s = (s.join(ref, on="horizon_h")
             .join(base, left_on=["cutoff_id", "horizon_h", "base_model"],
                   right_on=["cutoff_id", "horizon_h", "model_id"])
             .with_columns(stratum=pl.lit(name)))  # fmt: skip
        frames.append(s)
    out = pl.concat(frames)
    tlat, tlon = out["truth_lat"].to_numpy(), out["truth_lon"].to_numpy()
    true_disp = distance_m(out["a_lat"].to_numpy(), out["a_lon"].to_numpy(), tlat, tlon)
    rr_disp = distance_m(out["a_lat"].to_numpy(), out["a_lon"].to_numpy(),
                         out["pred_lat"].to_numpy(), out["pred_lon"].to_numpy())  # fmt: skip
    best_cand = np.array([
        float(np.min(distance_m(np.asarray(la), np.asarray(lo), t1, t2))) if len(la) else np.nan
        for la, lo, t1, t2 in zip(out["cand_lat"], out["cand_lon"], tlat, tlon, strict=True)
    ])  # fmt: skip
    rng = np.random.default_rng(CONTROL_SEED)
    a_lat, a_lon = out["a_lat"].to_numpy(), out["a_lon"].to_numpy()
    control = np.array([
        _rotated_oracle(la, lo, x, y, t1, t2, rng)
        for la, lo, x, y, t1, t2 in zip(out["cand_lat"], out["cand_lon"], a_lat, a_lon, tlat,
                                        tlon, strict=True)
    ])  # fmt: skip
    err = out["error_m"].to_numpy()
    stationary = (out["motion_set"] == "stationary").to_numpy()
    branch = (out["n_far"] >= 2).to_numpy()
    flags = {
        "references_exhausted": out["held"].to_numpy(),
        "branch_disagreement": branch,
        "wrong_branch": branch & (err > WRONG_BRANCH_MIN_ERR_M) & (best_cand < 0.5 * err),
        "wrong_branch_control": branch & (err > WRONG_BRANCH_MIN_ERR_M) & (control < 0.5 * err),
        "departure": stationary & (true_disp > DEPART_M),
        "false_departure": stationary & (true_disp <= STAY_M) & (rr_disp > DEPART_M),
        "sparse_history": ((out["context_steps"] < SPARSE_STEPS)
                           | (out["anchor_age_min"] > STALE_WARNING_MINUTES)).to_numpy(),
        "new_geography": out["radius_fallback"].to_numpy(),
        "timing_dominated": (err > TIMING_MIN_ERR_M)
        & (np.abs(out["along_m"].to_numpy()) > np.abs(out["cross_m"].to_numpy())),
    }  # fmt: skip
    return out.drop("cand_lat", "cand_lon").with_columns(
        true_disp_m=true_disp, rr_disp_m=rr_disp, best_candidate_err_m=best_cand,
        control_candidate_err_m=control,
        loss=pl.when(pl.col("has_reference")).then(pl.col("error_m") > pl.col("base_err")),
        **{k: pl.Series(v, dtype=pl.Boolean) for k, v in flags.items()},
    )  # fmt: skip


def summarize(samples: pl.DataFrame) -> pl.DataFrame:
    """Per (stratum, horizon, category): how common it is, and what it does to retrieval
    against the best baseline. `share_of_losses` = flagged losses / all losses."""
    rows = []
    for (stratum, h), g in samples.group_by("stratum", "horizon_h", maintain_order=True):
        n = g.height
        scored = bool(g["has_reference"].all())
        losses = int(g["loss"].sum()) if scored else 0
        for cat in ("all", *CATEGORIES):
            s = g if cat == "all" else g.filter(pl.col(cat))
            if not s.height:
                continue
            rows.append({
                "stratum": stratum, "horizon_h": h, "category": cat, "n": s.height,
                "share": s.height / n,
                "loss_rate": float(s["loss"].mean()) if scored else None,
                "share_of_losses": int(s["loss"].sum()) / losses if losses else None,
                "selects_on_truth": cat in TRUTH_SELECTED, "has_reference": scored,
                "rr_median_km": float(s["error_m"].median()) / 1000,
                "base_median_km": float(s["base_err"].median()) / 1000,
                "median_diff_km": float((s["error_m"] - s["base_err"]).median()) / 1000,
                "base_model": g["base_model"][0],
            })  # fmt: skip
    return pl.DataFrame(rows).sort("stratum", "horizon_h", "category")
