"""Gap-aware track segmentation and outlier flagging (build plan §4 processing rules).

Implemented as DuckDB SQL over canonical observations so it scales to a month of Gulf data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

SEGMENT_VERSION = "segment-v2"  # v2: trailing 30-min SOG mean/min


@dataclass(frozen=True)
class SegmentConfig:
    gap_minutes: float = 30.0
    max_implied_speed_kn: float = 50.0  # single-point spikes above this are dropped
    stationary_sog_kn: float = 0.5
    underway_sog_kn: float = 2.0

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_SEGMENT_CONFIG = SegmentConfig()


def segmented_sql(source: str, cfg: SegmentConfig = DEFAULT_SEGMENT_CONFIG) -> str:
    """Return SQL producing canonical columns plus segment ids and motion features.

    Steps:
      1. Drop single-point position spikes: a row whose implied speed from both its previous
         and next neighbour exceeds `max_implied_speed_kn` (a glitch, not a fast vessel).
      2. Start a new segment when the gap to the previous report exceeds `gap_minutes`.
      3. Add trailing 10- and 30-minute SOG statistics (only reports at or before each row —
         no look-ahead).

    CAUSALITY: the spike test uses lead(), i.e. the *next* report. Applied to a full archive,
    whether the report at a cutoff survives depends on the first report after the cutoff.
    That is acceptable for truth and for the Milestone 0 audit (affects ~5e-6 of rows), but
    model context must be built by running this on observations already filtered to
    `observed_at <= as_of` (Milestone 2 leakage invariance test enforces it).
    """
    gap_s = cfg.gap_minutes * 60
    vmax_mps = cfg.max_implied_speed_kn * 1852 / 3600
    # Equirectangular distance is adequate for spike detection between consecutive reports.
    dist = (
        "6371008.8 * sqrt(pow(radians({lat2}-{lat1}),2) + "
        "pow(radians({lon2}-{lon1})*cos(radians(({lat1}+{lat2})/2)),2))"
    )
    d_prev = dist.format(lat1="p_lat", lon1="p_lon", lat2="latitude_deg", lon2="longitude_deg")
    d_next = dist.format(lat1="latitude_deg", lon1="longitude_deg", lat2="n_lat", lon2="n_lon")
    return f"""
    WITH nb AS (
        SELECT *,
            lag(latitude_deg)  OVER w AS p_lat, lag(longitude_deg)  OVER w AS p_lon,
            lag(observed_at)   OVER w AS p_t,
            lead(latitude_deg) OVER w AS n_lat, lead(longitude_deg) OVER w AS n_lon,
            lead(observed_at)  OVER w AS n_t
        FROM {source}
        WINDOW w AS (PARTITION BY vessel_id ORDER BY observed_at)
    ),
    spikes AS (
        SELECT *,
            ({d_prev}) / greatest(epoch(observed_at - p_t), 1) > {vmax_mps}
            AND ({d_next}) / greatest(epoch(n_t - observed_at), 1) > {vmax_mps} AS is_spike
        FROM nb
    ),
    clean AS (
        SELECT * EXCLUDE (p_lat, p_lon, p_t, n_lat, n_lon, n_t, is_spike)
        FROM spikes WHERE is_spike IS NOT TRUE
    ),
    gaps AS (
        SELECT *,
            epoch(observed_at - lag(observed_at) OVER w) AS dt_prev_s
        FROM clean
        WINDOW w AS (PARTITION BY vessel_id ORDER BY observed_at)
    )
    SELECT *,
        sum(CASE WHEN dt_prev_s IS NULL OR dt_prev_s > {gap_s} THEN 1 ELSE 0 END)
            OVER (PARTITION BY vessel_id ORDER BY observed_at
                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS segment_no,
        avg(sog_knots) OVER (PARTITION BY vessel_id ORDER BY observed_at
                  RANGE BETWEEN INTERVAL 10 MINUTES PRECEDING AND CURRENT ROW) AS sog_mean_10m,
        avg(sog_knots) OVER w30 AS sog_mean_30m,
        min(sog_knots) OVER w30 AS sog_min_30m,
        count(*) OVER (PARTITION BY vessel_id ORDER BY observed_at
                  RANGE BETWEEN INTERVAL 3 HOURS PRECEDING AND CURRENT ROW) AS n_reports_3h
    FROM gaps
    WINDOW w30 AS (PARTITION BY vessel_id ORDER BY observed_at
                   RANGE BETWEEN INTERVAL 30 MINUTES PRECEDING AND CURRENT ROW)
    """


def gap_segments(observed_at: Sequence[datetime], gap_minutes: float) -> list[int]:
    """Segment number per report for one time-ordered track: a new segment starts after any
    gap longer than `gap_minutes`. Pure-Python twin of segment_no in segmented_sql, used for
    display (a renderer must never connect points across segments)."""
    out: list[int] = []
    seg, prev = 0, None
    limit = timedelta(minutes=gap_minutes)
    for t in observed_at:
        if prev is not None:
            if t < prev:
                raise ValueError("observed_at must be sorted")
            if t - prev > limit:
                seg += 1
        out.append(seg)
        prev = t
    return out


def segments_and_gaps(
    observed_at: Sequence[datetime], segment: Sequence[int]
) -> tuple[list[dict], list[dict]]:
    """Summaries of each segment and of each gap between consecutive segments."""
    segments: list[dict] = []
    gaps: list[dict] = []
    start = 0
    for i in range(1, len(observed_at) + 1):
        if i == len(observed_at) or segment[i] != segment[i - 1]:
            segments.append(
                {"segment": segment[start], "start": observed_at[start],
                 "end": observed_at[i - 1], "n_points": i - start}
            )  # fmt: skip
            if i < len(observed_at):
                a, b = observed_at[i - 1], observed_at[i]
                gaps.append({"start": a, "end": b, "duration_s": (b - a).total_seconds()})
            start = i
    return segments, gaps
