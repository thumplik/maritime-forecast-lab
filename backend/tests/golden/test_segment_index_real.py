"""Segment-index causality and eligibility on REAL NOAA data (needs local data; runs via
`make calibrate`).

1. Eligible segments from the full-month index equal those from an index built only from
   reports at or before each cutoff (rolling pool, the strictest check). The cutoffs are early
   in the month, so the truncated builds stay small.
2. At a test-period cutoff, the frozen pool only uses segments known by the end of the train
   period. Under P-unseen it only uses train-assigned vessels and never the target, for both a
   test-assigned and a train-assigned real target.
"""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.data.segment_index import (
    build_segment_index,
    eligible,
    load_segment_index,
    pool_until,
)
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore

pytestmark = pytest.mark.golden
DATASET = "noaa_us_gulf_2023-06"


def _setup(protocol):
    try:
        full = load_segment_index(DATASET)
    except FileNotFoundError:
        pytest.skip("needs the segment index (make index)")
    m = SplitManifest.model_validate_json(
        (MANIFEST_DIR / "splits" / f"{protocol}__{DATASET}.json").read_text()
    )
    return full, m


@pytest.mark.parametrize("protocol", ["P-known", "P-unseen"])
def test_real_index_is_causal(protocol):
    full, m = _setup(protocol)
    store = ParquetStore()
    target = full["vessel_id"][0]
    for as_of in (datetime(2023, 6, 2, 13, 7, tzinfo=UTC), datetime(2023, 6, 3, 6, 41, tzinfo=UTC)):
        trunc = build_segment_index(store, DATASET, end=as_of + timedelta(microseconds=1))
        for pool in ("rolling", "frozen"):
            a = eligible(full, as_of, target, m, pool).sort("segment_id")
            b = eligible(trunc, as_of, target, m, pool).sort("segment_id")
            assert a.height > 1000, a.height
            assert a.equals(b), (as_of, pool, set(a["segment_id"]) ^ set(b["segment_id"]))


@pytest.mark.parametrize("protocol", ["P-known", "P-unseen"])
def test_real_test_period_cutoff_uses_the_frozen_train_pool(protocol):
    full, m = _setup(protocol)
    as_of = datetime(2023, 6, 25, 9, 30, tzinfo=UTC)
    train_end = max(p.end for p in m.periods if p.split == "train")
    assert pool_until(as_of, m) == train_end < as_of
    vessels = full["vessel_id"].unique().sort().to_list()
    va = m.vessel_assignment
    targets = vessels[:1] if va is None else [
        next(v for v in vessels if va.assign(v) == "test"),
        next(v for v in vessels if va.assign(v) == "train"),
    ]  # fmt: skip
    for target in targets:
        e = eligible(full, as_of, target, m)
        assert e.height > 1000 and (e["known_at"] <= train_end).all()
        if va is None:
            # P-known: the target's own segments before the train end stay eligible
            own = full.filter((pl.col("vessel_id") == target) & (pl.col("known_at") <= train_end))
            assert set(own["segment_id"]) <= set(e["segment_id"])
        else:
            got = set(e["vessel_id"])
            assert target not in got and all(va.assign(v) == "train" for v in got)
    rolling = eligible(full, as_of, targets[0], m, "rolling")
    assert rolling.height > e.height and (rolling["known_at"] <= as_of).all()
