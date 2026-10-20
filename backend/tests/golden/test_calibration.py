"""§6.6 golden-number regression. Needs local data: run via `make calibrate`, never in CI."""

import json

import pytest

from maritime_forecast.config import RUNS_DIR

pytestmark = pytest.mark.golden


def _latest_summary() -> dict:
    runs = sorted(RUNS_DIR.glob("calibration_*/summary.json"))
    if not runs:
        pytest.skip("no calibration run found; run scripts/calibrate.py first")
    return json.loads(runs[-1].read_text())


def test_primary_cv_within_tolerance_of_paper():
    s = _latest_summary()
    r = s["primary_result"]
    for h, golden in s["golden_cv_km"].items():
        est = r[f"{h}h"][0]
        assert abs(est / golden - 1) <= s["tolerance"], f"{h} h: {est:.2f} km vs {golden} km"


def test_primary_variant_is_the_predeclared_one():
    s = _latest_summary()
    assert s["primary_variant"] == {
        "jitter": "drop_stationary",
        "cv_mode": "anchor_report",
        "vessels": "test_split",
        "stat": "mean",
    }
