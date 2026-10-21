"""B0 constant turn rate (build plan §5): hold the recent turn rate, capped and decaying.

Turn rate: median of wrapped dCOG/dt over consecutive reports in the last `window_minutes`
before the anchor, using only reports with usable COG and SOG >= `min_sog_kn` (turning at
standstill is noise). It is capped at `max_rate_deg_s` and decays as exp(-t / decay_minutes),
so heading(t) = cog0 + rate0 * tau_w * (1 - exp(-t / tau_w)). Speed is the anchor SOG.

The path is integrated in a local east-north frame (vectorized, 30 s steps) and mapped back
with the geodesic direct problem from the anchor (distance and azimuth from the centre are
exact, as in an azimuthal-equidistant projection). Defaults are untuned, fixed constants: no
data beyond the target's own history is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from maritime_forecast.geo.geodesic import direct, knots_to_mps
from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
)
from maritime_forecast.models.constant_velocity import STALE_WARNING_MINUTES, select_anchor

DEFAULTS = {
    "window_minutes": 10.0,
    "min_sog_kn": 2.0,
    "max_rate_deg_s": 0.05,
    "decay_minutes": 20.0,
    "integration_step_s": 30.0,
}


def turn_rate_deg_s(times_s: np.ndarray, cogs: np.ndarray) -> float | None:
    """Median wrapped dCOG/dt (deg/s) over consecutive reports; None if fewer than 2."""
    if len(cogs) < 2:
        return None
    dc = (np.diff(cogs) + 540.0) % 360.0 - 180.0
    dt = np.diff(times_s)
    ok = dt > 0
    return float(np.median(dc[ok] / dt[ok])) if ok.any() else None


def turn_path(sog_knots: float, cog0: float, rate0: float, leads_s, decay_s: float, step_s: float):
    """(distance m, azimuth deg) from the anchor at each lead, for a decaying turn."""
    leads = np.asarray(leads_s, float)
    t_end = float(leads.max()) if leads.size else 0.0
    t = np.arange(0.0, t_end + step_s, step_s)
    heading = np.radians(cog0 + rate0 * decay_s * (1.0 - np.exp(-t / decay_s)))
    v = knots_to_mps(sog_knots)
    mid = (heading[:-1] + heading[1:]) / 2  # midpoint rule per step
    dx = np.concatenate([[0.0], np.cumsum(v * np.sin(mid) * np.diff(t))])
    dy = np.concatenate([[0.0], np.cumsum(v * np.cos(mid) * np.diff(t))])
    x, y = np.interp(leads, t, dx), np.interp(leads, t, dy)
    return np.hypot(x, y), np.degrees(np.arctan2(x, y)) % 360.0


@dataclass
class ConstantTurnRate(BaseForecaster):
    model_id: str = "constant_turn_rate_v1"
    config: dict = field(default_factory=lambda: dict(DEFAULTS))

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            model_version="1.0.0",
            min_history_reports=1,
            needs_sog_cog=True,
            reads_beyond_own_history=False,
            uses_store_at_predict=False,
            description="Recent median turn rate (capped, exponentially decaying) at anchor SOG.",
        )

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        c = self.config
        anchor, fell_back = select_anchor(history)
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
        t_anchor = anchor["observed_at"]
        r = history.reports
        recent = r.filter(
            (r["observed_at"] <= t_anchor)
            & (r["observed_at"] >= t_anchor - timedelta(minutes=c["window_minutes"]))
            & r["cog_deg"].is_not_null()
            & (r["sog_knots"] >= c["min_sog_kn"])
        )
        if "quality_flags" in recent.columns:
            recent = recent.filter(~recent["quality_flags"].list.contains("sog_implausible"))
        times = np.array([(t - t_anchor).total_seconds() for t in recent["observed_at"]])
        rate = turn_rate_deg_s(times, recent["cog_deg"].to_numpy()) or 0.0
        cap = c["max_rate_deg_s"]
        warnings = []
        if abs(rate) > cap:
            warnings.append(f"turn rate {rate:.3f} deg/s capped at {cap}")
            rate = math.copysign(cap, rate)
        if anchor["sog_knots"] == 0 or anchor["cog_deg"] is None:
            rate, cog0 = 0.0, 0.0
        else:
            cog0 = anchor["cog_deg"]
        leads = [(t - t_anchor).total_seconds() for t in horizon.target_times]
        dist, az = turn_path(
            anchor["sog_knots"],
            cog0,
            rate,
            leads,
            c["decay_minutes"] * 60.0,
            c["integration_step_s"],
        )
        lat, lon = direct(anchor["latitude_deg"], anchor["longitude_deg"], az, dist)
        if fell_back:
            warnings.append("latest report lacks usable SOG/COG; anchored on an earlier report")
        if (history.as_of - t_anchor).total_seconds() / 60 > STALE_WARNING_MINUTES:
            warnings.append("anchor report is stale")
        return np.atleast_1d(lat).tolist(), np.atleast_1d(lon).tolist(), warnings
