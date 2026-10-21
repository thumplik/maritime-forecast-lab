"""B0 persistence (build plan §5): stay at the last reported position."""

from __future__ import annotations

from dataclasses import dataclass, field

from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
)

STALE_WARNING_MINUTES = 30


@dataclass
class Persistence(BaseForecaster):
    model_id: str = "persistence_v1"
    config: dict = field(default_factory=lambda: {"stale_warning_minutes": STALE_WARNING_MINUTES})

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            model_version="1.0.0",
            min_history_reports=1,
            description="Last reported position held for every target time.",
            reads_beyond_own_history=False,
            uses_store_at_predict=False,
        )

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        last = history.last()
        n = len(horizon.target_times)
        age_min = (history.as_of - last["observed_at"]).total_seconds() / 60
        warnings = []
        if age_min > self.config["stale_warning_minutes"]:
            warnings.append(f"last report is {age_min:.0f} min older than as_of")
        return [last["latitude_deg"]] * n, [last["longitude_deg"]] * n, warnings
