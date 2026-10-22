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
    predict_horizon_hours: float | None = Field(
        None, gt=0, le=72,
        description="Ask the models for this longer horizon and serve its prefix (replaying a "
        "benchmark run, which predicts once at its longest horizon). Default: the calibration "
        "run's when the request matches its step and history, else horizon_hours",
    )  # fmt: skip
    hex_sets: bool = Field(
        True, description="Attach calibrated hex prediction sets to route_retrieval_v2 forecasts"
    )
    conformal_level: Literal[0.8, 0.9, 0.95] | None = Field(
        0.9, description="Attach split-conformal radii at this nominal level (null: none)"
    )
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
        if self.predict_horizon_hours is not None:
            p = self.predict_horizon_hours * 60 / self.step_minutes
            if self.predict_horizon_hours < self.horizon_hours:
                raise ValueError("predict_horizon_hours must be at least horizon_hours")
            if not math.isclose(p, round(p), abs_tol=1e-9) or p > MAX_TARGET_STEPS:
                raise ValueError(
                    f"predict_horizon_hours must be whole steps, at most {MAX_TARGET_STEPS}"
                )
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
    reference_ends_at: AwareDatetime | None = Field(
        None,
        description="When the reference runs out (its segment ended). Later points hold the "
        "last referenced position; null if the reference covers every target time",
    )


class ConformalRegion(BaseModel):
    """Split-conformal displacement radius per target time (build plan §5 U1). Marginal coverage
    claim: over forecasts exchangeable with the stratum's calibration samples, at least
    `nominal_level` of true positions fall within `radii_m[i]` of the forecast point. It is not a
    probability for this particular ship, and it assumes exchangeability (measured empirical
    coverage on the held-out test is in docs/benchmarks.md). Radii exist only at fitted
    horizons; other target times are null (never interpolated)."""

    kind: Literal["conformal_radius"] = "conformal_radius"
    nominal_level: float = Field(gt=0, lt=1)
    calibration_set_id: str = Field(description="The calibration benchmark run the radii came from")
    radii_m: list[float | None] = Field(
        description="One per target time, metres; null where no radius was fitted"
    )
    strata: list[str | None] = Field(
        default_factory=list, description="Stratum each radius came from (after any fallback)"
    )
    fitted_horizons_h: list[float] = Field(default_factory=list)
    calibration_n: list[int | None] = Field(
        default_factory=list, description="Calibration errors behind each radius"
    )
    calibration_vessels: list[int | None] = Field(
        default_factory=list, description="Distinct vessels behind each radius"
    )
    class_source: str = Field(
        "backfilled", description="Vessel class used for the stratum: NOAA back-filled, not as-of"
    )

    @model_validator(mode="after")
    def _nonneg(self) -> ConformalRegion:
        if any(r is not None and r < 0 for r in self.radii_m):
            raise ValueError("radii must be non-negative")
        if self.strata and len(self.strata) != len(self.radii_m):
            raise ValueError("one stratum per radius")
        return self


class InputCoverage(BaseModel):
    n_reports: int
    first_report: AwareDatetime | None
    last_report: AwareDatetime | None
    last_report_age_s: float | None = Field(description="as_of minus the last report time")
    n_gaps: int
    motion_state: Literal["underway", "stationary"] | None = Field(
        None, description="The cutoff sampler's motion rule on the history (None: neither)"
    )
    vessel_class: str | None = Field(
        None, description="Class on the last report with one (NOAA back-filled, not as-of)"
    )


class ModelError(BaseModel):
    code: str
    message: str


class HexLayer(BaseModel):
    """One nested band of a hex set: the highest-density cells holding `level` of the
    probability (split-conformal threshold at that level, same calibration window)."""

    level: float
    n_cells: int
    area_km2: float
    boundary: dict | None = None


class HexSet(BaseModel):
    """A calibrated hex prediction set at one target time (build plan v0.4 §5 H): the H3 cells
    holding the true position with the nominal probability. Split-conformal on the calibration
    window: a MARGINAL coverage claim over the calibration population (underway vessels in the
    cutoff region, per stratum and horizon), not a per-forecast probability."""

    kind: Literal["h1_hex_set"] = "h1_hex_set"
    nominal_level: float
    calibration_set_id: str
    resolution: int = Field(description="H3 resolution of every cell")
    cells: list[str]
    area_km2: float
    stratum: str = Field(description="Calibration stratum (vessel class is NOAA back-filled)")
    calibration_n: int
    calibration_vessels: int
    n_candidates: int = Field(description="Retrieved continuations behind the density")
    boundary: dict | None = Field(None, description="GeoJSON MultiPolygon outline of the cells")
    layers: list[HexLayer] = Field(
        default_factory=list,
        description="Nested bands (e.g. 50% inside 80% inside 90%): where the vessel is most "
        "likely to be. Empty when only the main level is fitted",
    )
    adoption: Literal["adopted", "not_adopted", "pending"] = Field(
        "pending",
        description="The pre-registered held-out comparison against the conformal disc: "
        "pending until it has run; a not-adopted set is shown as experimental",
    )


class ModelForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    model_version: str = Field(description="Pinned version / checkpoint revision")
    predicted_horizon_hours: float | None = Field(
        None,
        description="Set when the model was asked for a longer horizon than the request and "
        "this is the prefix of that forecast: the benchmark runner predicts once at its longest "
        "horizon, so served forecasts match the calibrated ones (some models depend on it)",
    )
    config: dict = Field(default_factory=dict)
    status: Literal["ok", "unsupported", "failed"]
    error: ModelError | None = None
    target_times: list[AwareDatetime]
    points: list[TrackPoint] = Field(description="Central prediction; empty unless status=ok")
    trajectories: list[CandidateTrajectory] = Field(default_factory=list)
    central_trajectory_id: str | None = Field(
        None, description="The candidate trajectory `points` were taken from, if any"
    )
    uncertainty: ConformalRegion | None = None
    hex_sets: list[HexSet | None] = Field(
        default_factory=list,
        description="Per target time: the calibrated hex set, or null where none was fitted",
    )
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
            if self.central_trajectory_id is not None and self.central_trajectory_id not in {
                t.trajectory_id for t in self.trajectories
            }:
                raise ValueError("central_trajectory_id must name one of the trajectories")
            if self.uncertainty and len(self.uncertainty.radii_m) != len(self.target_times):
                raise ValueError("one uncertainty radius per target time")
        elif (
            self.error is None
            or self.points
            or self.trajectories
            or self.uncertainty
            or self.central_trajectory_id
        ):
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


class PathFeasibility(BaseModel):
    """Per target time, for the move from the previous point (the last report, for the
    first): evaluation.feasibility. Land flags are null when no navigable mask is loaded."""

    land_point: list[bool] | None = None
    land_crossing: list[bool] | None = None
    implausible_speed: list[bool]
    implausible_accel: list[bool]


class ModelEvaluation(BaseModel):
    model_id: str
    status: Literal["ok", "unsupported", "failed"]
    scores: list[HorizonScore]
    ade_m: float | None = Field(
        None,
        description="Mean error over the PAIRED target times (comparable across models); "
        "null if there are none",
    )
    feasibility: PathFeasibility | None = Field(None, description="Null unless status is ok")


class ForecastEvaluation(BaseModel):
    """Truth and errors for one forecast; returned separately and only on request.

    Every target time is reported for every model (no silent drops). A target time without
    truth is not evaluable for any model, with the truth reason; where truth exists but a model
    produced no forecast, that model's score is `model_failed`. `paired` marks the target times
    where every model is evaluable, the set to compare models on."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: str
    dataset_id: str
    vessel_id: str
    as_of: AwareDatetime
    data_version: str
    truth_region: str
    max_bracket_gap_minutes: float
    target_times: list[AwareDatetime]
    truth: list[TrackPoint | None] = Field(description="Per target time; null if not evaluable")
    truth_reasons: list[NotEvaluableReason | None] = Field(
        description="Per target time: why truth is missing; null where truth exists"
    )
    paired: list[bool] = Field(description="Per target time: every model is evaluable")
    models: list[ModelEvaluation]
    feasibility_mask: str | None = Field(
        None, description="Navigable mask the land flags come from; null if none was loaded"
    )
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> ForecastEvaluation:
        n = len(self.target_times)
        if not n:
            raise ValueError("an evaluation covers at least one target time")
        if any(t <= self.as_of for t in self.target_times):
            raise ValueError("target times must be after as_of")
        if not (len(self.truth) == len(self.truth_reasons) == len(self.paired) == n):
            raise ValueError("truth, truth_reasons and paired have one entry per target time")
        for tr, why, t in zip(self.truth, self.truth_reasons, self.target_times, strict=True):
            if (tr is None) == (why is None):
                raise ValueError("a target time has either truth or a not-evaluable reason")
            if tr is not None and tr.timestamp != t:
                raise ValueError("truth timestamps must match target_times")
        for m in self.models:
            if len(m.scores) != n:
                raise ValueError(f"{m.model_id}: one score per target time (no silent drops)")
            for sc, tr, why in zip(m.scores, self.truth, self.truth_reasons, strict=True):
                if tr is None and (sc.evaluable or sc.reason != why):
                    raise ValueError(f"{m.model_id}: no truth means the truth's reason")
                if tr is not None and not sc.evaluable and sc.reason != "model_failed":
                    raise ValueError(f"{m.model_id}: truth exists, so only model_failed applies")
        want = [all(m.scores[i].evaluable for m in self.models) and bool(self.models)
                for i in range(n)]  # fmt: skip
        if self.paired != want:
            raise ValueError("paired must mark exactly the target times every model scored")
        return self
