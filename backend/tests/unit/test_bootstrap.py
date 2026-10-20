"""Vessel-cluster bootstrap on SYNTHETIC values."""

import numpy as np
import pytest

from maritime_forecast.evaluation.bootstrap import vessel_bootstrap_ci, vessel_bootstrap_stats


def _data(seed=0):
    rng = np.random.default_rng(seed)
    vessel_effect = rng.normal(0, 3, 40)
    ids = np.repeat(np.arange(40), rng.integers(1, 30, 40))
    vals = 10 + vessel_effect[ids] + rng.normal(0, 1, len(ids))
    return vals, ids.astype(str)


def test_point_estimates_exact_and_ci_brackets():
    v, ids = _data()
    s = vessel_bootstrap_stats(v, ids, n_boot=500)
    assert s["mean"][0] == pytest.approx(v.mean()) and s["median"][0] == pytest.approx(np.median(v))
    for est, lo, hi in s.values():
        assert lo <= est <= hi


def test_agrees_with_resampling_implementation():
    v, ids = _data(1)
    fast = vessel_bootstrap_stats(v, ids, n_boot=2000, seed=3)
    slow_mean = vessel_bootstrap_ci(v, ids, np.mean, n_boot=2000, seed=4)
    # different random streams, same estimator: CI bounds agree within Monte-Carlo error
    assert fast["mean"][1] == pytest.approx(slow_mean[1], abs=0.35)
    assert fast["mean"][2] == pytest.approx(slow_mean[2], abs=0.35)


def test_cluster_ci_wider_than_iid():
    v, ids = _data(2)
    clustered = vessel_bootstrap_stats(v, ids, n_boot=1000)["mean"]
    iid = vessel_bootstrap_stats(v, np.arange(len(v)).astype(str), n_boot=1000)["mean"]
    assert (clustered[2] - clustered[1]) > 1.5 * (iid[2] - iid[1])


def test_paired_resamples_are_identical_across_models():
    v, ids = _data(3)
    order = np.unique(ids)
    a = vessel_bootstrap_stats(v, ids, n_boot=300, seed=9, vessel_order=order)
    b = vessel_bootstrap_stats(v + 1.0, ids, n_boot=300, seed=9, vessel_order=order)
    # same resamples: shifting every value by 1 shifts every bound by exactly 1
    for k in ("mean", "median"):
        assert np.allclose(np.array(b[k]) - np.array(a[k]), 1.0)


def test_weighted_median_matches_numpy():
    from maritime_forecast.evaluation.bootstrap import _weighted_median

    rng = np.random.default_rng(5)
    for _ in range(200):
        v = np.sort(rng.normal(size=rng.integers(1, 9)))
        w = rng.integers(0, 4, len(v))
        if w.sum() == 0:
            continue
        assert _weighted_median(v, w) == pytest.approx(np.median(np.repeat(v, w)))


def test_single_vessel_ci_contains_estimate():
    s = vessel_bootstrap_stats([3.0, 6.0], ["a", "a"], n_boot=50)
    for est, lo, hi in s.values():
        assert lo <= est <= hi


def test_paired_bootstrap_shares_resamples_and_matches_single_model():
    from maritime_forecast.evaluation.bootstrap import paired_vessel_bootstrap

    v, ids = _data(4)
    out = paired_vessel_bootstrap({"a": v, "b": v + 2.0}, ids, n_boot=300, seed=1)
    assert np.allclose(out["b"]["median_boots"] - out["a"]["median_boots"], 2.0)
    single = vessel_bootstrap_stats(v, ids, n_boot=300, seed=1)
    # same estimator, same seed -> identical
    assert out["a"]["median"] == pytest.approx(single["median"])
    assert out["a"]["mean"] == pytest.approx(single["mean"])
