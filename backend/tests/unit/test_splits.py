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
