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
