"""Forecast request/response and evaluation contracts (build plan §4 "Forecast request and
response", §5 contract, §6). Endpoints arrive in Milestone 2; the shapes are fixed here so the
frontend types and every model adapter target one contract.

Rules encoded in the types:
  * a forecast response never carries withheld truth or errors (that is ForecastEvaluation);
  * candidate-route scores are `similarity`, never probabilities, unless calibrated;
  * the only uncertainty representation is a calibrated region with its nominal level and
    calibration-set id — never per-axis marginals presented as a corridor.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MAX_TARGET_STEPS = 1024


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    vessel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    as_of: AwareDatetime = Field(description="Cutoff: only reports at or before this are used")
    history_hours: float = Field(24, gt=0, le=168)
    horizon_hours: float = Field(6, gt=0, le=72)
    step_minutes: int = Field(10, ge=1, le=60)
    model_ids: list[str] = Field(min_length=1, max_length=10)
    show_ground_truth: bool = Field(
        False, description="Request a ForecastEvaluation as well; truth is never in this response"
    )

    @model_validator(mode="after")
    def _grid(self) -> ForecastRequest:
        steps = self.horizon_hours * 60 / self.step_minutes
        if not math.isclose(steps, round(steps), abs_tol=1e-9):
            raise ValueError("horizon_hours must be a whole number of step_minutes")
        if steps > MAX_TARGET_STEPS:
            raise ValueError(f"at most {MAX_TARGET_STEPS} target steps")
        return self


class TrackPoint(BaseModel):
    timestamp: AwareDatetime
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)


class CandidateTrajectory(BaseModel):
    """One candidate path (e.g. a retrieved route). `similarity` is NOT a probability."""

    trajectory_id: str
    points: list[TrackPoint]
    similarity: float | None = Field(
        None, description="Model-specific similarity; not a probability unless calibrated"
    )
    reference_segment_ids: list[str] = Field(
        default_factory=list, description="Provenance: historical segments this path used"
    )


class ConformalRegion(BaseModel):
    """Calibrated displacement radius per target time (build plan §5 U1)."""

    kind: Literal["conformal_radius"] = "conformal_radius"
    nominal_level: float = Field(gt=0, lt=1)
    calibration_set_id: str
    radii_m: list[float] = Field(description="One radius per target time, metres")

    @model_validator(mode="after")
    def _nonneg(self) -> ConformalRegion:
        if any(r < 0 for r in self.radii_m):
            raise ValueError("radii must be non-negative")
        return self


class InputCoverage(BaseModel):
    n_reports: int
    first_report: AwareDatetime | None
    last_report: AwareDatetime | None
    last_report_age_s: float | None = Field(description="as_of minus the last report time")
    n_gaps: int


class ModelError(BaseModel):
    code: str
    message: str


class ModelForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    model_version: str = Field(description="Pinned version / checkpoint revision")
    config: dict = Field(default_factory=dict)
    status: Literal["ok", "unsupported", "failed"]
    error: ModelError | None = None
    target_times: list[AwareDatetime]
    points: list[TrackPoint] = Field(description="Central prediction; empty unless status=ok")
    trajectories: list[CandidateTrajectory] = Field(default_factory=list)
    uncertainty: ConformalRegion | None = None
    runtime_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ModelForecast:
        if any(b <= a for a, b in zip(self.target_times, self.target_times[1:], strict=False)):
            raise ValueError("target_times must be strictly increasing")
        if self.status == "ok":
            if self.error is not None:
                raise ValueError("status ok must not carry an error")
            if not self.target_times:
                raise ValueError("status ok needs at least one target time")
            if [p.timestamp for p in self.points] != self.target_times:
                raise ValueError("points must match target_times exactly")
            for tr in self.trajectories:
                if [p.timestamp for p in tr.points] != self.target_times:
                    raise ValueError("every candidate trajectory must match target_times")
            if self.uncertainty and len(self.uncertainty.radii_m) != len(self.target_times):
                raise ValueError("one uncertainty radius per target time")
        elif self.error is None or self.points or self.trajectories or self.uncertainty:
            raise ValueError(
                "a non-ok forecast has an error and no points, trajectories or uncertainty"
            )
        return self


class ForecastResponse(BaseModel):
    """Predictions only. Withheld truth and errors live in ForecastEvaluation."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str
    dataset_id: str
    vessel_id: str
    as_of: AwareDatetime
    source: str
    data_version: str = Field(description="Dataset manifest identity used for the cache key")
    input_coverage: InputCoverage
    forecasts: list[ModelForecast]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _causal(self) -> ForecastResponse:
        last = self.input_coverage.last_report
        if last is not None and last > self.as_of:
            raise ValueError("input includes a report after as_of")
        for f in self.forecasts:
            if any(t <= self.as_of for t in f.target_times):
                raise ValueError("target times must be after as_of")
        return self


NotEvaluableReason = Literal[
    "no_report_before", "no_report_after", "truth_gap", "insufficient_history", "model_failed",
    "outside_truth_region", "feed_outage",
]  # fmt: skip


class HorizonScore(BaseModel):
    horizon_minutes: float
    evaluable: bool
    reason: NotEvaluableReason | None = None
    error_m: float | None = None
    along_track_m: float | None = None
    cross_track_m: float | None = None

    @model_validator(mode="after")
    def _evaluable(self) -> HorizonScore:
        if self.evaluable != (self.reason is None and self.error_m is not None):
            raise ValueError("evaluable iff an error is present and no not-evaluable reason")
        if not self.evaluable and any(
            x is not None for x in (self.error_m, self.along_track_m, self.cross_track_m)
        ):
            raise ValueError("a not-evaluable score carries no error values")
        return self


class ModelEvaluation(BaseModel):
    model_id: str
    scores: list[HorizonScore]


class ForecastEvaluation(BaseModel):
    """Truth and errors for one forecast; returned separately and only on request."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str
    truth_region: str
    max_bracket_gap_minutes: float
    truth: list[TrackPoint | None] = Field(description="Per target time; null if not evaluable")
    models: list[ModelEvaluation]

    @model_validator(mode="after")
    def _complete(self) -> ForecastEvaluation:
        if not self.truth:
            raise ValueError("an evaluation covers at least one target time")
        for m in self.models:
            if len(m.scores) != len(self.truth):
                raise ValueError(f"{m.model_id}: one score per target time (no silent drops)")
            for sc, tr in zip(m.scores, self.truth, strict=True):
                if sc.evaluable != (tr is not None):
                    raise ValueError(f"{m.model_id}: evaluable only where truth exists")
        return self
