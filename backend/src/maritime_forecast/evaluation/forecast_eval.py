"""Score one served forecast against withheld truth (build plan §4, §6.1 steps 4-5).

The forecast is made first, causally, exactly as `POST /forecasts` serves it; truth is read
afterwards from the truth region with the benchmark's bracketing rule (evaluation.truth), so
the two can never mix. Target times without truth carry the truth's reason for every model;
where truth exists but a model produced nothing, that model's score is `model_failed`.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl

from maritime_forecast.config import default_region
from maritime_forecast.evaluation.feasibility import feasibility_flags
from maritime_forecast.evaluation.runner import truth_on_grid
from maritime_forecast.geo.geodesic import along_cross_track, distance_m
from maritime_forecast.geo.navigable_mask import NavigableMask
from maritime_forecast.schemas.forecast import (
    ForecastEvaluation,
    ForecastResponse,
    HorizonScore,
    ModelEvaluation,
    PathFeasibility,
    TrackPoint,
)
from maritime_forecast.storage.parquet_store import ParquetStore


def _num(x: float) -> float | None:
    return float(x) if np.isfinite(x) else None


def evaluate_forecast(
    store: ParquetStore,
    forecast: ForecastResponse,
    step_minutes: int,
    max_bracket_gap_minutes: float = 30.0,
    anchor: dict | None = None,
    mask: NavigableMask | None = None,
) -> ForecastEvaluation:
    """`anchor` is the last history report {lat, lon, sog_knots, observed_at}; without it no
    feasibility is reported. `mask` adds land flags (descriptive, never a constraint)."""
    if not forecast.forecasts:
        raise ValueError("nothing to evaluate")
    times = forecast.forecasts[0].target_times
    if any(f.target_times != times for f in forecast.forecasts):
        raise ValueError("every model forecast must share one target grid")
    step = timedelta(minutes=step_minutes)
    if times != [forecast.as_of + k * step for k in range(1, len(times) + 1)]:
        raise ValueError("target times are not the as_of + k * step grid truth is read on")
    cutoff = pl.DataFrame(
        {"cutoff_id": [0], "vessel_id": [forecast.vessel_id], "observed_at": [forecast.as_of]}
    )
    t = truth_on_grid(
        store, forecast.dataset_id, cutoff, len(times), step_minutes, max_bracket_gap_minutes
    )
    ev = t["evaluable"][0].astype(bool)
    lat, lon, course = t["lat"][0], t["lon"][0], t["course"][0]
    truth = [
        TrackPoint(timestamp=ts, latitude_deg=float(lat[i]), longitude_deg=float(lon[i]))
        if ev[i] else None
        for i, ts in enumerate(times)
    ]  # fmt: skip
    reasons = [None if ev[i] else t["reason"][0][i] for i in range(len(times))]
    minutes = [(ts - forecast.as_of).total_seconds() / 60 for ts in times]

    ok = {f.model_id: f.status == "ok" for f in forecast.forecasts}
    paired = [bool(ev[i]) and all(ok.values()) for i in range(len(times))]
    models = []
    for f in forecast.forecasts:
        if ok[f.model_id]:
            plat = np.array([p.latitude_deg for p in f.points])
            plon = np.array([p.longitude_deg for p in f.points])
            err = distance_m(plat, plon, lat, lon)
            along, cross = along_cross_track(plat, plon, lat, lon, course)
        feas = None
        if ok[f.model_id] and anchor is not None:
            fl = feasibility_flags(
                np.array([anchor["lat"]]), np.array([anchor["lon"]]),
                np.array([np.nan if anchor["sog_knots"] is None else anchor["sog_knots"]]),
                np.array([(times[0] - anchor["observed_at"]).total_seconds()]),
                plat[None, :], plon[None, :], step.total_seconds(), mask,
            )  # fmt: skip
            row = {k: None if v is None else v[0].tolist() for k, v in fl.items()}
            feas = PathFeasibility(
                land_point=row["land_point"], land_crossing=row["land_crossing"],
                implausible_speed=row["speed"], implausible_accel=row["accel"],
            )  # fmt: skip
        scores = []
        for i, m in enumerate(minutes):
            if not ev[i] or not ok[f.model_id]:
                why = reasons[i] if not ev[i] else "model_failed"
                scores.append(HorizonScore(horizon_minutes=m, evaluable=False, reason=why))
            else:
                scores.append(
                    HorizonScore(
                        horizon_minutes=m,
                        evaluable=True,
                        error_m=float(err[i]),
                        along_track_m=_num(along[i]),
                        cross_track_m=_num(cross[i]),
                    )  # fmt: skip
                )
        on_pairs = [s.error_m for s, p in zip(scores, paired, strict=True) if p]
        models.append(
            ModelEvaluation(
                model_id=f.model_id,
                status=f.status,
                scores=scores,
                ade_m=float(np.mean(on_pairs)) if on_pairs else None,
                feasibility=feas,
            )
        )
    warnings = []
    truth_region = default_region("truth_region").name
    if all(r == "no_report_before" for r in reasons):
        warnings.append(
            f"the vessel has no report in the truth region ({truth_region}) before any target "
            "time: it was not reporting, or the dataset lies outside that region"
        )
    elif not any(ev):
        warnings.append("no target time has truth: nothing was scored")
    elif not any(paired):
        warnings.append("no target time is scored for every model: models are not comparable")
    return ForecastEvaluation(
        forecast_id=forecast.forecast_id,
        dataset_id=forecast.dataset_id,
        vessel_id=forecast.vessel_id,
        as_of=forecast.as_of,
        data_version=forecast.data_version,
        truth_region=truth_region,
        max_bracket_gap_minutes=max_bracket_gap_minutes,
        target_times=times,
        truth=truth,
        truth_reasons=reasons,
        paired=paired,
        models=models,
        feasibility_mask=mask.source_id if mask is not None else None,
        warnings=warnings,
    )
