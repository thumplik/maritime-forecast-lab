"""gap_segments / segments_and_gaps on SYNTHETIC times."""

from datetime import UTC, datetime, timedelta

import pytest

from maritime_forecast.data.segment import gap_segments, segments_and_gaps

T = [datetime(2023, 6, 1, tzinfo=UTC) + timedelta(minutes=m) for m in (0, 10, 40, 71, 72)]


def test_gap_boundary_is_strict():
    # 30 min exactly does not split; 31 min does
    assert gap_segments(T, 30) == [0, 0, 0, 1, 1]


def test_summaries():
    seg = gap_segments(T, 30)
    segs, gaps = segments_and_gaps(T, seg)
    assert [s["n_points"] for s in segs] == [3, 2]
    assert gaps == [{"start": T[2], "end": T[3], "duration_s": 1860.0}]


def test_unsorted_rejected():
    with pytest.raises(ValueError):
        gap_segments([T[1], T[0]], 30)


def test_empty():
    assert gap_segments([], 30) == [] and segments_and_gaps([], []) == ([], [])
