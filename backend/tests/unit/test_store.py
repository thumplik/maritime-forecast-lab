"""Partitioned Parquet store on SYNTHETIC canonical rows."""

from datetime import UTC, date, datetime, timedelta

import pytest

from maritime_forecast.config import BBox
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, 22, 0, tzinfo=UTC)


def _rows_sql(n=36, vessels=("367000001", "367000002")):
    # every 10 min from 2023-06-01 22:00 UTC for 6 h -> spans two UTC days
    vals = ", ".join(
        f"('{v}', TIMESTAMPTZ '{(T0 + timedelta(minutes=10 * k)).isoformat()}', "
        f"{29.0 + 0.01 * k}, {-94.0 - (1.0 if v.endswith('2') else 0.0)})"
        for v in vessels
        for k in range(n)
    )
    return (
        f"SELECT vessel_id, observed_at, latitude_deg, longitude_deg, 'cargo' AS vessel_class "
        f"FROM (VALUES {vals}) t(vessel_id, observed_at, latitude_deg, longitude_deg)"
    )


@pytest.fixture
def store(tmp_path):
    s = ParquetStore(tmp_path / "canonical")
    s.write("ds", _rows_sql())
    return s


def test_partitions_by_utc_day(store):
    assert store.datasets() == ["ds"]
    assert store.partitions("ds") == [date(2023, 6, 1), date(2023, 6, 2)]
    assert store.scan("ds").pl().height == 72


def test_files_prunes_by_date(store):
    late = datetime(2023, 6, 2, 1, tzinfo=UTC)
    assert [p.parent.name for p in store.files("ds", start=late)] == ["date=2023-06-02"]
    early_end = datetime(2023, 6, 1, 23, tzinfo=UTC)
    assert [p.parent.name for p in store.files("ds", end=early_end)] == ["date=2023-06-01"]
    # end exactly at midnight excludes the next day
    assert len(store.files("ds", end=datetime(2023, 6, 2, tzinfo=UTC))) == 1


def test_scan_half_open_window_bbox_vessels_columns(store):
    a, b = T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)
    df = store.scan("ds", a, b, vessel_ids=["367000001"], columns=["vessel_id", "observed_at"]).pl()
    assert df.columns == ["vessel_id", "observed_at"]
    assert df["observed_at"].to_list() == [a, a + timedelta(minutes=10), a + timedelta(minutes=20)]
    only_east = store.scan("ds", bbox=BBox("e", 28, 31, -94.5, -93.5)).pl()
    assert set(only_east["vessel_id"]) == {"367000001"}


def test_history_is_causal(store):
    as_of = T0 + timedelta(hours=2, minutes=5)  # between two reports, crossing midnight
    h = store.history("ds", "367000001", as_of, hours=1)
    assert h["observed_at"].max() <= as_of
    assert h["observed_at"].min() > as_of - timedelta(hours=1)
    assert h.height == 6
    exact = store.history("ds", "367000001", T0 + timedelta(hours=2), hours=1)
    assert exact["observed_at"].max() == T0 + timedelta(hours=2)  # at as_of is included


def test_track_and_vessels(store):
    assert store.track("ds", "367000002").height == 36
    v = store.vessels("ds", limit=1, offset=1)
    assert v["vessel_id"].to_list() == ["367000002"] and v["reports"][0] == 36


def test_write_replaces_only_days_written(store):
    store.write("ds", _rows_sql(n=3, vessels=("367000009",)))  # 22:00-22:20 on 06-01 only
    assert store.scan("ds", end=datetime(2023, 6, 2, tzinfo=UTC)).pl()[
        "vessel_id"
    ].unique().to_list() == ["367000009"]
    assert store.scan("ds", start=datetime(2023, 6, 2, tzinfo=UTC)).pl().height == 48


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 200])
def test_rejects_unsafe_dataset_ids(store, bad):
    with pytest.raises(ValueError):
        store.partitions(bad)


def test_rejects_unsafe_columns_and_naive_times(store):
    with pytest.raises(ValueError):
        store.scan("ds", columns=["vessel_id; DROP TABLE x"])
    with pytest.raises(ValueError):
        store.scan("ds", start=datetime(2023, 6, 1))
    with pytest.raises(ValueError):
        store.vessels("ds", limit=0)


def test_vessel_ids_are_bound_not_interpolated(store):
    assert store.scan("ds", vessel_ids=["' OR 1=1 --"]).pl().height == 0


def test_unknown_dataset(store):
    with pytest.raises(KeyError):
        store.partitions("nope")
