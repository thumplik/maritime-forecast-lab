"""B0 damped velocity (build plan v0.3 §5): speed decays from the anchor SOG as
v(t) = SOG * exp(-t / tau), so distance saturates at SOG * tau. tau -> inf is constant velocity;
tau -> 0 is persistence.

tau is fitted per vessel class on the TRAIN split of a protocol only (models/fit_damped.py) and
stored with the manifest hash it was fitted for. The model refuses parameters fitted for a
different manifest, so a benchmark can never use a tau that saw its test data.
Lead time is measured from the anchor report (stale timestamps respected, like
constant_velocity_v1). vessel_class is NOAA back-filled, not as-of (documented).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from maritime_forecast.config import DATA_DIR
from maritime_forecast.geo.geodesic import direct, knots_to_mps
from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
    ModelContext,
)
from maritime_forecast.models.constant_velocity import STALE_WARNING_MINUTES, select_anchor

MODEL_ID = "damped_velocity_v1"
FIT_DIR = DATA_DIR / "model_params" / MODEL_ID
# Candidate decay times (hours); None = no damping (constant velocity).
TAU_GRID_H: tuple[float | None, ...] = (
    0.02,
    0.05,
    0.1,
    0.15,
    0.25,
    0.5,
    1,
    1.5,
    2,
    3,
    4,
    6,
    8,
    12,
    24,
    48,
    None,
)


class DampedVelocityFit(BaseModel):
    """A fitted parameter set: tau per vessel class, with the provenance that makes it safe."""

    model_id: str = MODEL_ID
    fit_version: str = "fit-damped-v2"  # v2: grid down to 0.02 h; boundary flag
    protocol: str
    dataset_id: str
    manifest_sha256: str
    split: str = Field(description="Split the fit used; always 'train'")
    data_until: datetime = Field(
        description="Latest observation time the fit could have read (end of the train period). "
        "Forecasts with as_of before this would use parameters chosen with their own future."
    )
    taus_hours: dict[str, float | None] = Field(
        description="vessel_class -> tau hours (null = no damping); 'default' for other classes"
    )
    n_cutoffs: dict[str, int]
    at_grid_boundary: list[str] = Field(
        default_factory=list,
        description="Classes whose best tau is the smallest grid value (true optimum may be lower)",
    )
    objective: dict[str, dict[str, float]] = Field(
        description="class -> tau (as string) -> mean over horizons of median error (m)"
    )
    config: dict
    data_provenance: str
    git_sha: str
    created_at: datetime


def fit_path(protocol: str, dataset_id: str) -> Path:
    return FIT_DIR / f"{protocol}__{dataset_id}.json"


def history_vessel_class(history: AISHistory) -> str:
    """Vessel class of a history (most frequent non-null value; 'unknown' if none). Shared by
    the model and the fit so both pick tau the same way. NOAA back-fills this field."""
    if "vessel_class" not in history.reports.columns:
        return "unknown"
    c = history.reports["vessel_class"].drop_nulls()
    return str(c.mode().sort()[0]) if c.len() else "unknown"


def damped_distance_m(sog_knots, lead_s, tau_hours: float | None):
    """Distance travelled with exponentially decaying speed; tau None = constant velocity."""
    v = knots_to_mps(sog_knots)
    lead = np.asarray(lead_s, float)
    if tau_hours is None:
        return v * lead
    tau_s = tau_hours * 3600.0
    return v * tau_s * (1.0 - np.exp(-lead / tau_s))


@dataclass
class DampedVelocity(BaseForecaster):
    model_id: str = MODEL_ID
    config: dict = field(default_factory=dict)
    fit: DampedVelocityFit | None = None
    unfitted_reason: str = "no fitted parameters for this context"

    @classmethod
    def from_context(cls, ctx: ModelContext) -> DampedVelocity:
        if ctx.params is not None:  # explicit parameters (tests, harness)
            taus = ctx.params["taus_hours"]
            return cls(
                config={"taus_hours": taus, "fit": "explicit"}, fit=None, unfitted_reason=""
            ).with_taus(taus)
        if ctx.protocol and ctx.dataset_id:
            p = fit_path(ctx.protocol, ctx.dataset_id)
            if not p.exists():
                return cls(
                    unfitted_reason=f"no fit for {ctx.protocol} on {ctx.dataset_id}; "
                    "run scripts/fit_damped_velocity.py"
                )
            fit = DampedVelocityFit.model_validate_json(p.read_text())
            if ctx.manifest_sha256 is None:
                return cls(unfitted_reason="no split manifest to verify the fit against")
            if fit.manifest_sha256 != ctx.manifest_sha256:
                return cls(unfitted_reason="fit was made for a different split manifest")
            return cls(
                config={
                    "taus_hours": fit.taus_hours,
                    "fit_manifest_sha256": fit.manifest_sha256,
                    "fit_protocol": fit.protocol,
                    "fit_split": fit.split,
                    "fit_data_until": fit.data_until.isoformat(),
                },
                fit=fit,
                unfitted_reason="",
            ).with_taus(fit.taus_hours)
        return cls()

    def with_taus(self, taus: dict[str, float | None]) -> DampedVelocity:
        if "default" not in taus:
            raise ValueError("taus_hours needs a 'default' entry")
        for k, v in taus.items():
            if v is not None and not (v > 0 and math.isfinite(v)):
                raise ValueError(f"tau for {k} must be positive or null")
        self._taus = dict(taus)
        return self

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            model_version="1.0.0",
            min_history_reports=1,
            needs_sog_cog=True,
            reads_beyond_own_history=True,  # tau is fitted on other vessels' training data
            uses_store_at_predict=False,  # tau is fixed; the store is never queried
            installed=not self.unfitted_reason,
            description="Speed decays exp(-t/tau) from the anchor SOG along COG; tau per vessel "
            "class fitted on the protocol's train split.",
        )

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        if self.unfitted_reason:
            return self.unsupported(horizon, "not_fitted", self.unfitted_reason)
        if self.fit is not None and history.as_of < self.fit.data_until:
            return self.unsupported(
                horizon,
                "fit_overlaps_as_of",
                f"tau was fitted on data up to {self.fit.data_until.isoformat()}, after as_of; "
                "using it would be hindsight",
            )
        anchor, fell_back = select_anchor(history)
        if anchor is None:
            return self._result(
                horizon,
                status="failed",
                points=[],
                error={
                    "code": "no_usable_sog_cog",
                    "message": "no report in the history has usable SOG/COG",
                },
            )
        cls = history_vessel_class(history)
        tau = self._taus.get(cls, self._taus["default"])
        leads = [(t - anchor["observed_at"]).total_seconds() for t in horizon.target_times]
        dist = damped_distance_m(anchor["sog_knots"], leads, tau)
        cog = anchor["cog_deg"] if anchor["cog_deg"] is not None else 0.0  # sog 0 if cog None
        lat, lon = direct(anchor["latitude_deg"], anchor["longitude_deg"], cog, dist)
        warnings = []
        age_min = (history.as_of - anchor["observed_at"]).total_seconds() / 60
        if fell_back:
            warnings.append("latest report lacks usable SOG/COG; anchored on an earlier report")
        if age_min > STALE_WARNING_MINUTES:
            warnings.append(f"anchor report is {age_min:.0f} min older than as_of")
        return np.atleast_1d(lat).tolist(), np.atleast_1d(lon).tolist(), warnings
