"""Response models (OpenAPI source for the generated TypeScript client)."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maritime_forecast.schemas.benchmark import (
    BenchmarkRun,
    DroppedCell,
    ModelRef,
    ProtocolRef,
    Stat,
)


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    git_sha: str
    data_root_exists: bool
    datasets: int
    models: list[str] = Field(default_factory=list, description="Registered forecasters")


class IngestTotals(BaseModel):
    rows_in: int
    rows_out: int
    dropped: dict[str, int]
    duplicates_removed: int
    flags: dict[str, int]


class Admission(BaseModel):
    """Per-stratum horizon admission (build plan §6.4) from the latest coverage audit."""

    run_id: str
    rule: dict
    strata: dict[str, dict[str, Literal["first_class", "experimental"]]] = Field(
        description="stratum -> horizon hours (as string) -> status"
    )


class DatasetSummary(BaseModel):
    dataset_id: str
    has_manifest: bool
    source_id: str | None = None
    mapping_id: str | None = None
    canonical_version: str | None = None
    static_fields_provenance: str | None = None
    region: dict | None = None
    first_day: date
    last_day: date
    days: int
    rows: int | None = None
    ingest: IngestTotals | None = None
    admission: Admission | None = None


class DatasetList(BaseModel):
    datasets: list[DatasetSummary]


class VesselSummary(BaseModel):
    vessel_id: str
    vessel_class: str | None
    reports: int
    first_seen: datetime
    last_seen: datetime


class VesselPage(BaseModel):
    dataset_id: str
    from_: datetime = Field(alias="from")
    to: datetime
    bbox: list[float] | None = Field(None, description="[lon_min, lat_min, lon_max, lat_max]")
    total: int
    limit: int
    offset: int
    vessels: list[VesselSummary]

    model_config = {"populate_by_name": True}


class Segment(BaseModel):
    segment: int
    start: datetime
    end: datetime
    n_points: int


class Gap(BaseModel):
    start: datetime
    end: datetime
    duration_s: float


class TrackPoints(BaseModel):
    """Columnar points; index i across all lists is one report."""

    observed_at: list[datetime]
    latitude_deg: list[float]
    longitude_deg: list[float]
    sog_knots: list[float | None]
    cog_deg: list[float | None]
    heading_deg: list[float | None]
    nav_status: list[str | None]
    segment: list[int]
    quality_flags: list[list[str]]


class Track(BaseModel):
    dataset_id: str
    vessel_id: str
    from_: datetime = Field(alias="from")
    to: datetime
    gap_minutes: float = Field(description="A gap longer than this starts a new segment")
    n_points: int
    vessel_class: str | None
    static_fields_provenance: str | None
    warnings: list[str]
    segments: list[Segment]
    gaps: list[Gap] = Field(description="Never draw a line across these")
    points: TrackPoints

    model_config = {"populate_by_name": True}


class ModelInfo(BaseModel):
    model_id: str
    model_version: str
    description: str
    installed: bool
    calibration_only: bool
    reads_beyond_own_history: bool
    max_horizon_hours: float
    step_minutes: tuple[int, int]
    min_history_reports: int
    config: dict = Field(default_factory=dict, description="Parameters in use (e.g. fitted tau)")


class ModelList(BaseModel):
    dataset_id: str | None
    parameters_from: str | None = Field(
        None, description="Protocol whose TRAIN-split fit supplies fitted parameters"
    )
    models: list[ModelInfo]


# -- benchmarks ---------------------------------------------------------------------------
class BenchmarkRequest(BaseModel):
    """A paired benchmark (build plan §6) run by the API in a local background process."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    protocol: Literal["P-known", "P-unseen"]
    model_ids: list[str] = Field(
        default_factory=list, max_length=10, description="Empty = every product model"
    )
    horizons_hours: list[Annotated[float, Field(gt=0, le=72)]] = Field(
        default_factory=lambda: [1, 2, 3, 6, 12, 24], min_length=1, max_length=12
    )
    step_minutes: int = Field(10, ge=1, le=60)
    history_hours: float = Field(24, gt=0, le=168)
    seed: int = Field(20230601, ge=0, lt=2**32)
    n_boot: int = Field(1000, ge=100, le=5000)
    max_cutoffs_per_set: int | None = Field(
        None, ge=1, description="Smoke runs only; null = every sampled cutoff"
    )

    @model_validator(mode="after")
    def _grid(self) -> BenchmarkRequest:
        for h in self.horizons_hours:
            steps = h * 60 / self.step_minutes
            if not math.isclose(steps, round(steps), abs_tol=1e-9):
                raise ValueError("every horizon must be a whole number of step_minutes")
        if len(set(self.horizons_hours)) != len(self.horizons_hours):
            raise ValueError("horizons must be distinct")
        return self


class BenchmarkStatus(BaseModel):
    run_id: str
    status: Literal["running", "incomplete", "queued", "succeeded", "failed"]
    message: str | None = None
    run: BenchmarkRun | None = Field(
        None, description="The immutable run record, once finalized (checksums verified)"
    )


class SummaryModelCell(BaseModel):
    median_error_m: Stat
    p90_error_m: float
    n_failed: int = Field(description="Evaluable samples this model failed (excluded for all)")
    failure_rate: float
    skill_vs_best_baseline: Stat | None = None


class SummaryCell(BaseModel):
    """One stratum x horizon cell, every model side by side on the same paired samples."""

    stratum: str
    horizon_minutes: float
    admission: Literal["first_class", "experimental"]
    headline: bool
    n_evaluable: int
    n_paired: int
    n_vessels: int
    sample_set_sha256: str
    best_baseline_id: str | None
    models: dict[str, SummaryModelCell]


class BenchmarkSummary(BaseModel):
    run_id: str
    status: Literal["succeeded", "failed"]
    created_at: datetime
    git_sha: str
    dataset_id: str
    data_provenance: str
    protocol: ProtocolRef
    models: list[ModelRef]
    horizons_minutes: list[float]
    n_cutoffs: int | None
    seed: int | None
    max_cutoffs_per_set: int | None = Field(description="Set only for smoke runs")
    cells: list[SummaryCell]
    not_evaluable: dict[str, dict[str, int]]
    dropped_cells: list[DroppedCell] = Field(description="Unscored cells (same stratum filter)")
    warnings: list[str]
    error: str | None
