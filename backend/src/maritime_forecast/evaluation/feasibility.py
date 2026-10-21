"""Feasibility flags for forecast paths (build plan §6.5): land, speed, acceleration.

Flags describe a forecast; they never change it (forecasts are not constrained to water).
Every array is [N cutoffs, S grid steps]; step k's flag concerns the move from the previous
point (the anchor report for k = 0) to point k:

  land_point     point k is infeasible in the navigable mask: on OSM land and not in a cell
                 that permitted training traffic used (geo.navigable_mask; None without mask)
  land_crossing  the move to point k passes through an infeasible cell (None without mask)
  speed          the implied speed of that move exceeds `max_speed_kn` (a jump)
  accel          the implied speed changes faster than `max_accel_kn_per_min` from the previous
                 move (from the anchor's reported SOG for k = 0, when it has one)

`up_to(flags, k)` turns per-step flags into "anywhere in the first k steps".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from maritime_forecast.geo.geodesic import distance_m
from maritime_forecast.geo.navigable_mask import NavigableMask

FEASIBILITY_VERSION = "feasibility-v1"
MPS_PER_KNOT = 1852 / 3600


@dataclass(frozen=True)
class FeasibilityConfig:
    # Same bound as the canonical `sog_implausible` flag.
    max_speed_kn: float = 60.0
    # Generous: flags only clearly impossible speed changes, not hard manoeuvres.
    max_accel_kn_per_min: float = 2.0

    def as_dict(self) -> dict:
        return {**asdict(self), "version": FEASIBILITY_VERSION}


def feasibility_flags(
    anchor_lat: np.ndarray,
    anchor_lon: np.ndarray,
    anchor_sog_kn: np.ndarray,
    first_dt_s: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    step_s: float,
    mask: NavigableMask | None,
    cfg: FeasibilityConfig | None = None,
) -> dict[str, np.ndarray | None]:
    """Per-step flags. Rows with no forecast (NaN positions) get False everywhere; callers
    record those rows as failures, not as feasible."""
    cfg = cfg or FeasibilityConfig()
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    prev_lat = np.concatenate([np.asarray(anchor_lat, float)[:, None], lat[:, :-1]], axis=1)
    prev_lon = np.concatenate([np.asarray(anchor_lon, float)[:, None], lon[:, :-1]], axis=1)
    dt = np.full(lat.shape, float(step_s))
    dt[:, 0] = np.asarray(first_dt_s, float)
    d = distance_m(prev_lat, prev_lon, lat, lon)
    with np.errstate(invalid="ignore", divide="ignore"):
        v_kn = np.where(dt > 0, d / dt / MPS_PER_KNOT, np.where(d > 0, np.inf, 0.0))
        prev_v = np.concatenate([np.asarray(anchor_sog_kn, float)[:, None], v_kn[:, :-1]], axis=1)
        accel = np.abs(v_kn - prev_v) / (dt / 60)
    speed = np.nan_to_num(v_kn, nan=0.0) > cfg.max_speed_kn
    accel_flag = np.nan_to_num(accel, nan=0.0) > cfg.max_accel_kn_per_min
    land_point = land_crossing = None
    if mask is not None:
        land_point = mask.is_infeasible(lat, lon)
        land_crossing = mask.segment_infeasible(prev_lat, prev_lon, lat, lon)
    return {
        "land_point": land_point,
        "land_crossing": land_crossing,
        "speed": speed,
        "accel": accel_flag,
    }


def up_to(flags: np.ndarray, k: int) -> np.ndarray:
    """[N] — whether any of the first k steps is flagged."""
    return flags[:, :k].any(axis=1)
