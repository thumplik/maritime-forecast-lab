"""STAND-IN for TimestralForecaster in unit tests (the real model needs the optional extra and
weights). Same call shape: forecast(context (B, V, T), prediction_length, **kw) -> object with
`.median` (B, V, n). It extrapolates each variate linearly from its last two observed values,
plus a negligible term (< 1 mm) that depends on EVERY context value and on which are missing,
so the invariance harness sees any change to the context. It records its keyword arguments."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class _Forecast:
    median: np.ndarray


@dataclass
class LinearBackend:
    calls: list[dict] = field(default_factory=list)

    def forecast(self, context, prediction_length: int, **kw):
        ctx = np.asarray(context, dtype=float)
        self.calls.append({"shape": ctx.shape, **kw})
        b, v, _ = ctx.shape
        out = np.zeros((b, v, prediction_length))
        k = np.arange(1, prediction_length + 1)
        for i in range(b):
            for j in range(v):
                idx = np.flatnonzero(np.isfinite(ctx[i, j]))
                a, z = idx[-2], idx[-1]
                slope = (ctx[i, j, z] - ctx[i, j, a]) / (z - a)
                end = ctx.shape[-1] - 1
                whole = 1e-12 * float(np.nansum(ctx[i, j] * np.arange(1, ctx.shape[-1] + 1)))
                whole += 1e-9 * float(np.isfinite(ctx[i, j]).sum())
                out[i, j] = ctx[i, j, z] + slope * (end - z + k) + whole
        return _Forecast(out)
