"""Any mapped AIS source -> canonical observation schema (build plan §4).

The rules here are documented in docs/data-contracts.md. Source-specific details (column
names, units, sentinels, timestamp format) live in a SourceMapping (config/sources/*.yaml).
"""

from __future__ import annotations

from functools import cache

from maritime_forecast.data.mapping import SourceMapping, TimestampSpec, load_mapping

CANONICAL_VERSION = "canonical-v2"  # v2: generic mapping; output identical to v1 for NOAA
SOURCE_NOAA = "noaa_marinecadastre"

_SOG_TO_KNOTS = {"knots": 1.0, "mps": 3600.0 / 1852.0, "kmh": 1000.0 / 1852.0}

# AIS ship-type code -> coarse class. Kept in sync with docs/data-contracts.md.
_CLASS_FROM_CODE = """
    CASE
      WHEN {c} IS NULL OR {c} = 0 THEN 'unknown'
      WHEN {c} = 30 THEN 'fishing'
      WHEN {c} IN (21, 22, 31, 32, 52) THEN 'tug_tow'
      WHEN {c} BETWEEN 60 AND 69 THEN 'passenger'
      WHEN {c} BETWEEN 70 AND 79 THEN 'cargo'
      WHEN {c} BETWEEN 80 AND 89 THEN 'tanker'
      WHEN {c} IN (36, 37) THEN 'pleasure_sail'
      WHEN {c} BETWEEN 33 AND 35 OR {c} IN (50, 51) OR {c} BETWEEN 53 AND 59 THEN 'service'
      WHEN {c} = 20 OR {c} BETWEEN 23 AND 29 OR {c} BETWEEN 40 AND 49 THEN 'hsc_wig'
      ELSE 'other'
    END
"""


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _col(m: SourceMapping, field: str) -> str | None:
    return _ident(m.columns[field]) if field in m.columns else None


def _str(m: SourceMapping, field: str) -> str:
    """Trimmed string value with null_values and value_maps applied, or NULL if unmapped."""
    c = _col(m, field)
    if c is None:
        return "CAST(NULL AS VARCHAR)"
    s = f"nullif(trim(CAST({c} AS VARCHAR)), '')"
    nulls = m.null_values.get(field, [])
    if nulls:
        s = f"CASE WHEN {s} IN ({', '.join(_lit(v) for v in nulls)}) THEN NULL ELSE {s} END"
    vmap = m.value_maps.get(field)
    if vmap:
        cases = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in vmap.items())
        s = f"CASE {s} {cases} ELSE {s} END"
    return s


def _num(m: SourceMapping, field: str) -> str:
    c = _col(m, field)
    return f"TRY_CAST({c} AS DOUBLE)" if c else "CAST(NULL AS DOUBLE)"


def _ts(col: str | None, spec: TimestampSpec) -> str:
    if col is None:
        return "CAST(NULL AS TIMESTAMPTZ)"
    tz = _lit(spec.timezone)
    if spec.epoch_unit is not None:
        div = "1000.0" if spec.epoch_unit == "ms" else "1.0"
        return f"to_timestamp(TRY_CAST({col} AS DOUBLE) / {div})"
    s = f"trim(CAST({col} AS VARCHAR))"
    if spec.format is not None:
        return f"timezone({tz}, try_strptime({s}, {_lit(spec.format)}))"
    # Honour an explicit offset; read naive values in the declared timezone.
    return (
        f"CASE WHEN regexp_matches({s}, '(Z|[+-][0-9]{{2}}(:?[0-9]{{2}})?)$') "
        f"THEN TRY_CAST({s} AS TIMESTAMPTZ) "
        f"ELSE timezone({tz}, TRY_CAST({s} AS TIMESTAMP)) END"
    )


# First matching reason a staged row is dropped, or NULL if it is kept.
DROP_REASON_SQL = """
    CASE
      WHEN vessel_id IS NULL OR vessel_id = '' THEN 'missing_vessel_id'
      WHEN observed_at IS NULL THEN 'bad_timestamp'
      WHEN latitude_deg IS NULL OR longitude_deg IS NULL
           OR NOT (latitude_deg BETWEEN -90 AND 90 AND longitude_deg BETWEEN -180 AND 180)
        THEN 'bad_coordinates'
      WHEN latitude_deg = 0 AND longitude_deg = 0 THEN 'null_island'
    END
"""


