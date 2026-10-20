"""Forecast / evaluation / benchmark-run contracts encode the evaluation rules."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from maritime_forecast.schemas.benchmark import BenchmarkRun, CellResult, Stat
from maritime_forecast.schemas.forecast import (
    ForecastEvaluation,
    ForecastRequest,
    ForecastResponse,
    HorizonScore,
    ModelForecast,
)

AS_OF = datetime(2023, 6, 10, 12, tzinfo=UTC)
TIMES = [AS_OF + timedelta(minutes=10 * k) for k in range(1, 4)]


def req(**kw):
    return ForecastRequest(**({"dataset_id": "d", "vessel_id": "367000001", "as_of": AS_OF,
                               "model_ids": ["constant_velocity_v1"]} | kw))  # fmt: skip


def test_request_defaults_and_grid():
    r = req()
    assert (r.history_hours, r.horizon_hours, r.step_minutes, r.show_ground_truth) == (
        24,
        6,
        10,
        False,
    )
    with pytest.raises(ValidationError):
        req(horizon_hours=1.05)  # not a whole number of steps
    with pytest.raises(ValidationError):
        req(horizon_hours=72, step_minutes=1)  # 4320 steps > 1024
    with pytest.raises(ValidationError):
        req(as_of=datetime(2023, 6, 10, 12))  # naive
    with pytest.raises(ValidationError):
        req(vessel_id="../x")
    assert (
        req(horizon_hours=0.1, step_minutes=1).horizon_hours == 0.1
    )  # 6 steps despite float noise


def ok(**kw):
    base = {
        "model_id": "cv",
        "model_version": "1",
        "status": "ok",
        "target_times": TIMES,
        "points": [{"timestamp": t, "latitude_deg": 29, "longitude_deg": -94} for t in TIMES],
    }
    return ModelForecast(**(base | kw))  # fmt: skip


def test_model_forecast_rules():
    assert ok().status == "ok"
    with pytest.raises(ValidationError):  # points must match the requested grid exactly
        ok(points=ok().points[:2])
    with pytest.raises(ValidationError):  # failures carry an error and never fake points
        ok(status="unsupported")
    f = ModelForecast(model_id="t", model_version="1", status="unsupported", target_times=TIMES,
                      points=[], error={"code": "history_too_short", "message": "needs 48 h"})  # fmt: skip
    assert f.points == []
    with pytest.raises(ValidationError):  # one radius per target time
        ok(uncertainty={"nominal_level": 0.9, "calibration_set_id": "c", "radii_m": [1.0]})
    with pytest.raises(ValidationError):  # no per-axis marginal "corridors"
        ok(uncertainty={"kind": "marginal_quantiles", "nominal_level": 0.9,
                        "calibration_set_id": "c", "radii_m": [1, 2, 3]})  # fmt: skip


def test_forecast_response_never_carries_truth():
    assert not {"truth", "error_m", "errors"} & set(ForecastResponse.model_fields)
    with pytest.raises(ValidationError):
        ForecastResponse.model_validate({"truth": []})


def cell(model, n=100, **kw):
    s = Stat(estimate=1, ci_low=0.5, ci_high=1.5)
    base = dict(model_id=model, horizon_minutes=60, stratum="underway:ALL",
                admission="first_class", n_evaluable=n, n_paired=n, n_vessels=10, sample_set_sha256="a" * 64,
                n_failed=0, failure_rate=0.0, headline=True,
                median_error_m=s, mean_error_m=s, p90_error_m=2)  # fmt: skip
    return CellResult(**(base | kw))


def run(**kw):
    base = {"run_id": "r", "created_at": AS_OF, "status": "succeeded", "git_sha": "x",
            "dataset_id": "d", "data_provenance": "noaa_marinecadastre@v1",
            "protocol": {"protocol": "P-unseen", "manifest_version": "splits-v1",
                         "dataset_id": "d", "manifest_sha256": "0" * 64},
            "sampler": {"seed": 1},
            "models": [{"model_id": "cv", "model_version": "1"},
                       {"model_id": "persistence", "model_version": "1"}],
            "horizons_minutes": [60]}  # fmt: skip
    return BenchmarkRun(**(base | kw))


def test_benchmark_run_enforces_pairing_and_operational_only():
    assert run(results=[cell("cv"), cell("persistence")]).status == "succeeded"
    for other in ({"n_paired": 98}, {"n_vessels": 7}, {"sample_set_sha256": "b" * 64},
                  {"admission": "experimental", "headline": False}):  # fmt: skip
        with pytest.raises(ValidationError, match="different sample sets"):
            run(results=[cell("cv"), cell("persistence", **other)])
    with pytest.raises(ValidationError, match="undeclared"):
        run(results=[cell("cv"), cell("persistence"), cell("oracle")])
    with pytest.raises(ValidationError, match="every model in every cell"):  # silent drop
        run(results=[cell("cv")])
    with pytest.raises(ValidationError, match="every declared horizon"):  # never scored
        run(horizons_minutes=[60, 120], results=[cell("cv"), cell("persistence")])
    with pytest.raises(ValidationError):
        run(operational=False)  # best-of-N runs cannot be recorded as runs
    with pytest.raises(ValidationError):
        run(status="failed")  # without an error


def test_cell_failures_are_reported_and_not_headline():
    c = cell("cv", n_failed=5, failure_rate=5 / 105, headline=False)
    assert c.failure_rate == pytest.approx(0.0476, abs=1e-4)
    with pytest.raises(ValidationError):  # 4.8% failures: not headline
        cell("cv", n_failed=5, failure_rate=5 / 105, headline=True)
    assert cell("cv", n=999, n_failed=1, failure_rate=1 / 1000, headline=True).headline  # 0.1%
    with pytest.raises(ValidationError):
        cell("cv", n_failed=5, failure_rate=0.0, headline=False)
    with pytest.raises(ValidationError):
        cell("cv", admission="experimental", headline=True)


def test_model_forecast_holes_closed():
    with pytest.raises(ValidationError):  # "ok" with no prediction
        ok(target_times=[], points=[])
    with pytest.raises(ValidationError):  # failed but still ships a trajectory
        ModelForecast(model_id="r", model_version="1", status="failed", target_times=TIMES,
                      points=[], error={"code": "x", "message": "y"},
                      trajectories=[{"trajectory_id": "t", "points": []}])  # fmt: skip
    with pytest.raises(ValidationError):  # negative radius
        ok(uncertainty={"nominal_level": 0.9, "calibration_set_id": "c", "radii_m": [1, -1, 2]})
    with pytest.raises(ValidationError):  # unordered grid
        ok(target_times=list(reversed(TIMES)), points=list(reversed(ok().points)))
    with pytest.raises(ValidationError):  # trajectory off the grid
        ok(trajectories=[{"trajectory_id": "t", "points": ok().points[:2]}])


def test_response_is_causal():
    cov = {"n_reports": 3, "first_report": AS_OF - timedelta(hours=1),
           "last_report": AS_OF, "last_report_age_s": 0, "n_gaps": 0}  # fmt: skip
    base = {"forecast_id": "f", "dataset_id": "d", "vessel_id": "v", "as_of": AS_OF,
            "source": "s", "data_version": "x", "input_coverage": cov, "forecasts": [ok()]}  # fmt: skip
    assert ForecastResponse(**base).forecast_id == "f"
    with pytest.raises(ValidationError):
        ForecastResponse(
            **(base | {"input_coverage": cov | {"last_report": AS_OF + timedelta(seconds=1)}})
        )
    early = [AS_OF] + TIMES[1:]
    with pytest.raises(ValidationError):
        ForecastResponse(
            **(
                base
                | {
                    "forecasts": [
                        ok(
                            target_times=early,
                            points=[
                                {"timestamp": t, "latitude_deg": 29, "longitude_deg": -94}
                                for t in early
                            ],
                        )
                    ]
                }
            )
        )


def test_scores_and_evaluations_are_complete():
    HorizonScore(horizon_minutes=10, evaluable=True, error_m=5.0)
    HorizonScore(horizon_minutes=10, evaluable=False, reason="truth_gap")
    for bad in ({"evaluable": False, "error_m": 12.0}, {"evaluable": True, "reason": "truth_gap"},
                {"evaluable": False, "reason": "truth_gap", "error_m": 3.0}):  # fmt: skip
        with pytest.raises(ValidationError):
            HorizonScore(horizon_minutes=10, **bad)
    pt = {"timestamp": TIMES[0], "latitude_deg": 29, "longitude_deg": -94}
    good = {"forecast_id": "f", "truth_region": "us_gulf", "max_bracket_gap_minutes": 30,
            "truth": [pt, None],
            "models": [{"model_id": "cv", "scores": [
                {"horizon_minutes": 10, "evaluable": True, "error_m": 1.0},
                {"horizon_minutes": 20, "evaluable": False, "reason": "truth_gap"}]}]}  # fmt: skip
    ForecastEvaluation(**good)
    with pytest.raises(ValidationError):  # silent per-horizon drop
        ForecastEvaluation(
            **(good | {"models": [{"model_id": "cv", "scores": good["models"][0]["scores"][:1]}]})
        )
    with pytest.raises(ValidationError):
        ForecastEvaluation(**(good | {"truth": [], "models": []}))
