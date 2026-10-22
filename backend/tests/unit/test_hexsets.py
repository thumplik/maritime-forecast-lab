"""Hex prediction sets (evaluation/hexsets.py) on SYNTHETIC inputs."""

import math

import numpy as np
import pytest

from maritime_forecast.evaluation import hexsets as hx
from maritime_forecast.geo.geodesic import LocalFrame

LAT0, LON0 = 29.0, -94.0
FRAME = LocalFrame(LAT0, LON0)


def _mix(xy, w=None, h=2000.0, broad=20000.0, eps=0.05, centre=(0.0, 0.0)):
    xy = np.asarray(xy, float)
    return hx.Mixture(xy, np.ones(len(xy)) if w is None else np.asarray(w, float), h, centre,
                      broad, eps)  # fmt: skip


def test_mixture_is_a_density():
    m = _mix([[0, 0], [30000, 0]], w=[1, 3])
    g = np.linspace(-150000, 180000, 661)
    gx, gy = np.meshgrid(g, g)
    total = m.density(gx, gy).sum() * (g[1] - g[0]) ** 2
    assert total == pytest.approx(1.0, abs=1e-3)


def test_level_set_matches_brute_force_and_is_multimodal():
    m = _mix([[-25000, 0], [25000, 0]], h=3000.0, broad=40000.0, eps=0.02)
    res = 7
    tau = float(m.density(np.array([-25000.0 + 4000]), np.array([0.0]))[0])
    got = set(hx.level_set_cells(m, FRAME, tau, res))
    brute = hx._disk(LAT0, LON0, 120000, res)
    la, lo = hx.cell_centres(brute)
    x, y = FRAME.to_xy(la, lo)
    want = {c for c, d in zip(brute, m.density(x, y), strict=True) if d >= tau}
    assert got == want and got
    assert hx.cell_of(LAT0, LON0, res) not in got  # the gap between the two branches
    xs = FRAME.to_xy(*hx.cell_centres(sorted(got)))[0]
    assert (xs < -15000).any() and (xs > 15000).any()  # both branches


def test_broad_component_alone_when_there_are_no_candidates():
    m = _mix(np.zeros((0, 2)), broad=10000.0)
    assert m.broad_peak() == pytest.approx(1 / (2 * math.pi * 1e8))
    cells = hx.level_set_cells(m, FRAME, m.broad_peak() * math.exp(-0.5), 7)  # r = 1 sigma
    area = len(cells) * hx.cell_area_km2(7)
    assert area == pytest.approx(math.pi * 10**2, rel=0.15)


def test_conformal_density_threshold_covers_at_the_nominal_level():
    rng = np.random.default_rng(0)
    m = _mix([[0, 0], [20000, 5000]], w=[2, 1], h=4000.0, broad=30000.0, eps=0.05)

    def draw(n):  # samples from the mixture itself
        comp = rng.choice(3, n, p=[0.95 * 2 / 3, 0.95 / 3, 0.05])
        mu = np.array([[0, 0], [20000, 5000], [0, 0]])[comp]
        sd = np.array([4000.0, 4000.0, 30000.0])[comp][:, None]
        return mu + sd * rng.standard_normal((n, 2))

    cal, test = draw(2000), draw(4000)
    tau = hx.density_threshold(m.density(cal[:, 0], cal[:, 1]), 0.9)
    cover = (m.density(test[:, 0], test[:, 1]) >= tau).mean()
    assert 0.88 <= cover <= 0.92
    assert hx.density_threshold(np.array([1.0, 2.0]), 0.9) is None  # n too small


def test_disc_cells_and_scores():
    cells = hx.disc_cells(LAT0, LON0, 10000.0, 7)
    assert len(cells) * hx.cell_area_km2(7) == pytest.approx(math.pi * 100, rel=0.1)
    import h3

    # a point's own cell centre is within one edge length of it
    assert hx.disc_score(FRAME, LAT0, LON0, 7) < h3.average_hexagon_edge_length(7, "m")
    assert hx.cell_of(LAT0, LON0, 7) in cells
    assert hx.resolution_for(3) == 7 and hx.resolution_for(6) == 6


@pytest.mark.parametrize("tau_scale", [0.3, 1.0, 3.0, 30.0])
def test_level_set_is_exact_across_thresholds(tau_scale):
    """Brute force over a wide disk equals the bounded enumeration, whether the broad part,
    the kernels or both decide the set."""
    rng = np.random.default_rng(3)
    m = _mix(rng.normal(0, 15000, (30, 2)), w=rng.random(30), h=2500.0, broad=25000.0, eps=0.05)
    tau = tau_scale * m.broad_peak()
    got = set(hx.level_set_cells(m, FRAME, tau, 6))
    brute = hx._disk(LAT0, LON0, 250000, 6)
    la, lo = hx.cell_centres(brute)
    x, y = FRAME.to_xy(la, lo)
    assert got == {c for c, d in zip(brute, m.density(x, y), strict=True) if d >= tau}


