"""Forecaster contract and B0 adapters on SYNTHETIC histories."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from maritime_forecast.geo.geodesic import distance_m
from maritime_forecast.models.base import (
    AISHistory,
    ForecastHorizon,
    LeakageError,
    ModelContext,
    TrajectoryForecaster,
)
from maritime_forecast.models.registry import UnknownModel, capabilities, get_model, model_ids
from maritime_forecast.schemas.forecast import ModelForecast

AS_OF = datetime(2023, 6, 1, 12, tzinfo=UTC)


def hist(rows, as_of=AS_OF, vessel="999000001"):
    df = pl.DataFrame(
        [
            {"vessel_id": vessel, "observed_at": AS_OF + timedelta(minutes=m), "latitude_deg": la,
             "longitude_deg": lo, "sog_knots": s, "cog_deg": c}
            for m, la, lo, s, c in rows
        ],
        schema={"vessel_id": pl.Utf8, "observed_at": pl.Datetime("us", "UTC"), "latitude_deg": pl.Float64,
                "longitude_deg": pl.Float64, "sog_knots": pl.Float64, "cog_deg": pl.Float64},
    )  # fmt: skip
    return AISHistory(vessel, as_of, 24, df)


H = ForecastHorizon(AS_OF, 1, 10)


def test_history_guards():
    with pytest.raises(LeakageError):
        hist([(-10, 29, -94, 10, 90), (5, 29, -94, 10, 90)])  # a report after as_of
    with pytest.raises(ValueError):
        hist([(0, 29, -94, 10, 90)], as_of=datetime(2023, 6, 1, 12))  # naive
    with pytest.raises(ValueError):
        hist([(-5, 29, -94, 10, 90), (-10, 29, -94, 10, 90)])  # unsorted
    with pytest.raises(ValueError):
        AISHistory("other", AS_OF, 24, hist([(0, 29, -94, 10, 90)]).reports)


def test_horizon_grid():
    assert len(H.target_times) == 6 and H.target_times[0] == AS_OF + timedelta(minutes=10)
    with pytest.raises(ValueError):
        ForecastHorizon(AS_OF, 0.25, 10)
    with pytest.raises(ValueError):
        ForecastHorizon(AS_OF, 72, 1)  # 4320 steps


def test_registry():
    assert model_ids() == [
        "constant_turn_rate_v1",
        "constant_velocity_paper_v1",
        "constant_velocity_v1",
        "damped_velocity_v1",
        "persistence_v1",
        "route_retrieval_v1",
        "route_retrieval_v2",
        "timestral_pos_v1",
    ]
    assert get_model("persistence_v1").model_id == "persistence_v1"
    from maritime_forecast.models.registry import product_model_ids

    assert "constant_velocity_paper_v1" not in product_model_ids()  # calibration only
    for m in model_ids():
        assert isinstance(get_model(m), TrajectoryForecaster)
    assert {c.model_id for c in capabilities()} == set(model_ids())
    reads = {c.model_id for c in capabilities() if c.reads_beyond_own_history}
    assert reads == {
        "damped_velocity_v1",
        "route_retrieval_v1",
        "route_retrieval_v2",
        "timestral_pos_v1",
    }  # fitted params; index; pretrained weights (external corpus)
    with pytest.raises(UnknownModel):
        get_model("oracle")


DAMPED_CTX = ModelContext(params={"taus_hours": {"default": 2.0}})


@pytest.mark.parametrize(
    "model_id",
    [
        "persistence_v1",
        "constant_velocity_v1",
        "constant_velocity_paper_v1",
        "constant_turn_rate_v1",
        "damped_velocity_v1",
    ],
)
def test_every_model_returns_the_requested_grid_or_a_typed_error(model_id):
    m = get_model(model_id, DAMPED_CTX)
    f = m.predict(hist([(-10, 29, -94, 10, 90), (0, 29.01, -94, 10, 90)]), H)
    assert isinstance(f, ModelForecast) and f.status == "ok"
    assert [p.timestamp for p in f.points] == H.target_times
    empty = m.predict(hist([]), H)
    assert (
        empty.status == "unsupported"
        and empty.error.code == "insufficient_history"
        and not empty.points
    )


def test_persistence():
    f = get_model("persistence_v1").predict(hist([(-90, 28, -95, 5, 0), (-45, 29, -94, 10, 90)]), H)
    assert {(p.latitude_deg, p.longitude_deg) for p in f.points} == {(29, -94)}
    assert any("45 min older" in w for w in f.warnings)


def test_cv_standard_respects_anchor_time_and_falls_back():
    m = get_model("constant_velocity_v1")
    # last report at as_of - 30 min, 10 kn east: at as_of + 10 min the vessel has moved 40 min
    f = m.predict(hist([(-30, 29, -94, 10, 90)]), H)
    d = distance_m(29, -94, f.points[0].latitude_deg, f.points[0].longitude_deg)
    assert d == pytest.approx(10 * 1852 * 40 / 60, rel=1e-6)
    # latest report lacks COG -> anchor on the earlier one, with a warning
    f = m.predict(hist([(-10, 29, -94, 10, 90), (0, 29.01, -94, 10, None)]), H)
    assert f.status == "ok" and any("earlier report" in w for w in f.warnings)
    # zero speed needs no course
    f = m.predict(hist([(0, 29, -94, 0, None)]), H)
    assert all(
        p.latitude_deg == pytest.approx(29) and p.longitude_deg == pytest.approx(-94)
        for p in f.points
    )
    f = m.predict(hist([(0, 29, -94, None, None)]), H)
    assert f.status == "failed" and f.error.code == "no_usable_sog_cog" and not f.points


def test_cv_paper_uses_last_report_from_as_of_without_fallback():
    m = get_model("constant_velocity_paper_v1")
    f = m.predict(hist([(-30, 29, -94, 10, 90)]), H)
    d = distance_m(29, -94, f.points[0].latitude_deg, f.points[0].longitude_deg)
    assert d == pytest.approx(10 * 1852 * 10 / 60, rel=1e-6)  # lead from as_of, not anchor
    f = m.predict(hist([(-10, 29, -94, 10, 90), (0, 29.01, -94, 10, None)]), H)
    assert f.status == "failed" and f.error.code == "anchor_missing_sog_cog"


def test_horizon_and_step_limits():
    m = get_model("persistence_v1")
    f = m.predict(hist([(0, 29, -94, 1, 1)]), ForecastHorizon(AS_OF, 1, 10))
    assert f.status == "ok"
    with pytest.raises(ValueError):
        m.predict(hist([(0, 29, -94, 1, 1)]), ForecastHorizon(AS_OF + timedelta(hours=1), 1, 10))


def test_history_boundary_microsecond(tmp_path):
    """A report exactly at as_of is visible; one 1 µs later is not."""
    from maritime_forecast.storage.parquet_store import ParquetStore

    st = ParquetStore(tmp_path / "c")
    st.write(
        "b",
        f"""SELECT * FROM (VALUES
        ('999000001', TIMESTAMPTZ '{AS_OF.isoformat()}', 29.0, -94.0, 1.0, 1.0),
        ('999000001', TIMESTAMPTZ '{AS_OF.isoformat()}' + INTERVAL 1 MICROSECOND, 30.0, -94.0, 1.0, 1.0)
    ) t(vessel_id, observed_at, latitude_deg, longitude_deg, sog_knots, cog_deg)""",
    )
    h = st.history("b", "999000001", AS_OF, 1)
    assert h["observed_at"].to_list() == [AS_OF]


def test_exceptions_and_non_finite_outputs_become_typed_failures():
    from dataclasses import dataclass

    from maritime_forecast.models.base import BaseForecaster, ModelCapabilities

    @dataclass
    class Broken(BaseForecaster):
        model_id: str = "broken"
        mode: str = "raise"

        def capabilities(self):
            return ModelCapabilities(model_id=self.model_id, model_version="0", description="x",
                                     reads_beyond_own_history=False, uses_store_at_predict=False)  # fmt: skip

        def _predict(self, history, horizon):
            if self.mode == "raise":
                raise RuntimeError("boom")
            n = len(horizon.target_times)
            return [float("nan")] * n, [0.0] * n, []

    h = hist([(0, 29, -94, 1, 1)])
    f = Broken().predict(h, H)
    assert f.status == "failed" and f.error.code == "exception" and not f.points
    f = Broken(mode="nan").predict(h, H)
    assert f.status == "failed" and f.error.code == "non_finite_output"


def test_capability_flag_must_be_declared():
    from pydantic import ValidationError

    from maritime_forecast.models.base import ModelCapabilities

    with pytest.raises(ValidationError):
        ModelCapabilities(model_id="x", model_version="0", description="x")
    with pytest.raises(ValidationError):  # no default for the store flag either
        ModelCapabilities(
            model_id="x", model_version="0", description="x", reads_beyond_own_history=True
        )
    with pytest.raises(ValidationError):  # store use implies reading beyond own history
        ModelCapabilities(
            model_id="x",
            model_version="0",
            description="x",
            reads_beyond_own_history=False,
            uses_store_at_predict=True,
        )


def test_cv_standard_skips_implausible_sog_anchor():
    m = get_model("constant_velocity_v1")
    h = hist([(-10, 29, -94, 10, 90), (0, 29.01, -94, 80, 90)])
    df = h.reports.with_columns(quality_flags=pl.Series([[], ["sog_implausible"]]))
    f = m.predict(AISHistory(h.vessel_id, h.as_of, 24, df), H)
    assert f.status == "ok" and any("earlier report" in w for w in f.warnings)


# -- damped velocity --------------------------------------------------------------------------
def test_damped_distance_limits():
    from maritime_forecast.models.damped_velocity import damped_distance_m

    lead = np.array([600.0, 3600.0, 36000.0])
    cv = damped_distance_m(10, lead, None)
    assert np.allclose(cv, 10 * 1852 / 3600 * lead)
    d = damped_distance_m(10, lead, 1.0)
    assert np.all(d < cv) and d[-1] == pytest.approx(10 * 1852, rel=1e-3)  # saturates at SOG*tau
    assert damped_distance_m(10, lead, 1e-6).max() < 1  # tau -> 0: persistence


def test_damped_uses_class_tau_and_default():
    ctx = ModelContext(params={"taus_hours": {"cargo": 0.5, "default": None}})
    m = get_model("damped_velocity_v1", ctx)
    h = hist([(0, 29, -94, 10, 90)])
    far = ForecastHorizon(AS_OF, 6, 60)

    def final_distance(cls):
        hh = AISHistory(h.vessel_id, h.as_of, 24, h.reports.with_columns(vessel_class=pl.lit(cls)))
        p = m.predict(hh, far).points[-1]
        return distance_m(29, -94, p.latitude_deg, p.longitude_deg)

    assert final_distance("cargo") == pytest.approx(10 * 1852 * 0.5 * (1 - np.exp(-12)), rel=1e-3)
    assert final_distance("tug_tow") == pytest.approx(10 * 1852 * 6, rel=1e-6)  # default: CV


def test_damped_refuses_unfitted_and_mismatched_fits(tmp_path, monkeypatch):
    import maritime_forecast.models.damped_velocity as dv

    f = get_model("damped_velocity_v1").predict(hist([(0, 29, -94, 10, 90)]), H)
    assert f.status == "unsupported" and f.error.code == "not_fitted"
    monkeypatch.setattr(dv, "FIT_DIR", tmp_path)
    fit = dv.DampedVelocityFit(
        protocol="P-known",
        dataset_id="d",
        manifest_sha256="a" * 64,
        split="train",
        data_until=AS_OF - timedelta(days=1),
        taus_hours={"default": 2.0},
        n_cutoffs={},
        objective={},
        config={},
        data_provenance="x",
        git_sha="x",
        created_at=AS_OF,
    )
    dv.fit_path("P-known", "d").write_text(fit.model_dump_json())
    ok_ctx = ModelContext(dataset_id="d", protocol="P-known", manifest_sha256="a" * 64)
    assert get_model("damped_velocity_v1", ok_ctx).capabilities().installed
    bad = ModelContext(dataset_id="d", protocol="P-known", manifest_sha256="b" * 64)
    m = get_model("damped_velocity_v1", bad)
    assert not m.capabilities().installed
    assert m.predict(hist([(0, 29, -94, 10, 90)]), H).error.code == "not_fitted"


# -- constant turn rate -----------------------------------------------------------------------
def test_turn_rate_estimate_wraps():
    from maritime_forecast.models.constant_turn import turn_rate_deg_s

    rate = turn_rate_deg_s(np.array([0, 60, 120.0]), np.array([358.0, 0.0, 2.0]))
    assert rate == pytest.approx(2 / 60)
    assert turn_rate_deg_s(np.array([0.0]), np.array([10.0])) is None


def test_turn_path_straight_and_circle():
    from maritime_forecast.models.constant_turn import turn_path

    d, az = turn_path(10, 45.0, 0.0, [3600.0], decay_s=1e9, step_s=30)
    assert d[0] == pytest.approx(10 * 1852, rel=1e-9) and az[0] == pytest.approx(45.0)
    d, _ = turn_path(10, 0.0, 0.1, [3600.0], decay_s=1e12, step_s=1)  # 360 deg in 1 h
    assert d[0] < 50


def test_ctr_turns_with_history_and_caps():
    m = get_model("constant_turn_rate_v1")
    rows = [(-10 + k, 29 + 0.001 * k, -94, 10, (90 + 2 * k) % 360) for k in range(11)]
    hz = ForecastHorizon(AS_OF, 1, 10)
    f = m.predict(hist(rows), hz)
    cv = get_model("constant_velocity_v1").predict(hist(rows), hz)
    assert f.status == "ok"
    a, b = f.points[-1], cv.points[-1]
    assert distance_m(a.latitude_deg, a.longitude_deg, b.latitude_deg, b.longitude_deg) > 500
    wild = [(-2, 29, -94, 10, 0), (-1, 29.001, -94, 10, 90), (0, 29.002, -94, 10, 180)]
    assert any("capped" in w for w in m.predict(hist(wild), H).warnings)


def test_damped_refuses_cutoffs_inside_its_fit_window(tmp_path, monkeypatch):
    import maritime_forecast.models.damped_velocity as dv

    monkeypatch.setattr(dv, "FIT_DIR", tmp_path)
    fit = dv.DampedVelocityFit(protocol="P-known", dataset_id="d", manifest_sha256="a" * 64,
                               split="train", data_until=AS_OF + timedelta(hours=1),
                               taus_hours={"default": 2.0}, n_cutoffs={}, objective={}, config={},
                               data_provenance="x", git_sha="x", created_at=AS_OF)  # fmt: skip
    dv.fit_path("P-known", "d").write_text(fit.model_dump_json())
    ctx = ModelContext(dataset_id="d", protocol="P-known", manifest_sha256="a" * 64)
    f = get_model("damped_velocity_v1", ctx).predict(hist([(0, 29, -94, 10, 90)]), H)
    assert f.status == "unsupported" and f.error.code == "fit_overlaps_as_of" and not f.points
    no_manifest = ModelContext(dataset_id="d", protocol="P-known", manifest_sha256=None)
    assert not get_model("damped_velocity_v1", no_manifest).capabilities().installed
