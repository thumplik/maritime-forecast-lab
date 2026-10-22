"""T1-pos adapter (models/timestral_adapter.py) on SYNTHETIC histories, with a stand-in backend.
The real checkpoint is exercised only by tests marked `timestral` (optional extra + weights)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from fixtures.fake_timestral import LinearBackend

import maritime_forecast.models.timestral_adapter as ta
from maritime_forecast.geo.geodesic import LocalFrame, direct
from maritime_forecast.models.base import AISHistory, ForecastHorizon, ModelContext
from maritime_forecast.models.registry import get_model, product_model_ids

T0 = datetime(2023, 6, 1, 12, tzinfo=UTC)


def _history(minutes, lat0=29.0, lon0=-94.0, cog=225.0, kn=12.0, as_of=None):
    """SYNTHETIC straight track: reports at T0 - m minutes (m in `minutes`), moving along cog."""
    rows = []
    for m in sorted(minutes, reverse=True):
        d = -kn * 1852 / 3600 * m * 60  # metres back along the course from the T0 position
        lat, lon = direct(lat0, lon0, cog, d)
        rows.append({"vessel_id": "999000009", "observed_at": T0 - timedelta(minutes=m),
                     "latitude_deg": float(lat), "longitude_deg": float(lon), "sog_knots": kn,
                     "cog_deg": cog, "heading_deg": None, "nav_status": None,
                     "vessel_class": "cargo", "quality_flags": []})  # fmt: skip
    df = pl.DataFrame(rows).with_columns(pl.col("observed_at").dt.cast_time_unit("us"))
    return AISHistory("999000009", as_of or T0, 3.0, df)


def test_context_grid_is_causal_and_never_bridges_gaps():
    h = _history([0, 5, 10, 15, 20, 70, 75, 80])  # 50-min gap between 20 and 70 min ago
    frame = LocalFrame(h.last()["latitude_deg"], h.last()["longitude_deg"])
    ctx = ta.context_xy(h, frame, step_minutes=10, gap_minutes=30)
    assert ctx.shape == (2, 19)  # 3 h at 10 min, as_of included, oldest first
    obs = np.isfinite(ctx[0])
    # grid k minutes before as_of -> column 18 - k/10
    assert obs[18] and obs[17] and obs[16]  # 0, 10, 20 min: on reports
    assert not obs[15] and not obs[14] and not obs[13] and not obs[12]  # inside the 50-min gap
    assert obs[11] and obs[10]  # 70, 80 min
    assert not obs[:10].any()  # before the first report
    assert ctx[0, 18] == pytest.approx(0, abs=1e-6) and ctx[1, 18] == pytest.approx(0, abs=1e-6)


def test_context_interpolates_between_close_reports_only():
    h = _history([0, 7, 14])  # 7-min cadence: the 10-min grid point lies between two reports
    frame = LocalFrame(h.last()["latitude_deg"], h.last()["longitude_deg"])
    ctx = ta.context_xy(h, frame, step_minutes=10, gap_minutes=30)
    x_at = lambda m: frame.to_xy(*[h.reports.filter(  # noqa: E731
        pl.col("observed_at") == T0 - timedelta(minutes=m))[c][0]
        for c in ("latitude_deg", "longitude_deg")])[0]  # fmt: skip
    assert ctx[0, -2] == pytest.approx(x_at(7) + (3 / 7) * (x_at(14) - x_at(7)), rel=1e-9)


def test_stale_as_of_leaves_the_last_grid_point_unobserved():
    h = _history([0, 10, 20], as_of=T0 + timedelta(minutes=25))
    frame = LocalFrame(h.last()["latitude_deg"], h.last()["longitude_deg"])
    ctx = ta.context_xy(h, frame, step_minutes=10, gap_minutes=30)
    assert not np.isfinite(ctx[0, -1])  # no report at or after as_of: nothing to bracket with


def test_forecast_on_grid_without_clipping_and_heading_south_west():
    fake = LinearBackend()
    model = get_model("timestral_pos_v1", ModelContext(params={"backend": fake}))
    h = _history(range(0, 121, 5))  # 2 h at 5 min, heading 225° (south-west)
    hz = ForecastHorizon(T0, 1, 10)
    f = model.predict(h, hz)
    assert f.status == "ok", f.error
    assert [p.timestamp for p in f.points] == hz.target_times
    assert fake.calls[0]["clip_nonnegative"] is False and fake.calls[0]["symmetric_averaging"]
    assert fake.calls[0]["shape"] == (1, 2, 19)
    # the vessel keeps going south-west (a clipped forecast would sit at the anchor)
    last = h.last()
    assert f.points[-1].latitude_deg < last["latitude_deg"] - 0.1
    assert f.points[-1].longitude_deg < last["longitude_deg"] - 0.1
    # and 1 h at 12 kn is 22.2 km along the course
    lat, lon = direct(last["latitude_deg"], last["longitude_deg"], 225.0, 12 * 1852)
    assert f.points[-1].latitude_deg == pytest.approx(float(lat), abs=2e-3)
    assert f.points[-1].longitude_deg == pytest.approx(float(lon), abs=2e-3)


def test_typed_errors_when_not_installed_or_context_too_short(monkeypatch):
    monkeypatch.setattr(ta, "package_available", lambda: False)
    model = get_model("timestral_pos_v1")
    assert model.capabilities().installed is False and model.capabilities().optional
    f = model.predict(_history(range(0, 60, 5)), ForecastHorizon(T0, 1, 10))
    assert f.status == "unsupported" and f.error.code == "not_installed" and not f.points
    fake = get_model("timestral_pos_v1", ModelContext(params={"backend": LinearBackend()}))
    f = fake.predict(_history([0, 50]), ForecastHorizon(T0, 1, 10))
    assert f.status == "unsupported" and f.error.code == "insufficient_context"


def test_optional_models_are_not_in_the_default_benchmark_set():
    assert "timestral_pos_v1" not in product_model_ids()
    assert "timestral_vel_v1" not in product_model_ids()
    assert "persistence_v1" in product_model_ids()


@pytest.mark.timestral
def test_real_checkpoint_heads_south_west_and_is_deterministic():
    """Needs `uv sync --extra timestral` and `make timestral-weights` (run with -m timestral)."""
    if not (ta.package_available() and ta.weights_present()):
        pytest.skip("timestral extra or weights not installed")
    model = get_model("timestral_pos_v1")
    h = _history(range(0, 181, 1))
    hz = ForecastHorizon(T0, 1, 10)
    a, b = model.predict(h, hz), model.predict(h, hz)
    assert a.status == "ok", a.error
    assert a.points == b.points
    last = h.last()
    assert a.points[-1].latitude_deg < last["latitude_deg"]
    assert a.points[-1].longitude_deg < last["longitude_deg"]


def test_a_different_package_commit_is_not_installed(monkeypatch):
    monkeypatch.setattr(ta, "installed_commit", lambda: "0" * 40)
    assert not ta.package_available()
    f = get_model("timestral_pos_v1").predict(_history(range(0, 60, 5)), ForecastHorizon(T0, 1, 10))
    assert f.error.code == "not_installed" and "found 0000000" in f.error.message


@pytest.mark.timestral
def test_real_checkpoint_keeps_no_state_between_forecasts():
    """One backend serves every cutoff: forecasting A first must not change B."""
    if not (ta.package_available() and ta.weights_present()):
        pytest.skip("timestral extra or weights not installed")
    model = get_model("timestral_pos_v1")
    hz = ForecastHorizon(T0, 1, 10)
    a, b = _history(range(0, 181, 1)), _history(range(0, 181, 3), cog=40.0, kn=8.0)
    first = model.predict(b, hz)
    model.predict(a, hz)
    assert model.predict(b, hz).points == first.points


def test_velocity_forecast_integrates_from_the_position_at_as_of():
    fake = LinearBackend()
    model = get_model("timestral_vel_v1", ModelContext(params={"backend": fake}))
    h = _history(range(0, 121, 5))  # 12 kn heading 225°
    hz = ForecastHorizon(T0, 1, 10)
    f = model.predict(h, hz)
    assert f.status == "ok", f.error
    assert fake.calls[0]["shape"] == (1, 2, 18)  # one fewer than the position grid
    assert fake.calls[0]["clip_nonnegative"] is False
    vel = ta.velocity_series(np.array([[0.0, 600.0, np.nan, 1800.0]] * 2), 10)
    assert vel[0, 0] == pytest.approx(1.0) and np.isnan(vel[0, 1:]).all()  # gaps give none
    last = h.last()
    lat, lon = direct(last["latitude_deg"], last["longitude_deg"], 225.0, 12 * 1852)
    assert f.points[-1].latitude_deg == pytest.approx(float(lat), abs=2e-3)
    assert f.points[-1].longitude_deg == pytest.approx(float(lon), abs=2e-3)


def test_velocity_refuses_without_a_position_at_as_of():
    model = get_model("timestral_vel_v1", ModelContext(params={"backend": LinearBackend()}))
    stale = _history(range(0, 121, 5), as_of=T0 + timedelta(minutes=25))
    f = model.predict(stale, ForecastHorizon(stale.as_of, 1, 10))
    assert f.status == "unsupported" and f.error.code == "stale_anchor"


@pytest.mark.timestral
def test_real_checkpoint_velocity_heads_south_west():
    if not (ta.package_available() and ta.weights_present()):
        pytest.skip("timestral extra or weights not installed")
    f = get_model("timestral_vel_v1").predict(
        _history(range(0, 181, 1)), ForecastHorizon(T0, 1, 10)
    )
    assert f.status == "ok", f.error
    last = _history(range(0, 181, 1)).last()
    assert f.points[-1].latitude_deg < last["latitude_deg"] - 0.05
    assert f.points[-1].longitude_deg < last["longitude_deg"] - 0.05
