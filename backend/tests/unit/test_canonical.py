"""Canonicalization rules on a tiny SYNTHETIC raw NOAA table (not real AIS)."""

import duckdb
import pytest

from maritime_forecast.data.canonical import noaa_to_canonical_sql

RAW = """
CREATE TABLE raw AS SELECT * FROM (VALUES
  ('367000001', TIMESTAMP '2023-06-01 00:00:00', 29.3, -94.7, 10.0, 90.0, 511.0, 70, 15, 200.0, 30.0, 0.0, 'A'),
  ('367000001', TIMESTAMP '2023-06-01 00:00:00', 29.3, -94.7, 10.0, 90.0, 511.0, 70, 15, 200.0, 30.0, 0.0, 'A'),
  ('367000001', TIMESTAMP '2023-06-01 00:01:00', 29.3, -94.69, 102.3, 360.0, 88.0, 70, 0, 200.0, 30.0, 9.5, 'A'),
  ('367000001', TIMESTAMP '2023-06-01 00:02:00', 91.0, -94.68, 10.0, 90.0, 88.0, 70, 0, 200.0, 30.0, 9.5, 'A'),
  ('12345',     TIMESTAMP '2023-06-01 00:00:00', 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, NULL, NULL, NULL, 'B'),
  ('12345',     TIMESTAMP '2023-06-01 00:01:00', 29.0, -94.0, 70.0, 10.0, 10.0, 31, 0, NULL, NULL, NULL, 'B')
) t(MMSI, BaseDateTime, LAT, LON, SOG, COG, Heading, VesselType, Status, Length, Width, Draft,
    TransceiverClass)
"""


@pytest.fixture
def rows():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(RAW)
    return con.execute(noaa_to_canonical_sql("raw")).fetchall(), [d[0] for d in con.description]


def test_drops_bad_coords_and_duplicates(rows):
    data, cols = rows
    assert len(data) == 3  # 1 dup removed, LAT 91 dropped, (0,0) dropped


def test_sentinels_and_flags(rows):
    data, cols = rows
    r = [dict(zip(cols, x, strict=True)) for x in data]
    first, second = [x for x in r if x["vessel_id"] == "367000001"]
    assert first["heading_deg"] is None and first["nav_status"] is None
    assert first["draft_m"] is None and first["vessel_class"] == "cargo"
    assert second["sog_knots"] is None and second["cog_deg"] is None
    assert "sog_sentinel" in second["quality_flags"]
    assert second["heading_deg"] == 88.0
    tug = next(x for x in r if x["vessel_id"] == "12345")
    assert set(tug["quality_flags"]) == {"bad_mmsi", "sog_implausible"}
    assert tug["vessel_class"] == "tug_tow"
    assert tug["static_fields_provenance"] == "backfilled"
    assert str(first["observed_at"].tzinfo) in ("UTC", "Etc/UTC")
