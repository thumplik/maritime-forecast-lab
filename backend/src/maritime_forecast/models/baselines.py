"""Deterministic baselines (build plan §5 B0), as pure functions over arrays.

Milestone 0 keeps these as functions; the TrajectoryForecaster contract and registry
arrive in Milestone 2 once two implementations need them.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike

from maritime_forecast.geo.geodesic import direct, inverse, knots_to_mps


def persistence(lat0: ArrayLike, lon0: ArrayLike, lead_s: ArrayLike):
    """Stay at the last position for every lead time. Shapes broadcast to (..., n_leads)."""
    lead = np.asarray(lead_s, float)
    lat = np.broadcast_to(np.asarray(lat0, float)[..., None], (*np.shape(lat0), lead.size))
    lon = np.broadcast_to(np.asarray(lon0, float)[..., None], (*np.shape(lon0), lead.size))
    return lat.copy(), lon.copy()


def constant_velocity(
    lat0: ArrayLike, lon0: ArrayLike, sog_knots: ArrayLike, cog_deg: ArrayLike, lead_s: ArrayLike
):
    """Travel SOG x lead along COG on the WGS84 geodesic from the anchor report.

    Missing COG with non-zero SOG yields NaN (caller records a failure); zero SOG
    with missing COG degenerates to persistence.
    """
    lat0 = np.asarray(lat0, float)[..., None]
    lon0 = np.asarray(lon0, float)[..., None]
    sog = np.asarray(sog_knots, float)[..., None]
    cog = np.asarray(cog_deg, float)[..., None]
    lead = np.asarray(lead_s, float)
    dist = knots_to_mps(sog) * lead
    cog_filled = np.where(np.isnan(cog) & (sog == 0), 0.0, cog)
    lat, lon = direct(lat0, lon0, np.nan_to_num(cog_filled), dist)
    bad = np.broadcast_to(np.isnan(cog_filled) | np.isnan(sog), lat.shape)
    lat = np.where(bad, np.nan, lat)
    lon = np.where(bad, np.nan, lon)
    return lat, lon


def velocity_from_last_two(lat: ArrayLike, lon: ArrayLike, dt_s: float):
    """Speed (knots) and course (deg) from the last two points of a regular grid.

    `lat`/`lon` have shape (..., n_context). Used by the paper-compatible CV mode, which
    extrapolates the displacement between the last two context steps.
    """
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    d, brg = inverse(lat[..., -2], lon[..., -2], lat[..., -1], lon[..., -1])
    return d / dt_s / knots_to_mps(1.0), brg
