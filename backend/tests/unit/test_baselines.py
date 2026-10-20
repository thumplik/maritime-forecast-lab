import numpy as np
import pytest

from maritime_forecast.geo.geodesic import distance_m, inverse
from maritime_forecast.models.baselines import (
    constant_velocity,
    persistence,
    velocity_from_last_two,
)

LEADS = np.array([600.0, 3600.0, 7200.0])


def test_persistence_shape_and_values():
    lat, lon = persistence(np.array([29.0, 28.0]), np.array([-94.0, -95.0]), LEADS)
    assert lat.shape == (2, 3)
    assert np.all(lat[0] == 29.0) and np.all(lon[1] == -95.0)


def test_cv_distance_matches_speed():
    lat, lon = constant_velocity(29.0, -94.0, 10.0, 45.0, LEADS)
    d = distance_m(29.0, -94.0, lat, lon)
    np.testing.assert_allclose(d, 10 * 1852 * LEADS / 3600, rtol=1e-9)
    _, brg = inverse(29.0, -94.0, lat[..., 0], lon[..., 0])
    assert brg == pytest.approx(45.0, abs=1e-6)


def test_cv_zero_speed_missing_cog_is_persistence():
    lat, lon = constant_velocity(29.0, -94.0, 0.0, np.nan, LEADS)
    np.testing.assert_allclose(lat, 29.0)
    np.testing.assert_allclose(lon, -94.0)


def test_cv_missing_cog_moving_is_nan():
    lat, lon = constant_velocity(29.0, -94.0, 5.0, np.nan, LEADS)
    assert np.all(np.isnan(lat)) and np.all(np.isnan(lon))


def test_cv_vectorized_batch():
    lat, lon = constant_velocity([29.0, 29.0], [-94.0, -94.0], [5.0, 0.0], [90.0, 90.0], LEADS)
    assert lat.shape == (2, 3)
    assert np.all(lon[0] > -94.0) and np.allclose(lon[1], -94.0)


def test_velocity_from_last_two_roundtrip():
    lat, lon = constant_velocity(29.0, -94.0, 12.0, 200.0, np.array([0.0, 600.0]))
    sog, cog = velocity_from_last_two(lat, lon, 600.0)
    assert sog == pytest.approx(12.0, rel=1e-9)
    assert cog == pytest.approx(200.0, abs=1e-6)
