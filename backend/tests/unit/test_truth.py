"""Truth bracketing on SYNTHETIC observations."""

import duckdb
import pytest

from maritime_forecast.evaluation.truth import truth_sql


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone='UTC'")
    c.execute("""
    CREATE TABLE obs AS SELECT * FROM (VALUES
      ('A', TIMESTAMPTZ '2023-06-01 00:00:00+00', 29.0, -94.0),
      ('A', TIMESTAMPTZ '2023-06-01 00:10:00+00', 29.1, -94.0),
      ('A', TIMESTAMPTZ '2023-06-01 02:00:00+00', 29.5, -94.0),
      ('B', TIMESTAMPTZ '2023-06-01 00:00:00+00', 28.0, -95.0)
    ) t(vessel_id, observed_at, latitude_deg, longitude_deg)""")
    c.execute("""
    CREATE TABLE tg AS SELECT * FROM (VALUES
      (1, 'A', TIMESTAMPTZ '2023-06-01 00:05:00+00'),
      (2, 'A', TIMESTAMPTZ '2023-06-01 01:00:00+00'),
      (3, 'A', TIMESTAMPTZ '2023-06-01 03:00:00+00'),
      (4, 'B', TIMESTAMPTZ '2023-06-01 00:00:00+00'),
      (5, 'A', TIMESTAMPTZ '2023-05-31 23:00:00+00')
    ) t(cutoff_id, vessel_id, target_t)""")
    return c


def test_bracketing(con):
    rows = con.execute(
        f"SELECT cutoff_id, evaluable, reason, truth_lat FROM ({truth_sql('tg', 'obs')}) "
        "ORDER BY cutoff_id"
    ).fetchall()
    by = {r[0]: r for r in rows}
    assert by[1][1] and by[1][3] == pytest.approx(29.05)  # interpolated midpoint
    assert by[2][1] is False and by[2][2] == "truth_gap"  # 110-min bracket, never bridged
    assert by[3][2] == "no_report_after"
    assert by[4][1] and by[4][3] == pytest.approx(28.0)  # exact hit
    assert by[5][2] == "no_report_before"