@pytest.mark.parametrize("res,radius_m", [(7, 100_000.0), (6, 300_000.0), (6, 400_000.0)])
def test_disk_contains_every_centre_within_large_radii(res, radius_m):
    """Reference: a much larger k-ring, filtered by centre distance (not _disk itself)."""
    import h3

    lat, lon = 29.3, -94.7
    edge = h3.average_hexagon_edge_length(res, "m")
    ref = list(h3.grid_disk(hx.cell_of(lat, lon, res), int(3 * radius_m / edge)))
    la, lo = hx.cell_centres(ref)
    x, y = LocalFrame(lat, lon).to_xy(la, lo)
    want = {c for c, d in zip(ref, np.hypot(x, y), strict=True) if d <= radius_m}
    assert want <= set(hx._disk(lat, lon, radius_m, res))
    assert set(hx.disc_cells(lat, lon, radius_m, res)) == want


def test_h1_bandwidth_scales_with_candidate_spread():
    tight = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    wide = tight * 100
    w = np.ones(3)
    assert hx.h1_bandwidth(tight, w, 0.5) == hx.H1_MIN_BANDWIDTH_M  # floor
    assert hx.h1_bandwidth(wide, w, 0.5) == pytest.approx(0.5 * hx.weighted_spread(wide, w))
    assert hx.weighted_spread(wide[:1], w[:1]) == 0.0
    la, lo = FRAME.to_latlon(wide[:, 0], wide[:, 1])
    m = hx.h1_mixture(FRAME, la, lo, np.array([1.0, 0.0, 1.0]), 0.5, 20000.0)
    assert len(m.xy) == 2 and m.h >= hx.H1_MIN_BANDWIDTH_M  # zero weight is not a candidate
    assert len(hx.h1_mixture(FRAME, la[:0], lo[:0], w[:0], 0.5, 20000.0).xy) == 0


def _reports(sogs, cls="cargo"):
    from datetime import UTC, datetime, timedelta

    import polars as pl

    t0 = datetime(2023, 6, 1, 12, tzinfo=UTC)
    n = len(sogs)
    return pl.DataFrame({"observed_at": [t0 - timedelta(minutes=5 * (n - 1 - i)) for i in range(n)],
                         "sog_knots": sogs, "vessel_class": [cls] * n})  # fmt: skip


def test_is_transiting_follows_the_sampler_rule():
    assert hx.is_transiting(_reports([10.0] * 8))
    assert not hx.is_transiting(_reports([10.0] * 8, cls="tug_tow"))  # not a transiting class
    assert not hx.is_transiting(_reports([10.0] * 6 + [0.3, 10.0]))  # a stop in the last 30 min
    assert hx.is_transiting(_reports([0.0] + [10.0] * 7))  # the stop is older than 30 min
    assert not hx.is_transiting(_reports([1.5] * 8))  # too slow


def test_h1_set_returns_cells_area_and_outline():
    cell = {"c": 0.5, "broad_m": 20000.0, "tau": 1e-9, "res": 6, "stratum": "underway:ALL",
            "n": 300, "n_vessels": 120, "eps": 0.05, "min_bandwidth_m": 500.0}  # fmt: skip
    la, lo = FRAME.to_latlon(np.array([0.0, 5000.0]), np.array([0.0, 0.0]))
    got = hx.h1_set(cell, LAT0, LON0, la, lo, np.array([1.0, 1.0]))
    assert got["cells"] and got["area_km2"] == pytest.approx(
        len(got["cells"]) * hx.cell_area_km2(6)
    )
    assert got["boundary"]["type"] in ("Polygon", "MultiPolygon") and got["n_candidates"] == 2
    assert hx.cell_of(LAT0, LON0, 6) in got["cells"]


def test_h1_set_layers_are_nested_and_the_main_set_unchanged():
    base = {"c": 0.5, "broad_m": 20000.0, "res": 6, "stratum": "underway:ALL", "n": 300,
            "n_vessels": 120, "eps": 0.05, "min_bandwidth_m": 500.0}  # fmt: skip
    la, lo = FRAME.to_latlon(np.array([0.0, 8000.0, 30000.0]), np.array([0.0, 0.0, 5000.0]))
    w = np.array([3.0, 2.0, 1.0])
    plain = hx.h1_set({**base, "tau": 2e-10}, LAT0, LON0, la, lo, w)
    layered = hx.h1_set({**base, "tau": 2e-10, "taus": {"0.5": 2e-9, "0.8": 8e-10, "0.9": 2e-10}},
                        LAT0, LON0, la, lo, w)  # fmt: skip
    assert layered["cells"] == plain["cells"] and plain["layers"] == []
    sizes = [x["n_cells"] for x in layered["layers"]]
    assert [x["level"] for x in layered["layers"]] == [0.5, 0.8, 0.9]
    assert sizes == sorted(sizes) and sizes[-1] == len(plain["cells"]) and sizes[0] > 0
