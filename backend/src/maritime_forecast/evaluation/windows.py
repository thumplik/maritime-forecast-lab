"""Fixed-length regular-grid windows, as used by the calibration paper (§6.6).

Each segment (no gap > threshold) is resampled onto a regular grid by linear interpolation
*within* the segment, then cut into windows of n_ctx context + n_tgt target steps.

CALIBRATION ONLY (P-paper). This mirrors the paper's preprocessing and is not causal:
  * the last context grid point is interpolated toward the first report *after* it;
  * a window exists only if the segment continues through all target steps, so vessels
    that stop or leave coverage drop out instead of being recorded as not evaluable.
Product evaluation uses the cutoff sampler + evaluation.truth, with context built only from
observations at or before `as_of`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from maritime_forecast.geo.geodesic import lon_lerp_sql


def grid_sql(observations: str, step_minutes: int = 10) -> str:
    """Resample each (vessel_id, segment_no) onto a clock-aligned regular grid.

    `observations` needs vessel_id, segment_no, observed_at, latitude_deg, longitude_deg,
    sog_knots, cog_deg. Grid points lie within [first, last] report of the segment, so no
    extrapolation. Also returns the SOG/COG of the latest raw report at or before each grid
    time (the "anchor report" used by paper-compatible constant velocity).
    """
    step = f"INTERVAL {step_minutes} MINUTE"
    frac = "epoch(grid_t - p_t) / epoch(n_t - p_t)"
    return f"""
    WITH bounds AS (
        SELECT vessel_id, segment_no, min(observed_at) AS t0, max(observed_at) AS t1
        FROM {observations} GROUP BY ALL
    ),
    grid AS (
        SELECT vessel_id, segment_no,
            unnest(generate_series(
                CASE WHEN time_bucket({step}, t0) = t0 THEN t0
                     ELSE time_bucket({step}, t0) + {step} END,
                t1, {step})) AS grid_t
        FROM bounds
    ),
    prev AS (
        SELECT g.*, o.observed_at AS p_t, o.latitude_deg AS p_lat, o.longitude_deg AS p_lon,
               o.sog_knots AS anchor_sog, o.cog_deg AS anchor_cog
        FROM grid g ASOF JOIN {observations} o
          ON g.vessel_id = o.vessel_id AND g.segment_no = o.segment_no
         AND g.grid_t >= o.observed_at
    ),
    both_ AS (
        SELECT p.*, o.observed_at AS n_t, o.latitude_deg AS n_lat, o.longitude_deg AS n_lon
        FROM prev p ASOF JOIN {observations} o
          ON p.vessel_id = o.vessel_id AND p.segment_no = o.segment_no
         AND p.grid_t <= o.observed_at
    )
    SELECT vessel_id, segment_no, grid_t,
        CASE WHEN n_t = p_t THEN p_lat
             ELSE p_lat + epoch(grid_t - p_t) / epoch(n_t - p_t) * (n_lat - p_lat) END AS lat,
        CASE WHEN n_t = p_t THEN p_lon
             ELSE {lon_lerp_sql("p_lon", "n_lon", frac)} END AS lon,
        anchor_sog, anchor_cog
    FROM both_
    ORDER BY vessel_id, segment_no, grid_t
    """


@dataclass
class Windows:
    vessel_id: np.ndarray  # (W,)
    start_t: np.ndarray  # (W,) datetime64 of first context step
    lat: np.ndarray  # (W, n_ctx + n_tgt)
    lon: np.ndarray
    anchor_sog: np.ndarray  # (W,) raw-report SOG at the last context step
    anchor_cog: np.ndarray
    n_ctx: int
    n_tgt: int


def make_windows(grid: pl.DataFrame, n_ctx: int = 18, n_tgt: int = 18, stride: int = 18) -> Windows:
    """Cut each segment's grid into windows of n_ctx + n_tgt steps, starting every `stride` steps.

    `grid` must be sorted by vessel_id, segment_no, grid_t (as grid_sql returns it).
    """
    L = n_ctx + n_tgt
    seg_key = grid.select(
        (pl.col("vessel_id") + "/" + pl.col("segment_no").cast(pl.Utf8)).alias("k")
    )["k"].to_numpy()
    change = np.r_[True, seg_key[1:] != seg_key[:-1]]
    seg_start = np.flatnonzero(change)
    seg_end = np.r_[seg_start[1:], len(seg_key)]
    starts = [
        np.arange(s, e - L + 1, stride)
        for s, e in zip(seg_start, seg_end, strict=True)
        if e - s >= L
    ]
    starts = np.concatenate(starts) if starts else np.array([], dtype=int)
    idx = starts[:, None] + np.arange(L)[None, :]
    lat = grid["lat"].to_numpy()
    lon = grid["lon"].to_numpy()
    sog = grid["anchor_sog"].fill_null(np.nan).to_numpy().astype(float)
    cog = grid["anchor_cog"].fill_null(np.nan).to_numpy().astype(float)
    anchor = starts + n_ctx - 1
    return Windows(
        vessel_id=grid["vessel_id"].to_numpy()[starts],
        start_t=grid["grid_t"].to_numpy()[starts],
        lat=lat[idx],
        lon=lon[idx],
        anchor_sog=sog[anchor],
        anchor_cog=cog[anchor],
        n_ctx=n_ctx,
        n_tgt=n_tgt,
    )
