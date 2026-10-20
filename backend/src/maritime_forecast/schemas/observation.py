"""Canonical AIS observation (build plan §4; mapping rules in docs/data-contracts.md)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StaticProvenance = Literal["broadcast", "backfilled", "unknown"]
VesselClass = Literal[
    "fishing", "tug_tow", "passenger", "cargo", "tanker", "pleasure_sail", "service", "hsc_wig",
    "other", "unknown",
]  # fmt: skip


class Observation(BaseModel):
    """One AIS position report. Field order and names match the canonical Parquet columns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vessel_id: str = Field(min_length=1, description="Normalized identity, typically MMSI")
    observed_at: datetime = Field(description="Timezone-aware UTC")
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)
    sog_knots: float | None = Field(default=None, ge=0)
    cog_deg: float | None = Field(default=None, ge=0, lt=360)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    nav_status: str | None = None
    vessel_type: str | None = None
    vessel_class: VesselClass = "unknown"
    length_m: float | None = Field(default=None, gt=0)
    width_m: float | None = Field(default=None, gt=0)
    draft_m: float | None = Field(default=None, gt=0)
    transceiver_class: Literal["A", "B"] | None = None
    destination: str | None = None
    eta: datetime | None = None
    source: str
    static_fields_provenance: StaticProvenance
    quality_flags: list[str] = Field(default_factory=list)

    @field_validator("observed_at", "eta")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("timestamps must be timezone-aware UTC")
        return v
