"""Grid resampling and windowing on SYNTHETIC tracks."""

from datetime import UTC, datetime, timedelta

import duckdb
import numpy as np
import polars as pl
import pytest

from maritime_forecast.evaluation.windows import grid_sql, make_windows


@pytest.fixture
def grid():
    t0 = datetime(2023, 6, 1, 0, 3, tzinfo=UTC)
    rows = []
    # vessel A: one segment, report every 2 min for 7 h, moving north 0.001 deg/min
    for k in range(0, 7 * 60, 2):
        rows.append(("A", 1, t0 + timedelta(minutes=k), 29.0 + 0.001 * k, -94.0, 3.6, 0.0))
    # vessel B: segment too short for a window
    for k in range(0, 60, 2):
        rows.append(("B", 1, t0 + timedelta(minutes=k), 28.0, -95.0, 0.0, None))
    obs = pl.DataFrame(
        rows,
        orient="row",
        schema=[
            "vessel_id",
            "segment_no",
            "observed_at",
            "latitude_deg",
            "longitude_deg",
            "sog_knots",
            "cog_deg",
        ],
    )
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.register("obs", obs.to_arrow())
    return con.execute(grid_sql("obs", 10)).pl()


def test_grid_is_clock_aligned_and_interpolated(grid):
    a = grid.filter(pl.col("vessel_id") == "A")
    assert a["grid_t"][0] == datetime(2023, 6, 1, 0, 10, tzinfo=UTC)
    # 7 minutes after first report -> lat 29.007
    assert a["lat"][0] == pytest.approx(29.007)
    assert (a["grid_t"].diff().drop_nulls() == timedelta(minutes=10)).all()


def test_windows(grid):
    w = make_windows(grid, n_ctx=18, n_tgt=18, stride=18)
    # A has 41 grid points (00:10 .. 07:00 minus tail): only the start-0 window fits 36 steps
    assert w.lat.shape == (1, 36)
    assert set(w.vessel_id) == {"A"}
    np.testing.assert_allclose(np.diff(w.lat[0]), 0.01, atol=1e-9)
    assert w.anchor_sog[0] == pytest.approx(3.6)
