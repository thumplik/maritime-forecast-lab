"""Causal removal of stationary GPS jitter for model windows (build plan §4 processing rules).

A moored or anchored vessel's reported position wanders by metres to tens of metres. For model
inputs that noise looks like motion. Within each run of consecutive 'stationary' reports (same
vessel and segment), each position is replaced by the mean of the run's positions *so far*,
so a report's smoothed position depends only on reports at or before it. Stationary rows are
kept (never dropped) and marked.

CAUSAL ONLY IF ITS INPUT IS: segment.segmented_sql drops position spikes using the *next*
report. For model context, run segmented_sql on observations already filtered to
`observed_at <= as_of` and dejitter that; never dejitter a full-archive segmentation and then
slice it (test_dejitter_prescribed_path_ignores_post_cutoff_spike).

Known bias (not leakage): at a departure the trailing 10-min mean SOG stays below the
stationary threshold for a few minutes, so the last context reports may still be smoothed to
the mooring mean and lag the true position by up to a few hundred metres.
"""

from __future__ import annotations

import polars as pl

DEJITTER_VERSION = "dejitter-v1"


def dejitter_stationary(df: pl.DataFrame) -> pl.DataFrame:
    """Return df with stationary positions replaced by a causal running mean, plus a
    `dejittered` flag. Needs vessel_id, segment_no, observed_at, latitude_deg, longitude_deg,
    motion_state (from segment.segmented_sql). Rows are returned time-ordered per vessel.

    Longitudes are averaged as offsets from the run's first report (wrapped), so a mooring
    on the antimeridian is handled.
    """
    out = df.sort("vessel_id", "observed_at", maintain_order=True).with_columns(
        _stat=pl.col("motion_state") == "stationary"
    )
    # A new run starts whenever the vessel, the segment, or the stationary flag changes.
    run_break = (
        (pl.col("vessel_id") != pl.col("vessel_id").shift())
        | (pl.col("segment_no") != pl.col("segment_no").shift())
        | (pl.col("_stat") != pl.col("_stat").shift())
    ).fill_null(True)
    out = out.with_columns(_run=run_break.cum_sum())
    lon0 = pl.col("longitude_deg").first().over("_run")
    dlon = ((pl.col("longitude_deg") - lon0 + 540) % 360) - 180
    out = out.with_columns(
        _lat_mean=pl.col("latitude_deg").cum_sum().over("_run")
        / pl.int_range(1, pl.len() + 1).over("_run"),
        _dlon_mean=dlon.cum_sum().over("_run") / pl.int_range(1, pl.len() + 1).over("_run"),
        _lon0=lon0,
    )
    return out.with_columns(
        latitude_deg=pl.when("_stat").then("_lat_mean").otherwise("latitude_deg"),
        longitude_deg=pl.when("_stat")
        .then(((pl.col("_lon0") + pl.col("_dlon_mean") + 540) % 360) - 180)
        .otherwise("longitude_deg"),
        dejittered=pl.col("_stat"),
    ).drop("_stat", "_run", "_lat_mean", "_dlon_mean", "_lon0")
