"""Model registry: model_id -> factory(store, dataset_id). GET /models and benchmarks read this.

Factories always receive the store and dataset a run uses. A model that needs data beyond the
target's history (an index, fitted parameters) must build it from that store, never by
opening the archive itself; the invariance harness blocks the real archive while it runs.
"""

from __future__ import annotations

from collections.abc import Callable

from maritime_forecast.models.base import ModelCapabilities, TrajectoryForecaster
from maritime_forecast.models.constant_velocity import ConstantVelocity, ConstantVelocityPaper
from maritime_forecast.models.persistence import Persistence
from maritime_forecast.storage.parquet_store import ParquetStore

Factory = Callable[[ParquetStore | None, str | None], TrajectoryForecaster]

# Baselines use only AISHistory, so they ignore the store and dataset.
_FACTORIES: dict[str, Factory] = {
    "persistence_v1": lambda store, ds: Persistence(),
    "constant_velocity_v1": lambda store, ds: ConstantVelocity(),
    "constant_velocity_paper_v1": lambda store, ds: ConstantVelocityPaper(),
}


class UnknownModel(KeyError):
    pass


def model_ids() -> list[str]:
    return sorted(_FACTORIES)


def get_model(
    model_id: str, store: ParquetStore | None = None, dataset_id: str | None = None
) -> TrajectoryForecaster:
    try:
        factory = _FACTORIES[model_id]
    except KeyError:
        raise UnknownModel(f"unknown model {model_id!r}; available: {model_ids()}") from None
    return factory(store, dataset_id)


def capabilities() -> list[ModelCapabilities]:
    return [get_model(m).capabilities() for m in model_ids()]
