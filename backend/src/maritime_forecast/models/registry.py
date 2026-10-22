"""Model registry: model_id -> factory(ModelContext). GET /models and benchmarks read this.

Factories receive the ModelContext of a run: store, dataset, protocol and manifest hash. A
model that needs data beyond the target's history (an index, fitted parameters) must take it
from that context, never by opening the archive itself; the invariance harness blocks the real
archive while it runs, and fitted parameters are only accepted for the matching manifest.
"""

from __future__ import annotations

from collections.abc import Callable

from maritime_forecast.models.base import ModelCapabilities, ModelContext, TrajectoryForecaster
from maritime_forecast.models.constant_turn import ConstantTurnRate
from maritime_forecast.models.constant_velocity import ConstantVelocity, ConstantVelocityPaper
from maritime_forecast.models.damped_velocity import DampedVelocity
from maritime_forecast.models.persistence import Persistence
from maritime_forecast.models.route_retrieval import V2, RouteRetrieval
from maritime_forecast.models.timestral_adapter import TimestralPos, TimestralVel

Factory = Callable[[ModelContext], TrajectoryForecaster]

_FACTORIES: dict[str, Factory] = {
    "persistence_v1": lambda ctx: Persistence(),
    "constant_velocity_v1": lambda ctx: ConstantVelocity(),
    "constant_velocity_paper_v1": lambda ctx: ConstantVelocityPaper(),
    "constant_turn_rate_v1": lambda ctx: ConstantTurnRate(),
    "damped_velocity_v1": DampedVelocity.from_context,
    "route_retrieval_v1": RouteRetrieval.from_context,
    "route_retrieval_v2": lambda ctx: RouteRetrieval.from_context(ctx, V2),
    "timestral_pos_v1": TimestralPos.from_context,
    "timestral_vel_v1": TimestralVel.from_context,
    # Fixed-context T1-pos variants for the history-length study (one paired set).
    "timestral_pos_c24_v1": lambda ctx: TimestralPos.from_context(ctx, "timestral_pos_c24_v1", 24),
    "timestral_pos_c48_v1": lambda ctx: TimestralPos.from_context(ctx, "timestral_pos_c48_v1", 48),
}


class UnknownModel(KeyError):
    pass


def model_ids() -> list[str]:
    return sorted(_FACTORIES)


def get_model(model_id: str, ctx: ModelContext | None = None) -> TrajectoryForecaster:
    try:
        factory = _FACTORIES[model_id]
    except KeyError:
        raise UnknownModel(f"unknown model {model_id!r}; available: {model_ids()}") from None
    return factory(ctx or ModelContext())


def capabilities() -> list[ModelCapabilities]:
    return [get_model(m).capabilities() for m in model_ids()]


def product_model_ids() -> list[str]:
    """The default benchmark model set: everything except calibration-only reproductions and
    models behind an optional dependency (those are benchmarked by naming them)."""
    return [c.model_id for c in capabilities() if not c.calibration_only and not c.optional]
