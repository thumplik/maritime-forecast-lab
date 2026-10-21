"""WGS84 geodesic helpers (vectorized over NumPy arrays).

Conventions: degrees, bearings clockwise from true north, distances in metres,
longitudes normalized to [-180, 180).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pyproj import Geod

WGS84 = Geod(ellps="WGS84")

KNOT_MPS = 1852.0 / 3600.0  # exact: 1 nmi = 1852 m
NMI_M = 1852.0
EARTH_RADIUS_M = 6_371_008.8  # mean radius, used only for along/cross-track on the sphere

Arr = NDArray[np.float64]


def knots_to_mps(kn: ArrayLike) -> Arr:
    return np.asarray(kn, dtype=float) * KNOT_MPS


def wrap_lon(lon: ArrayLike) -> Arr:
    return (np.asarray(lon, dtype=float) + 180.0) % 360.0 - 180.0


def lon_lerp_sql(lon_a: str, lon_b: str, w: str) -> str:
    """SQL for interpolating longitude a -> b by fraction w along the shorter way round,
    wrapped to [-180, 180). Plain a + w*(b-a) goes the long way across the antimeridian."""
    a, b, w = (f"CAST(({x}) AS DOUBLE)" for x in (lon_a, lon_b, w))
    dlon = f"(({b} - {a} + 540) % 360 - 180)"
    return f"(({a} + {w} * {dlon} + 540) % 360 - 180)"


def wrap_bearing(deg: ArrayLike) -> Arr:
    return np.asarray(deg, dtype=float) % 360.0


def direct(lat: ArrayLike, lon: ArrayLike, bearing_deg: ArrayLike, dist_m: ArrayLike):
    """Destination (lat, lon) after travelling dist_m along the initial bearing."""
    lat, lon, brg, d = np.broadcast_arrays(
        np.asarray(lat, float),
        np.asarray(lon, float),
        np.asarray(bearing_deg, float),
        np.asarray(dist_m, float),
    )
    lon2, lat2, _ = WGS84.fwd(lon, lat, brg, d)
    return np.asarray(lat2, float), wrap_lon(lon2)


def inverse(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike):
    """Geodesic distance (m) and initial bearing (deg, [0, 360)) from point 1 to point 2."""
    a = np.broadcast_arrays(*(np.asarray(x, float) for x in (lat1, lon1, lat2, lon2)))
    az, _, dist = WGS84.inv(a[1], a[0], a[3], a[2])
    return np.asarray(dist, float), wrap_bearing(az)


def distance_m(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike) -> Arr:
    return inverse(lat1, lon1, lat2, lon2)[0]


def along_cross_track(
    lat_pred: ArrayLike,
    lon_pred: ArrayLike,
    lat_true: ArrayLike,
    lon_true: ArrayLike,
    true_course_deg: ArrayLike,
):
    """Decompose prediction error into along-track and cross-track components (m).

    The reference direction is the true track's course at the truth point. Positive
    along-track = prediction is ahead of truth; positive cross-track = to starboard.
    Uses the local tangent plane at the truth point (accurate for errors << Earth radius).
    """
    dist, brg = inverse(lat_true, lon_true, lat_pred, lon_pred)
    rel = np.deg2rad(brg - np.asarray(true_course_deg, float))
    return dist * np.cos(rel), dist * np.sin(rel)


class LocalFrame:
    """Azimuthal-equidistant plane centred at (lat0, lon0) on WGS84: x east, y north, metres.

    This is the project's documented local frame. Distances and bearings FROM the centre are
    exact; between two other points the error grows with their distance from the centre
    (well under 0.1% within ~100 km). Recover lat/lon with `to_latlon` (exact inverse), so
    the antimeridian is handled by the projection, not by longitude arithmetic.
    """

    def __init__(self, lat0: float, lon0: float):
        from pyproj import Transformer

        self.lat0, self.lon0 = float(lat0), float(lon0)
        self._t = Transformer.from_crs(
            "EPSG:4326",
            f"+proj=aeqd +lat_0={self.lat0} +lon_0={self.lon0} +ellps=WGS84 +units=m",
            always_xy=True,
        )

    def to_xy(self, lat: ArrayLike, lon: ArrayLike) -> tuple[Arr, Arr]:
        x, y = self._t.transform(np.asarray(lon, float), np.asarray(lat, float))
        return np.asarray(x, float), np.asarray(y, float)

    def to_latlon(self, x: ArrayLike, y: ArrayLike) -> tuple[Arr, Arr]:
        lon, lat = self._t.transform(
            np.asarray(x, float), np.asarray(y, float), direction="INVERSE"
        )
        return np.asarray(lat, float), wrap_lon(lon)
