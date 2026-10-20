import numpy as np
import pytest

from maritime_forecast.geo.geodesic import (
    KNOT_MPS,
    along_cross_track,
    direct,
    distance_m,
    inverse,
    knots_to_mps,
    wrap_lon,
)


def test_knot_conversion_exact():
    assert knots_to_mps(1.0) == pytest.approx(0.514444, rel=1e-6)
    assert pytest.approx(1852.0) == KNOT_MPS * 3600


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315, 359.9])
def test_direct_inverse_roundtrip_all_quadrants(bearing):
    lat, lon = direct(29.3, -94.7, bearing, 10_000.0)
    d, b = inverse(29.3, -94.7, lat, lon)
    assert d == pytest.approx(10_000.0, abs=1e-3)
    assert (b - bearing + 180) % 360 - 180 == pytest.approx(0.0, abs=1e-6)


def test_one_nmi_north_is_one_arcminute_ish():
    lat, lon = direct(0.0, 0.0, 0.0, 1852.0)
    assert lat == pytest.approx(1 / 60, rel=0.01)
    assert lon == pytest.approx(0.0, abs=1e-12)


def test_antimeridian_crossing_wraps():
    lat, lon = direct(10.0, 179.99, 90.0, 5_000.0)
    assert -180 <= lon < -179.9
    assert distance_m(10.0, 179.99, lat, lon) == pytest.approx(5_000.0, abs=1e-3)


def test_high_latitude():
    lat, lon = direct(85.0, 0.0, 90.0, 20_000.0)
    assert distance_m(85.0, 0.0, lat, lon) == pytest.approx(20_000.0, abs=1e-3)
    assert lon > 1.0  # meridians converge: 20 km east is > 1 degree of longitude at 85N


def test_zero_distance():
    lat, lon = direct(29.0, -94.0, 123.0, 0.0)
    assert (lat, lon) == pytest.approx((29.0, -94.0))


def test_wrap_lon():
    np.testing.assert_allclose(wrap_lon([180.0, -180.0, 190.0, -190.0]), [-180, -180, -170, 170])


def test_along_cross_track_signs():
    # truth heading north; prediction 1 km ahead and 1 km to the east (starboard)
    lat_a, lon_a = direct(29.0, -94.0, 0.0, 1000.0)
    lat_p, lon_p = direct(lat_a, lon_a, 90.0, 1000.0)
    along, cross = along_cross_track(lat_p, lon_p, 29.0, -94.0, 0.0)
    assert along == pytest.approx(1000.0, rel=0.01)
    assert cross == pytest.approx(1000.0, rel=0.01)
