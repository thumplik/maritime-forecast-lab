"""NOAA MarineCadastre point data -> canonical observation schema (build plan §4).

The mapping and every rule here are documented in docs/data-contracts.md.
"""

from __future__ import annotations

CANONICAL_VERSION = "canonical-v1"
SOURCE_NOAA = "noaa_marinecadastre"

# AIS ship-type code -> coarse class. Kept in sync with docs/data-contracts.md.
_CLASS_CASE = """
    CASE
      WHEN VesselType IS NULL OR VesselType = 0 THEN 'unknown'
      WHEN VesselType = 30 THEN 'fishing'
      WHEN VesselType IN (21, 22, 31, 32, 52) THEN 'tug_tow'
      WHEN VesselType BETWEEN 60 AND 69 THEN 'passenger'
      WHEN VesselType BETWEEN 70 AND 79 THEN 'cargo'
      WHEN VesselType BETWEEN 80 AND 89 THEN 'tanker'
      WHEN VesselType IN (36, 37) THEN 'pleasure_sail'
      WHEN VesselType BETWEEN 33 AND 35 OR VesselType IN (50, 51)
           OR VesselType BETWEEN 53 AND 59 THEN 'service'
      WHEN VesselType = 20 OR VesselType BETWEEN 23 AND 29
           OR VesselType BETWEEN 40 AND 49 THEN 'hsc_wig'
      WHEN VesselType BETWEEN 90 AND 99 THEN 'other'
      ELSE 'other'
    END
"""


def noaa_to_canonical_sql(source: str) -> str:
    """SQL selecting canonical rows from a relation/table-function `source` of raw NOAA rows.

    Drops rows with missing/out-of-range coordinates or timestamps; nulls sentinels;
    removes exact (vessel_id, observed_at) duplicates; attaches quality flags.
    """
    return f"""
    WITH typed AS (
        SELECT
            trim(MMSI)                                         AS vessel_id,
            timezone('UTC', BaseDateTime)                      AS observed_at,
            LAT                                                AS latitude_deg,
            LON                                                AS longitude_deg,
            CASE WHEN SOG IS NULL OR SOG < 0 OR SOG >= 102.2 THEN NULL ELSE SOG END
                                                               AS sog_knots,
            CASE WHEN COG IS NULL OR COG < 0 OR COG >= 360 THEN NULL ELSE COG END
                                                               AS cog_deg,
            CASE WHEN Heading IS NULL OR Heading < 0 OR Heading >= 360 THEN NULL
                 ELSE Heading END                              AS heading_deg,
            CASE WHEN Status IS NULL OR Status = 15 THEN NULL
                 ELSE CAST(Status AS VARCHAR) END              AS nav_status,
            CASE WHEN VesselType IS NULL OR VesselType = 0 THEN NULL
                 ELSE CAST(VesselType AS VARCHAR) END          AS vessel_type,
            {_CLASS_CASE}                                      AS vessel_class,
            CASE WHEN Length > 0 THEN Length END               AS length_m,
            CASE WHEN Width > 0 THEN Width END                 AS width_m,
            CASE WHEN Draft > 0 THEN Draft END                 AS draft_m,
            nullif(trim(TransceiverClass), '')                 AS transceiver_class,
            CAST(NULL AS VARCHAR)                              AS destination,
            CAST(NULL AS TIMESTAMPTZ)                          AS eta,
            '{SOURCE_NOAA}'                                    AS source,
            'backfilled'                                       AS static_fields_provenance,
            SOG                                                AS _raw_sog,
            row_number() OVER ()                               AS _ord
        FROM {source}
        WHERE BaseDateTime IS NOT NULL
          AND LAT BETWEEN -90 AND 90 AND LON BETWEEN -180 AND 180
          AND NOT (LAT = 0 AND LON = 0)
          AND MMSI IS NOT NULL
    ),
    dedup AS (
        SELECT * FROM typed
        -- Deterministic tie-break on content (scan order is not stable under parallel reads).
        QUALIFY row_number() OVER (
            PARTITION BY vessel_id, observed_at
            ORDER BY latitude_deg, longitude_deg, sog_knots NULLS LAST, cog_deg NULLS LAST,
                     heading_deg NULLS LAST, _ord
        ) = 1
    )
    SELECT
        vessel_id, observed_at, latitude_deg, longitude_deg, sog_knots, cog_deg, heading_deg,
        nav_status, vessel_type, vessel_class, length_m, width_m, draft_m, transceiver_class,
        destination, eta, source, static_fields_provenance,
        list_filter([
            CASE WHEN NOT regexp_matches(vessel_id, '^[0-9]{{9}}$') THEN 'bad_mmsi' END,
            CASE WHEN sog_knots > 60 THEN 'sog_implausible' END,
            CASE WHEN _raw_sog IS NOT NULL AND sog_knots IS NULL THEN 'sog_sentinel' END
        ], x -> x IS NOT NULL)                                 AS quality_flags
    FROM dedup
    ORDER BY vessel_id, observed_at
    """
