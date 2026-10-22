"""Hex prediction sets (build plan v0.4, §5 H, §6.7): where will the vessel be, as a set of H3
cells that holds the true position with a calibrated probability.

Two set constructions, both calibrated the same way (split conformal, marginal coverage):

* **Density level set (H1).** A kernel density in the local AEQD frame from candidate future
  positions (e.g. retrieved continuations) with weights, mixed with a small broad Gaussian
  around a central point so the truth never has zero density. The nonconformity score is
  minus the density at the true position's cell centre; the set is every cell whose centre
  density is at least the calibrated threshold. It can be multimodal (both branches of a
  junction) and is never forced to be a disc.
* **Disc baseline.** U1's conformal disc around a point forecast, as cells: the score is the
  distance from the point to the true position's cell centre, the set is every cell whose
  centre lies within the calibrated radius.

Coverage is decided at cell level: covered if the true position's cell is in the set. Areas
are cell counts times the resolution's average cell area. Pure functions; scripts do the I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from maritime_forecast.evaluation.conformal import conformal_quantile
from maritime_forecast.geo.geodesic import LocalFrame


def resolution_for(horizon_h: float) -> int:
    """H3 resolution per horizon (§5 H): res 7 (~5.2 km^2) up to 3 h, res 6 (~36 km^2) after."""
    return 7 if horizon_h <= 3 else 6


def cell_area_km2(res: int) -> float:
    import h3

    return float(h3.average_hexagon_area(res, "km^2"))


def cell_of(lat: float, lon: float, res: int) -> str:
    import h3

    return h3.latlng_to_cell(float(lat), float(lon), res)


def cell_centres(cells: list[str]) -> tuple[np.ndarray, np.ndarray]:
    import h3

    ll = np.array([h3.cell_to_latlng(c) for c in cells], dtype=float).reshape(-1, 2)
    return ll[:, 0], ll[:, 1]


def _disk(lat: float, lon: float, radius_m: float, res: int) -> list[str]:
    """Cells of an H3 disk around a point containing every cell whose centre lies within
    `radius_m` of it (a superset; callers filter by distance or density). The ring count is
    grown until every centre on the outer ring is farther than the radius: a k-ring's nearest
    boundary is only ~1.5 k edge lengths away and edges vary with latitude and position."""
    import h3

    centre = cell_of(lat, lon, res)
    edge = h3.average_hexagon_edge_length(res, "m")
    k = int(math.ceil(radius_m / (1.5 * edge))) + 1
    frame = LocalFrame(lat, lon)
    while True:
        ring = list(h3.grid_ring(centre, k))
        la, lo = cell_centres(ring)
        x, y = frame.to_xy(la, lo)
        if np.hypot(x, y).min() > radius_m:
            return list(h3.grid_disk(centre, k))
        k += 1


# -- disc baseline -------------------------------------------------------------------------
def disc_score(frame: LocalFrame, truth_lat: float, truth_lon: float, res: int) -> float:
    """Distance (m) from the frame centre (the point forecast) to the true cell's centre."""
    la, lo = cell_centres([cell_of(truth_lat, truth_lon, res)])
    x, y = frame.to_xy(la, lo)
    return float(np.hypot(x[0], y[0]))


def disc_cells(lat: float, lon: float, radius_m: float, res: int) -> list[str]:
    """Cells whose centre lies within `radius_m` of (lat, lon)."""
    cells = _disk(lat, lon, radius_m, res)
    la, lo = cell_centres(cells)
    x, y = LocalFrame(lat, lon).to_xy(la, lo)
    keep = np.hypot(x, y) <= radius_m
    return [c for c, k in zip(cells, keep, strict=True) if k]


