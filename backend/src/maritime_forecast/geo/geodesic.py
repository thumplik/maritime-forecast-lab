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
