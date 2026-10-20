"""Forecaster contract (build plan §5 "Contract").

Every model, from persistence to Timestral, implements `TrajectoryForecaster`:

    model_id: str
    capabilities() -> ModelCapabilities
    predict(history: AISHistory, horizon: ForecastHorizon) -> ModelForecast

`AISHistory` is the only input a model receives about the target vessel. It refuses rows
after `as_of` at construction, so a model cannot see the future even by accident; build it
with `history_from_store`, which reads through `ParquetStore.history()`. Anything else a
model reads (a retrieval index, neighbours) must be declared in its capabilities and is
checked by the leakage-invariance harness (evaluation/invariance.py).

Unsupported requests and failures are returned as typed results, never fake predictions.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

import polars as pl
from pydantic import BaseModel, Field

from maritime_forecast.schemas.forecast import MAX_TARGET_STEPS, ModelForecast
from maritime_forecast.storage.parquet_store import ParquetStore

REQUIRED_COLUMNS = (
    "vessel_id",
    "observed_at",
    "latitude_deg",
    "longitude_deg",
    "sog_knots",
    "cog_deg",
)


class LeakageError(ValueError):
    """Raised when a model input contains information from after the cutoff."""


@dataclass(frozen=True)
class AISHistory:
    """One vessel's reports with observed_at <= as_of, time-ordered (oldest first)."""

    vessel_id: str
    as_of: datetime
    history_hours: float
    reports: pl.DataFrame
    dataset_id: str | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() != timedelta(0):
            raise ValueError("as_of must be timezone-aware UTC")
        missing = [c for c in REQUIRED_COLUMNS if c not in self.reports.columns]
        if missing:
            raise ValueError(f"history lacks columns {missing}")
        df = self.reports
        if df.height:
            if df["observed_at"].max() > self.as_of:
                raise LeakageError("history contains reports after as_of")
            if df["vessel_id"].n_unique() != 1 or df["vessel_id"][0] != self.vessel_id:
                raise ValueError("history must hold exactly the target vessel's reports")
            if not df["observed_at"].is_sorted():
                raise ValueError("history must be time-ordered")

    @property
    def n(self) -> int:
        return self.reports.height

    def last(self) -> dict | None:
        return self.reports.row(-1, named=True) if self.n else None


def history_from_store(
    store: ParquetStore, dataset_id: str, vessel_id: str, as_of: datetime, hours: float
) -> AISHistory:
    """The causal context for a forecast: ParquetStore.history() wrapped in AISHistory."""
    df = store.history(dataset_id, vessel_id, as_of, hours).sort("observed_at", maintain_order=True)
    return AISHistory(vessel_id, as_of, hours, df, dataset_id)


@dataclass(frozen=True)
class ForecastHorizon:
    """Target grid: as_of + k * step for k = 1..horizon/step."""

    as_of: datetime
    horizon_hours: float
    step_minutes: int = 10

    def __post_init__(self) -> None:
        n = self.horizon_hours * 60 / self.step_minutes
        if (
            self.step_minutes < 1
            or abs(n - round(n)) > 1e-9
            or not 1 <= round(n) <= MAX_TARGET_STEPS
        ):
            raise ValueError("horizon must be 1..1024 whole steps of step_minutes")

    @property
    def target_times(self) -> list[datetime]:
        n = round(self.horizon_hours * 60 / self.step_minutes)
        return [self.as_of + timedelta(minutes=self.step_minutes * k) for k in range(1, n + 1)]


class ModelCapabilities(BaseModel):
    model_id: str
    model_version: str
    description: str
    min_history_reports: int = Field(1, ge=0)
    max_horizon_hours: float = 72.0
    step_minutes: tuple[int, int] = (1, 60)
    needs_sog_cog: bool = False
    supports_missing_inputs: bool = True
    uncertainty: bool = False
    reads_beyond_own_history: bool = Field(
        description="True if the model uses data other than the target's own history (retrieval "
        "index, neighbours, fitted parameters). Required: no default, so it cannot be forgotten."
    )
    installed: bool = True


@runtime_checkable
class TrajectoryForecaster(Protocol):
    model_id: str

    def capabilities(self) -> ModelCapabilities: ...

    def predict(self, history: AISHistory, horizon: ForecastHorizon) -> ModelForecast: ...


@dataclass
class BaseForecaster:
    """Shared plumbing: request checks against capabilities, timing, typed failures.
    Subclasses implement `_predict` returning (lats, lons, warnings)."""

    model_id: str = ""
    config: dict = field(default_factory=dict)

    def capabilities(self) -> ModelCapabilities:  # pragma: no cover - abstract
        raise NotImplementedError

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):  # pragma: no cover
        raise NotImplementedError

    def _result(self, horizon: ForecastHorizon, **kw) -> ModelForecast:
        caps = self.capabilities()
        return ModelForecast(
            model_id=self.model_id,
            model_version=caps.model_version,
            config=self.config,
            target_times=horizon.target_times,
            **kw,
        )

    def unsupported(self, horizon: ForecastHorizon, code: str, message: str) -> ModelForecast:
        return self._result(
            horizon, status="unsupported", points=[], error={"code": code, "message": message}
        )

    def predict(self, history: AISHistory, horizon: ForecastHorizon) -> ModelForecast:
        caps = self.capabilities()
        if history.as_of != horizon.as_of:
            raise ValueError("history and horizon must share as_of")
        if horizon.horizon_hours > caps.max_horizon_hours:
            return self.unsupported(
                horizon, "horizon_too_long", f"max horizon is {caps.max_horizon_hours} h"
            )
        lo, hi = caps.step_minutes
        if not lo <= horizon.step_minutes <= hi:
            return self.unsupported(horizon, "unsupported_step", f"step must be {lo}-{hi} min")
        if history.n < caps.min_history_reports:
            return self.unsupported(
                horizon,
                "insufficient_history",
                f"needs >= {caps.min_history_reports} reports, got {history.n}",
            )
        t0 = time.perf_counter()
        try:
            out = self._predict(history, horizon)
        except Exception as e:  # noqa: BLE001 - every failure is a recorded, typed result
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={"code": "exception", "message": f"{type(e).__name__}: {e}"},
            )
        if isinstance(out, ModelForecast):  # adapter-declared failure
            return out
        lats, lons, warnings = out
        values = [float(v) for v in (*lats, *lons)]
        if len(lats) != len(horizon.target_times) or not all(math.isfinite(v) for v in values):
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={
                    "code": "non_finite_output",
                    "message": "prediction is not finite or off the grid",
                },
            )
        if not all(-90 <= la <= 90 for la in lats):
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={"code": "invalid_output", "message": "latitude out of range"},
            )
        points = [
            {
                "timestamp": t,
                "latitude_deg": float(la),
                "longitude_deg": ((float(lo) + 180.0) % 360.0) - 180.0,
            }
            for t, la, lo in zip(horizon.target_times, lats, lons, strict=True)
        ]
        return self._result(
            horizon,
            status="ok",
            points=points,
            warnings=warnings,
            runtime_ms=(time.perf_counter() - t0) * 1000,
        )
