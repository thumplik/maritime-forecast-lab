"""B0 constant velocity (build plan §5), in two modes.

* `constant_velocity_v1` (standard): anchors on the most recent report that has usable SOG and
  COG (zero SOG needs no COG) and extrapolates SOG x lead along COG on the WGS84 geodesic,
  where lead is measured from that report's own time. It respects stale timestamps (§5) and
  warns when it had to fall back to an older report or the anchor is old.
* `constant_velocity_paper_v1` (paper-compatible, §6.6): uses the LAST report's SOG and COG,
  exactly as the calibration paper does, and extrapolates from as_of, treating that report as
  current. No fallback: if the last report lacks SOG/COG the forecast fails (typed), which is
  how the paper's protocol behaves. Not for headline results; it exists to match §6.6.
  It is the CAUSAL counterpart of scripts/calibrate.py, not the same computation: calibrate.py
  starts from the 10-min grid position interpolated toward the NEXT report and selects windows
  with full future coverage (P-paper only). Never use that anchor outside P-paper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
)
from maritime_forecast.models.baselines import constant_velocity

STALE_WARNING_MINUTES = 30


def _usable(sog, cog, flags=()) -> bool:
    if "sog_implausible" in (flags or ()):
        return False
    if sog is None or (isinstance(sog, float) and math.isnan(sog)):
        return False
    return sog == 0 or not (cog is None or (isinstance(cog, float) and math.isnan(cog)))


@dataclass
class ConstantVelocity(BaseForecaster):
    model_id: str = "constant_velocity_v1"
    config: dict = field(default_factory=lambda: {"stale_warning_minutes": STALE_WARNING_MINUTES})

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            reads_beyond_own_history=False,
            model_version="1.0.0",
            min_history_reports=1,
            needs_sog_cog=True,
            description="SOG x lead along COG on the WGS84 geodesic from the latest report with "
            "usable SOG/COG; lead measured from that report's time.",
        )

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        cols = ["observed_at", "latitude_deg", "longitude_deg", "sog_knots", "cog_deg"]
        if "quality_flags" in history.reports.columns:
            cols.append("quality_flags")
        rows = history.reports.select(cols)
        anchor, fell_back = None, False
        for i in range(rows.height - 1, -1, -1):
            r = rows.row(i, named=True)
            if _usable(r["sog_knots"], r["cog_deg"], r.get("quality_flags")):
                anchor = r
                fell_back = i != rows.height - 1
                break
        if anchor is None:
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={
                    "code": "no_usable_sog_cog",
                    "message": "no report in the history has usable SOG/COG",
                },
            )
        leads = [(t - anchor["observed_at"]).total_seconds() for t in horizon.target_times]
        cog = anchor["cog_deg"] if anchor["cog_deg"] is not None else np.nan
        lat, lon = constant_velocity(
            anchor["latitude_deg"], anchor["longitude_deg"], anchor["sog_knots"], cog, leads
        )
        warnings = []
        age_min = (history.as_of - anchor["observed_at"]).total_seconds() / 60
        if fell_back:
            warnings.append("latest report lacks usable SOG/COG; anchored on an earlier report")
        if age_min > self.config["stale_warning_minutes"]:
            warnings.append(f"anchor report is {age_min:.0f} min older than as_of")
        return lat.tolist(), lon.tolist(), warnings


@dataclass
class ConstantVelocityPaper(BaseForecaster):
    model_id: str = "constant_velocity_paper_v1"
    config: dict = field(default_factory=dict)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            reads_beyond_own_history=False,
            model_version="1.0.0",
            min_history_reports=1,
            needs_sog_cog=True,
            supports_missing_inputs=False,
            description="Calibration-paper CV: last report's SOG/COG, extrapolated from as_of.",
        )

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        last = history.last()
        if not _usable(last["sog_knots"], last["cog_deg"]):
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={
                    "code": "anchor_missing_sog_cog",
                    "message": "the last report lacks SOG/COG",
                },
            )
        leads = [(t - history.as_of).total_seconds() for t in horizon.target_times]
        cog = last["cog_deg"] if last["cog_deg"] is not None else np.nan
        lat, lon = constant_velocity(
            last["latitude_deg"], last["longitude_deg"], last["sog_knots"], cog, leads
        )
        age_min = (history.as_of - last["observed_at"]).total_seconds() / 60
        warnings = []
        if age_min > STALE_WARNING_MINUTES:
            warnings.append(
                f"last report is {age_min:.0f} min older than as_of (treated as current)"
            )
        return lat.tolist(), lon.tolist(), warnings
