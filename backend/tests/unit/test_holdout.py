"""Held-out test period guards (evaluation.holdout) and the dev split under P-region."""

from datetime import UTC, datetime, timedelta

import pytest

import maritime_forecast.evaluation.holdout as ho
from maritime_forecast.evaluation.splits import JUNE_JULY_DATASET, p_known, p_region

V2 = p_known(JUNE_JULY_DATASET)
V1 = p_known("noaa_us_gulf_2023-06")
MODELS = [{"model_id": "m", "model_version": "1", "config": {"k": 10}}]
CLEAN = "abc123"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ho, "MANIFEST_DIR", tmp_path)
    return tmp_path


def test_only_holdout_test_split_is_gated():
    ho.check_test_access(V1, "test", False, "x-dirty", MODELS, None)  # June-only: no gate
    ho.check_test_access(V2, "dev", False, "x-dirty", MODELS, None)  # dev: no gate
    with pytest.raises(ValueError, match="confirm"):
        ho.check_test_access(V2, "test", False, CLEAN, MODELS, None)
    with pytest.raises(ValueError, match="clean"):
        ho.check_test_access(V2, "test", True, CLEAN + "-dirty", MODELS, None)
    ho.check_test_access(V2, "test", True, CLEAN, MODELS, None)


def test_ledger_records_and_blocks_a_second_look():
    ho.record_test_access(V2, "run1", CLEAN, MODELS, ["review-a"], None)
    (e,) = ho.read_ledger(JUNE_JULY_DATASET)
    assert e["run_id"] == "run1" and e["informed_by"] == ["review-a"]
    assert e["models"][0]["config_sha256"] == ho.config_sha({"k": 10})
    with pytest.raises(ValueError, match="already scored"):
        ho.check_test_access(V2, "test", True, CLEAN, MODELS, None)
    ho.check_test_access(V2, "test", True, CLEAN, MODELS, "fixed a scoring bug")
    changed = [{**MODELS[0], "config": {"k": 5}}]  # a different config is a new design
    ho.check_test_access(V2, "test", True, CLEAN, changed, None)


def test_touches_test_for_api_evaluation():
    july = datetime(2023, 7, 10, tzinfo=UTC)
    assert ho.touches_test(V2, july, july + timedelta(hours=1))
    late_june = datetime(2023, 6, 30, 12, tzinfo=UTC)
    assert ho.touches_test(V2, late_june, late_june + timedelta(days=2))  # reaches July 2
    assert not ho.touches_test(V2, late_june, late_june + timedelta(hours=6))
    assert not ho.touches_test(V1, july, july + timedelta(hours=1))


def test_p_region_dev_window_stays_on_the_train_side():
    m = p_region(JUNE_JULY_DATASET)
    dev = datetime(2023, 6, 19, 12, tzinfo=UTC)
    box = m.region_split.test_region
    inside = ((box.lat_min + box.lat_max) / 2, (box.lon_min + box.lon_max) / 2)
    far = (25.0, -84.0)  # beyond the buffer, still in the Gulf
    assert m.split_of("x", dev, *far) == "dev"
    assert m.split_of("x", dev, *inside) is None
    end = dev + timedelta(hours=6)
    assert m.window_split("x", dev - timedelta(hours=6), end, [far], [far]) == "dev"
    assert m.window_split("x", dev - timedelta(hours=6), end, [far], [inside]) is None


def test_analysis_entries_are_recorded_and_do_not_block_runs():
    ho.record_test_access(V2, "run1", CLEAN, MODELS, [], None)
    ho.record_analysis(JUNE_JULY_DATASET, "run1", "conformal_coverage", {"min_n": 30}, CLEAN, "out")
    entries = ho.read_ledger(JUNE_JULY_DATASET)
    assert [e.get("kind") for e in entries] == [None, "conformal_coverage"]
    assert entries[1]["models"] == [] and entries[1]["config_sha256"] == ho.config_sha(
        {"min_n": 30}
    )
    changed = [{**MODELS[0], "config": {"k": 5}}]
    ho.check_test_access(V2, "test", True, CLEAN, changed, None)  # analysis adds no model
