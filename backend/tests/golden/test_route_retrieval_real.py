"""route_retrieval_v1 protocol eligibility on REAL NOAA test cutoffs (needs local data and the
prebuilt segment index; runs via `make calibrate`).

For each sampled test-split cutoff of the kept benchmark runs, every reference a forecast
used must be (a) known complete by the end of the train period (frozen pool), so no
post-cutoff or test-period voyage was eligible, and (b) under P-unseen, from a train-assigned
vessel other than the target.
"""

from pathlib import Path

import polars as pl
import pytest

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR, RUNS_DIR
from maritime_forecast.data.segment_index import load_segment_index
from maritime_forecast.evaluation.runner import BenchmarkConfig, benchmark_context
from maritime_forecast.models.base import ForecastHorizon, histories_for_cutoffs
from maritime_forecast.models.registry import get_model
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore

pytestmark = pytest.mark.golden
DATASET = "noaa_us_gulf_2023-06"


@pytest.mark.parametrize("protocol", ["P-known", "P-unseen"])
def test_every_reference_is_protocol_eligible(protocol):
    runs = sorted((RUNS_DIR / "benchmarks").glob(f"{protocol}_{DATASET}_*/cutoffs.parquet"))
    if not runs or not (PROCESSED_DIR / "canonical" / DATASET).exists():
        pytest.skip("needs local NOAA data and a kept benchmark run")
    try:
        index = load_segment_index(DATASET)
    except (FileNotFoundError, ValueError):
        pytest.skip("needs the segment index (make index)")
    mpath = MANIFEST_DIR / "splits" / f"{protocol}__{DATASET}.json"
    manifest = SplitManifest.model_validate_json(mpath.read_text())
    train_end = max(p.end for p in manifest.periods if p.split == "train")
    test_start = min(p.start for p in manifest.periods if p.split == "test")
    store = ParquetStore()
    _, ctx = benchmark_context(BenchmarkConfig(dataset_id=DATASET, manifest_path=mpath), store)
    model = get_model("route_retrieval_v1", ctx)
    cut = (pl.read_parquet(runs[-1]).sample(150, seed=11).drop("cutoff_id")
           .with_row_index("cutoff_id"))  # fmt: skip
    assert (cut["observed_at"] >= test_start).all()
    known = dict(zip(index["segment_id"], index["known_at"], strict=True))
    vessel = dict(zip(index["segment_id"], index["vessel_id"], strict=True))
    n_ok = n_refs = 0
    for _, h in histories_for_cutoffs(store, DATASET, cut, 24):
        f = model.predict(h, ForecastHorizon(h.as_of, 6, 10))
        if f.status != "ok":
            continue
        n_ok += 1
        for t in f.trajectories:
            (sid,) = t.reference_segment_ids
            n_refs += 1
            assert known[sid] <= train_end, (sid, known[sid])
            if manifest.vessel_assignment is not None:
                assert vessel[sid] != h.vessel_id
                assert manifest.vessel_assignment.assign(vessel[sid]) == "train"
    assert n_ok >= 140 and n_refs >= 1000, (n_ok, n_refs)  # not vacuous


def test_prebuilt_index_matches_the_models_own_pool():
    """The model builds its pool from data before the train end; the prebuilt full-archive
    index, filtered by eligible(), must give the same segments (slice-1 equivalence)."""
    try:
        index = load_segment_index(DATASET)
    except (FileNotFoundError, ValueError):
        pytest.skip("needs the segment index (make index)")
    import maritime_forecast.models.route_retrieval as rr
    from maritime_forecast.data.segment_index import eligible

    mpath = MANIFEST_DIR / "splits" / f"P-known__{DATASET}.json"
    manifest = SplitManifest.model_validate_json(Path(mpath).read_text())
    train_end = max(p.end for p in manifest.periods if p.split == "train")
    pool = rr.load_pool(ParquetStore(), DATASET, manifest, train_end, rr.RetrievalConfig())
    want = eligible(index, train_end, "", manifest, "frozen")["segment_id"].sort().to_list()
    assert sorted(pool.segment_id.tolist()) == want
