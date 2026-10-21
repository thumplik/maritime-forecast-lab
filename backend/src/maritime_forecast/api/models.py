"""Response models (OpenAPI source for the generated TypeScript client)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


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