def staged_sql(m: SourceMapping, source: str) -> str:
    """Source rows renamed and parsed to canonical fields; nothing dropped yet."""
    eta_spec = m.eta_timestamp or m.timestamp
    return f"""
        SELECT
            trim(CAST({_col(m, "vessel_id")} AS VARCHAR))      AS vessel_id,
            {_ts(_col(m, "observed_at"), m.timestamp)}         AS observed_at,
            {_num(m, "latitude_deg")}                          AS latitude_deg,
            {_num(m, "longitude_deg")}                         AS longitude_deg,
            {_num(m, "sog")}                                   AS _raw_sog,
            {_num(m, "cog_deg")}                               AS _raw_cog,
            {_num(m, "heading_deg")}                           AS _raw_heading,
            {_str(m, "nav_status")}                            AS nav_status,
            {_str(m, "vessel_type")}                           AS vessel_type,
            {_num(m, "length_m")}                              AS _length,
            {_num(m, "width_m")}                               AS _width,
            {_num(m, "draft_m")}                               AS _draft,
            {_str(m, "transceiver_class")}                     AS _transceiver,
            {_str(m, "destination")}                           AS destination,
            {_ts(_col(m, "eta"), eta_spec)}                    AS eta,
            row_number() OVER ()                               AS _ord
        FROM {source}
    """


def canonical_sql(m: SourceMapping, source: str) -> str:
    """SQL selecting canonical rows from relation/table-function `source` under mapping `m`.

    Drops rows without an id or timestamp and rows with impossible coordinates (incl. 0,0);
    nulls sentinels; removes (vessel_id, observed_at) duplicates; attaches quality flags.
    """
    if m.vessel_class_from == "ais_code":
        vclass = _CLASS_FROM_CODE.format(c="TRY_CAST(vessel_type AS INTEGER)")
    else:
        cases = " ".join(
            f"WHEN {_lit(k.lower())} THEN {_lit(v)}" for k, v in m.vessel_class_text_map.items()
        )
        vclass = f"CASE lower(vessel_type) {cases} ELSE 'unknown' END"
    return f"""
    WITH staged AS (
        {staged_sql(m, source)}
    ),
    typed AS (
        SELECT
            vessel_id, observed_at, latitude_deg, longitude_deg,
            CASE WHEN _raw_sog IS NULL OR _raw_sog < 0
                      OR _raw_sog >= {m.sog_invalid_at_or_above} THEN NULL
                 ELSE _raw_sog * {_SOG_TO_KNOTS[m.sog_unit]!r} END AS sog_knots,
            CASE WHEN _raw_cog < 0 OR _raw_cog >= 360 THEN NULL ELSE _raw_cog END AS cog_deg,
            CASE WHEN _raw_heading < 0 OR _raw_heading >= 360 THEN NULL
                 ELSE _raw_heading END                          AS heading_deg,
            nav_status, vessel_type,
            {vclass}                                           AS vessel_class,
            CASE WHEN _length > 0 THEN _length END             AS length_m,
            CASE WHEN _width > 0 THEN _width END               AS width_m,
            CASE WHEN _draft > 0 THEN _draft END               AS draft_m,
            CASE WHEN _transceiver IN ('A', 'B') THEN _transceiver END AS transceiver_class,
            destination, eta,
            {_lit(m.source_id)}                                AS source,
            {_lit(m.static_fields_provenance)}                 AS static_fields_provenance,
            _raw_sog, _transceiver, _ord
        FROM staged
        WHERE ({DROP_REASON_SQL}) IS NULL
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
            CASE WHEN _raw_sog IS NOT NULL AND sog_knots IS NULL THEN 'sog_sentinel' END,
            CASE WHEN _transceiver IS NOT NULL AND transceiver_class IS NULL
                 THEN 'bad_transceiver_class' END
        ], x -> x IS NOT NULL)                                 AS quality_flags
    FROM dedup
    ORDER BY vessel_id, observed_at
    """


@cache
def noaa_mapping() -> SourceMapping:
    return load_mapping(SOURCE_NOAA)


def noaa_to_canonical_sql(source: str) -> str:
    """Canonical rows from raw NOAA MarineCadastre columns (as written by fetch_noaa.py)."""
    return canonical_sql(noaa_mapping(), source)
