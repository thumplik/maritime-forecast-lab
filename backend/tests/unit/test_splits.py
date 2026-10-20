"""Split manifests: disjointness on their stated axes, purge, and round-trip (§6.2)."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from maritime_forecast.evaluation.splits import P_PAPER_ASSIGNMENT, p_known, p_paper, p_unseen
from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment

MMSIS = [str(366_000_000 + i * 7919) for i in range(5000)]
TIMES = [datetime(2023, 6, 1, tzinfo=UTC) + timedelta(hours=h) for h in range(0, 30 * 24, 5)]


def test_p_unseen_vessel_sets_disjoint():
    m = p_unseen("d")
    by = {}
    for v in MMSIS:
        by.setdefault(m.vessel_assignment.assign(v), set()).add(v)
    assert set(by) == {"train", "calibration", "test"}
    assert not (by["train"] & by["test"]) and not (by["train"] & by["calibration"])
    assert not (by["calibration"] & by["test"])
    # proportions roughly 70/10/20
    assert abs(len(by["test"]) / len(MMSIS) - 0.2) < 0.03


def test_p_unseen_point_membership_requires_both_axes():
    m = p_unseen("d")
    for v in MMSIS[:300]:
        vs = m.vessel_assignment.assign(v)
        for t in TIMES:
            s = m.split_of(v, t)
            if s is not None:
                assert s == vs == m.time_split(t)


def test_test_vessel_never_in_train_anywhere():
    m = p_unseen("d")
    for v in MMSIS[:500]:
        splits = {m.split_of(v, t) for t in TIMES} - {None}
        assert len(splits) <= 1


def test_periods_disjoint_with_purge():
    for m in (p_known("d"), p_unseen("d")):
        ps = sorted(m.periods, key=lambda p: p.start)
        for a, b in zip(ps, ps[1:], strict=False):
            assert b.start - a.end >= timedelta(hours=m.purge_hours)
        for t in TIMES:
            assert sum(p.contains(t) for p in m.periods) <= 1


def test_window_straddling_boundary_is_purged():
    m = p_known("d")
    end_train = next(p.end for p in m.periods if p.split == "train")
    assert (
        m.window_split("x", end_train - timedelta(hours=3), end_train + timedelta(hours=1)) is None
    )
    assert (
        m.window_split("x", end_train - timedelta(hours=7), end_train - timedelta(hours=1))
        == "train"
    )


def test_overlapping_periods_rejected():
    with pytest.raises(ValidationError):
        SplitManifest(
            protocol="P-known",
            dataset_id="d",
            description="",
            purge_hours=24,
            periods=[
                Period(
                    split="train",
                    start=datetime(2023, 6, 1, tzinfo=UTC),
                    end=datetime(2023, 6, 10, tzinfo=UTC),
                ),
                Period(
                    split="test",
                    start=datetime(2023, 6, 10, 12, tzinfo=UTC),
                    end=datetime(2023, 6, 20, tzinfo=UTC),
                ),
            ],
        )


def test_buckets_must_tile():
    with pytest.raises(ValidationError):
        VesselAssignment(buckets={"train": (0, 700), "test": (750, 1000)})
    with pytest.raises(ValidationError):
        VesselAssignment(buckets={"train": (0, 800), "test": (700, 1000)})


def test_p_known_cannot_have_vessel_assignment():
    with pytest.raises(ValidationError):
        SplitManifest.model_validate(
            p_known("d").model_dump() | {"vessel_assignment": P_PAPER_ASSIGNMENT.model_dump()}
        )


def test_materialized_overlap_rejected():
    with pytest.raises(ValidationError):
        SplitManifest.model_validate(
            p_unseen("d").model_dump() | {"vessels": {"train": ["1"], "test": ["1"]}}
        )


def test_p_paper_matches_milestone0_calibration_rule():
    # scripts/calibrate.py originally used: sha256(mmsi) % 1000 < 200  -> test
    for v in MMSIS:
        old = int(hashlib.sha256(v.encode()).hexdigest(), 16) % 1000 < 200
        assert (p_paper("d").split_of(v) == "test") == old


def test_roundtrip_json():
    for m in (p_known("d"), p_unseen("d"), p_paper("d")):
        assert SplitManifest.model_validate_json(m.model_dump_json()) == m


# -- P-region --------------------------------------------------------------------------
from maritime_forecast.evaluation.splits import p_region  # noqa: E402
from maritime_forecast.schemas.manifest import RegionSplit  # noqa: E402

TEST_DAY = datetime(2023, 6, 25, tzinfo=UTC)
TRAIN_DAY = datetime(2023, 6, 5, tzinfo=UTC)
CAL_DAY = datetime(2023, 6, 19, tzinfo=UTC)
HOUSTON = (29.3, -94.8)  # inside the test box
BUFFER = (29.3, -93.0)  # ~29 km east of the box edge at -93.3
FAR = (27.0, -88.0)  # train side


def test_region_sides():
    rs = p_region("d").region_split
    assert rs.side(*HOUSTON) == "test" and rs.side(*BUFFER) is None and rs.side(*FAR) == "train"
    lat_min, lat_max, lon_min, lon_max = rs.outer
    from maritime_forecast.schemas.manifest import KM_PER_DEG_LAT

    assert KM_PER_DEG_LAT == 110.574  # shortest meridian degree: never under-pads
    assert lat_max - 30.5 == pytest.approx(50 / KM_PER_DEG_LAT)
    assert lon_max - (-93.3) > 50 / 111.32  # longitude pad wider than latitude pad


def test_region_sql_matches_python():
    import duckdb
    import numpy as np

    rs = p_region("d").region_split
    rng = np.random.default_rng(1)
    pts = [
        (float(a), float(b))
        for a, b in zip(rng.uniform(26, 32, 3000), rng.uniform(-98, -90, 3000), strict=True)
    ]
    con = duckdb.connect()
    con.execute("CREATE TABLE p(latitude_deg DOUBLE, longitude_deg DOUBLE)")
    con.executemany("INSERT INTO p VALUES (?, ?)", pts)
    sql = con.execute(f"SELECT {rs.sql_case()} FROM p").fetchall()
    assert [r[0] for r in sql] == [rs.side(a, b) for a, b in pts]


def test_p_region_pairs_sides_with_periods():
    m = p_region("d")
    assert m.split_of("x", TEST_DAY, *HOUSTON) == "test"
    assert m.split_of("x", TRAIN_DAY, *FAR) == "train"
    assert m.split_of("x", CAL_DAY, *FAR) == "calibration"
    assert m.split_of("x", TRAIN_DAY, *HOUSTON) is None  # test waters never train
    assert m.split_of("x", TEST_DAY, *FAR) is None  # train waters never test
    assert m.split_of("x", TEST_DAY, *BUFFER) is None
    with pytest.raises(ValueError):
        m.split_of("x", TEST_DAY)


def test_p_region_drops_straddling_windows():
    m = p_region("d")
    s, e = TEST_DAY, TEST_DAY + timedelta(hours=6)
    assert m.window_split("x", s, e, [HOUSTON, (29.4, -94.7)]) == "test"
    assert m.window_split("x", s, e, [HOUSTON, BUFFER]) is None
    assert m.window_split("x", TRAIN_DAY, TRAIN_DAY + timedelta(hours=6), [FAR, HOUSTON]) is None
    with pytest.raises(ValueError):
        m.window_split("x", s, e)


def test_region_split_only_for_p_region():
    rs = p_region("d").region_split
    with pytest.raises(ValidationError):
        SplitManifest.model_validate(p_known("d").model_dump() | {"region_split": rs.model_dump()})
    with pytest.raises(ValidationError):
        SplitManifest.model_validate(p_region("d").model_dump() | {"region_split": None})
    with pytest.raises(ValidationError):
        RegionSplit(
            test_region={"name": "x", "lat_min": 30, "lat_max": 29, "lon_min": 0, "lon_max": 1},
            buffer_km=1,
        )


def test_p_region_roundtrip():
    m = p_region("d")
    assert SplitManifest.model_validate_json(m.model_dump_json()) == m


def test_split_for_side_matches_split_of():
    m = p_region("d")
    for t in (TRAIN_DAY, CAL_DAY, TEST_DAY):
        for pos in (HOUSTON, BUFFER, FAR):
            assert m.split_for_side(t, m.region_split.side(*pos)) == m.split_of("x", t, *pos)
    with pytest.raises(ValueError):
        p_known("d").split_for_side(TRAIN_DAY, "train")


def test_p_region_test_membership_ignores_where_labels_go():
    # Regression (leakage review): a test cutoff in the box whose vessel later leaves it must
    # stay in the test set; dropping it would select test samples on their future.
    m = p_region("d")
    s, e = TEST_DAY, TEST_DAY + timedelta(hours=6)
    assert m.window_split("x", s, e, [HOUSTON], label_positions=[BUFFER, FAR]) == "test"


def test_p_region_train_labels_must_stay_on_train_side():
    m = p_region("d")
    s, e = TRAIN_DAY, TRAIN_DAY + timedelta(hours=6)
    assert m.window_split("x", s, e, [FAR], label_positions=[FAR]) == "train"
    assert m.window_split("x", s, e, [FAR], label_positions=[FAR, BUFFER]) is None
    assert m.window_split("x", s, e, [FAR], label_positions=[HOUSTON]) is None


def test_region_sql_null_coordinates_are_excluded():
    import duckdb

    rs = p_region("d").region_split
    got = duckdb.sql(f"SELECT {rs.sql_case('NULL::DOUBLE', '-88.0')}").fetchone()[0]
    assert got is None
