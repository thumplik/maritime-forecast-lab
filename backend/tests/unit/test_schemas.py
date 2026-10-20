"""Observation contract: validation rules and agreement with the canonical Parquet columns."""

from datetime import UTC, datetime, timedelta, timezone

import duckdb
import pytest
from pydantic import ValidationError

from maritime_forecast.data.canonical import noaa_to_canonical_sql
from maritime_forecast.schemas import Observation

BASE = dict(
    vessel_id="367000001",
    observed_at=datetime(2023, 6, 1, tzinfo=UTC),
    latitude_deg=29.3,
    longitude_deg=-94.7,
    source="noaa_marinecadastre",
    static_fields_provenance="backfilled",
)


def test_valid_minimal():
    o = Observation(**BASE)
    assert o.quality_flags == [] and o.vessel_class == "unknown"


@pytest.mark.parametrize(
    "bad",
    [
        {"observed_at": datetime(2023, 6, 1)},  # naive
        {"observed_at": datetime(2023, 6, 1, tzinfo=timezone(timedelta(hours=-5)))},  # not UTC
        {"latitude_deg": 91.0},
        {"longitude_deg": -180.5},
        {"cog_deg": 360.0},
        {"sog_knots": -1.0},
        {"transceiver_class": "C"},
        {"static_fields_provenance": "guessed"},
        {"unexpected_field": 1},
    ],
)
def test_rejects(bad):
    with pytest.raises(ValidationError):
        Observation(**(BASE | bad))


def test_canonical_columns_match_contract():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("""CREATE TABLE raw AS SELECT '367000001' MMSI, TIMESTAMP '2023-06-01' BaseDateTime,
        29.3 LAT, -94.7 LON, 10.0 SOG, 90.0 COG, 511.0 Heading, 70 VesselType, 0 Status,
        200.0 Length, 30.0 Width, 9.0 Draft, 'A' TransceiverClass""")
    rel = con.execute(noaa_to_canonical_sql("raw"))
    cols = [d[0] for d in rel.description]
    assert cols == list(Observation.model_fields)
    row = dict(zip(cols, rel.fetchone(), strict=True))
    Observation(**row)  # a real canonical row validates against the contract
