"""Cluster (by-vessel) bootstrap confidence intervals (build plan §6.5)."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from numpy.typing import ArrayLike


def vessel_bootstrap_ci(
    values: ArrayLike,
    vessel_ids: ArrayLike,
    stat: Callable[[np.ndarray], float] = np.mean,
    n_boot: int = 1000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Point estimate and percentile CI, resampling vessels (clusters) with replacement."""
    v = np.asarray(values, float)
    ids = np.asarray(vessel_ids)
    ok = ~np.isnan(v)
    v, ids = v[ok], ids[ok]
    if v.size == 0:
        return (np.nan, np.nan, np.nan)
    uniq, inv = np.unique(ids, return_inverse=True)
    groups = [v[inv == g] for g in range(uniq.size)]
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, uniq.size, uniq.size)
        boots[b] = stat(np.concatenate([groups[i] for i in pick]))
    lo, hi = np.quantile(boots, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(stat(v)), float(lo), float(hi)


def _weighted_median(sorted_vals: np.ndarray, weights: np.ndarray) -> float:
    """np.median of the multiset where sorted_vals[i] appears weights[i] times: the middle
    element, or the mean of the two middle elements when the total count is even."""
    cw = np.cumsum(weights)
    total = int(cw[-1])
    lo = sorted_vals[np.searchsorted(cw, (total + 1) // 2)]  # k-th smallest, 1-based
    hi = sorted_vals[np.searchsorted(cw, total // 2 + 1)]
    return float((lo + hi) / 2) if total % 2 == 0 else float(lo)


def vessel_bootstrap_stats(
    values: ArrayLike,
    vessel_ids: ArrayLike,
    n_boot: int = 1000,
    level: float = 0.95,
    seed: int = 0,
    vessel_order: ArrayLike | None = None,
) -> dict[str, tuple[float, float, float]]:
    """Mean and median with vessel-cluster bootstrap CIs, via multinomial vessel weights.

    Equivalent to resampling vessels with replacement but O(n) per resample. PAIRED: with the
    same seed and the same `vessel_order` (the cell's sorted vessel ids), every model in a cell
    gets identical resamples, so CIs of different models are directly comparable.
    """
    v = np.asarray(values, float)
    ids = np.asarray(vessel_ids)
    order = np.unique(ids) if vessel_order is None else np.asarray(vessel_order)
    pos = {x: i for i, x in enumerate(order)}
    g = np.fromiter((pos[x] for x in ids), dtype=np.int64, count=len(ids))
    k = len(order)
    srt = np.argsort(v, kind="stable")
    v_sorted, g_sorted = v[srt], g[srt]
    sums = np.bincount(g, weights=v, minlength=k)
    counts = np.bincount(g, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    medians = np.empty(n_boot)
    for b in range(n_boot):
        w = rng.multinomial(k, np.full(k, 1.0 / k))  # times each vessel is drawn
        means[b] = (w @ sums) / (w @ counts)
        medians[b] = _weighted_median(v_sorted, w[g_sorted])
    a = (1 - level) / 2
    out = {}
    for name, est, boots in (
        ("mean", float(v.mean()), means),
        ("median", float(np.median(v)), medians),
    ):
        lo, hi = np.quantile(boots, [a, 1 - a])
        out[name] = (est, float(lo), float(hi))
    return out


def paired_vessel_bootstrap(
    values_by_model: dict[str, ArrayLike],
    vessel_ids: ArrayLike,
    n_boot: int = 1000,
    level: float = 0.95,
    seed: int = 0,
) -> dict[str, dict]:
    """Vessel-cluster bootstrap with ONE set of resamples shared by every model.

    Returns per model: mean and median as (estimate, lo, hi), plus `median_boots`, the
    bootstrap distribution of the median, so paired contrasts (e.g. skill against a baseline)
    can be formed resample by resample.
    """
    ids = np.asarray(vessel_ids)
    order = np.unique(ids)
    pos = {x: i for i, x in enumerate(order)}
    g = np.fromiter((pos[x] for x in ids), dtype=np.int64, count=len(ids))
    k = len(order)
    rng = np.random.default_rng(seed)
    weights = rng.multinomial(k, np.full(k, 1.0 / k), size=n_boot)  # shared resamples
    counts = np.bincount(g, minlength=k).astype(float)
    a = (1 - level) / 2
    out: dict[str, dict] = {}
    for m, vals in values_by_model.items():
        v = np.asarray(vals, float)
        srt = np.argsort(v, kind="stable")
        v_sorted, g_sorted = v[srt], g[srt]
        sums = np.bincount(g, weights=v, minlength=k)
        means = (weights @ sums) / (weights @ counts)
        medians = np.array([_weighted_median(v_sorted, w[g_sorted]) for w in weights])
        res: dict = {"median_boots": medians}
        for name, est, boots in (
            ("mean", float(v.mean()), means),
            ("median", float(np.median(v)), medians),
        ):
            lo, hi = np.quantile(boots, [a, 1 - a])
            res[name] = (est, float(lo), float(hi))
        out[m] = res
    return out