# -- density level sets --------------------------------------------------------------------
@dataclass(frozen=True)
class Mixture:
    """Weighted isotropic Gaussian kernels (bandwidth h, metres) at candidate positions in a
    local frame, mixed with weight `eps` with one broad Gaussian (scale `broad_m`) at
    `centre_xy`. A proper density on the plane (per m^2)."""

    xy: np.ndarray  # (k, 2) candidate positions, metres
    w: np.ndarray  # (k,) non-negative weights; normalised internally
    h: float
    centre_xy: tuple[float, float]
    broad_m: float
    eps: float

    def __post_init__(self) -> None:
        if self.h <= 0 or self.broad_m <= 0 or not 0 < self.eps <= 1:
            raise ValueError("h, broad_m > 0 and 0 < eps <= 1")

    def density(self, qx: np.ndarray, qy: np.ndarray) -> np.ndarray:
        qx, qy = np.asarray(qx, float), np.asarray(qy, float)
        out = np.zeros(np.broadcast(qx, qy).shape)
        if len(self.xy) and self.w.sum() > 0 and self.eps < 1:
            w = self.w / self.w.sum()
            d2 = (qx[..., None] - self.xy[:, 0]) ** 2 + (qy[..., None] - self.xy[:, 1]) ** 2
            k = np.exp(-d2 / (2 * self.h**2)) / (2 * np.pi * self.h**2)
            out += (1 - self.eps) * (k * w).sum(-1)
            eps = self.eps
        else:  # no candidate: the broad component is the whole density
            eps = 1.0
        bx, by = self.centre_xy
        b2 = (qx - bx) ** 2 + (qy - by) ** 2
        return out + eps * np.exp(-b2 / (2 * self.broad_m**2)) / (2 * np.pi * self.broad_m**2)

    def broad_peak(self) -> float:
        eps = self.eps if len(self.xy) and self.w.sum() > 0 and self.eps < 1 else 1.0
        return eps / (2 * np.pi * self.broad_m**2)


def density_score(mix: Mixture, frame: LocalFrame, truth_lat: float, truth_lon: float,
                  res: int) -> float:  # fmt: skip
    """Density at the true cell's centre (higher = more conforming)."""
    la, lo = cell_centres([cell_of(truth_lat, truth_lon, res)])
    x, y = frame.to_xy(la, lo)
    return float(mix.density(x, y)[0])


def level_set_cells(mix: Mixture, frame: LocalFrame, tau: float, res: int) -> list[str]:
    """Every cell whose centre density is >= tau, enumerated exactly but only where possible.

    Weights sum to 1, so the kernel part at a point is at most (1 - eps) K(d_min), with K the
    single-kernel density and d_min the distance to the nearest candidate. Outside the disk
    where the broad part falls to tau/2 it contributes < tau/2 (and never more than its peak
    b), so a qualifying cell there needs K(d_min) >= max(tau - b, tau/2) / (1 - eps): one
    radius R around every candidate. Inside that broad disk every cell is checked."""
    if tau <= 0:
        raise ValueError("tau must be positive")
    has_k = len(mix.xy) and mix.w.sum() > 0 and mix.eps < 1
    b = mix.broad_peak()
    seeds: list[tuple[float, float, float]] = []
    if b > tau / 2:  # the broad part alone can matter: check its whole tau/2 disk
        r = mix.broad_m * math.sqrt(2 * math.log(b / (tau / 2)))
        cla, clo = frame.to_latlon(np.array([mix.centre_xy[0]]), np.array([mix.centre_xy[1]]))
        seeds.append((float(cla[0]), float(clo[0]), max(r, 1.0)))
    if has_k:
        need = max(tau - b, tau / 2) / (1 - mix.eps)
        kmax = 1 / (2 * math.pi * mix.h**2)
        if need < kmax:
            radius = mix.h * math.sqrt(2 * math.log(kmax / need))
            la, lo = frame.to_latlon(mix.xy[:, 0], mix.xy[:, 1])
            unique = {cell_of(a, o, res): (a, o) for a, o in zip(la, lo, strict=True)}
            seeds += [(a, o, radius) for a, o in unique.values()]
    cells: set[str] = set()
    for a, o, r in seeds:
        cells.update(_disk(a, o, r, res))
    if not cells:
        return []
    cl = sorted(cells)
    la, lo = cell_centres(cl)
    x, y = frame.to_xy(la, lo)
    keep = mix.density(x, y) >= tau
    return [c for c, k in zip(cl, keep, strict=True) if k]


def density_threshold(scores: np.ndarray, level: float) -> float | None:
    """Split-conformal threshold on densities: the set {density >= tau} covers a new sample
    with probability >= level (marginal, exchangeable). None if n is too small."""
    q = conformal_quantile(-np.asarray(scores, float), level)
    return None if q is None else -q
