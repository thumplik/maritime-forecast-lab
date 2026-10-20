"""Withheld ground truth at target times (build plan §6.1 step 4).

Truth at time t = linear interpolation between the last report at or before t and the first
report at or after t, from the same vessel, *only* when those two reports are no more than
`max_bracket_gap` apart. Otherwise the target is not evaluable. Truth is never bridged across
a coverage gap.
"""

from __future__ import annotations

from maritime_forecast.geo.geodesic import lon_lerp_sql

NOT_EVALUABLE_REASONS = (
    "no_report_before",  # vessel has no report at/before target in the truth source (rare)
    "no_report_after",  # vessel never reappears in the truth source after target
    "truth_gap",  # bracketing reports exist but are too far apart
)


def truth_sql(
    targets: str,
    observations: str,
    max_bracket_gap_minutes: float = 30.0,
    prev_cols: tuple[str, ...] = (),
) -> str:
    """SQL joining `targets(cutoff_id, vessel_id, target_t, ...)` to bracketed truth.

    `observations` must have vessel_id, observed_at, latitude_deg, longitude_deg and may
    already be restricted to a truth region. Output adds p_t/p_lat/p_lon and n_t/n_lat/n_lon
    (the bracketing reports), truth_lat, truth_lon, bracket_gap_s, evaluable, reason.
    `prev_cols` copies further columns of the preceding report as p_<col> (e.g. cog_deg, for
    the true course when a report falls exactly on the target time).
    """
    gap_s = max_bracket_gap_minutes * 60
    extra = "".join(f", o.{c} AS p_{c}" for c in prev_cols)
    return f"""
    WITH prev AS (
        SELECT t.*, o.observed_at AS p_t, o.latitude_deg AS p_lat, o.longitude_deg AS p_lon{extra}
        FROM {targets} t ASOF LEFT JOIN {observations} o
          ON t.vessel_id = o.vessel_id AND t.target_t >= o.observed_at
    ),
    both_ AS (
        SELECT p.*, o.observed_at AS n_t, o.latitude_deg AS n_lat, o.longitude_deg AS n_lon
        FROM prev p ASOF LEFT JOIN {observations} o
          ON p.vessel_id = o.vessel_id AND p.target_t <= o.observed_at
    ),
    j AS (
        SELECT *,
            epoch(n_t - p_t) AS bracket_gap_s,
            CASE WHEN n_t = p_t THEN 0.0
                 ELSE epoch(target_t - p_t) / nullif(epoch(n_t - p_t), 0) END AS w
        FROM both_
    )
    SELECT * EXCLUDE (w),
        p_lat + w * (n_lat - p_lat) AS truth_lat,
        {lon_lerp_sql("p_lon", "n_lon", "w")} AS truth_lon,
        CASE WHEN p_t IS NULL THEN 'no_report_before'
             WHEN n_t IS NULL THEN 'no_report_after'
             WHEN bracket_gap_s > {gap_s} THEN 'truth_gap'
        END AS reason,
        p_t IS NOT NULL AND n_t IS NOT NULL AND bracket_gap_s <= {gap_s} AS evaluable
    FROM j
    """
