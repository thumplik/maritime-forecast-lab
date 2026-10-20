"""Source column mappings: how a provider's AIS columns map onto the canonical schema (§4).

A mapping is declarative (YAML in config/sources/) and versioned. `canonical.canonical_sql`
turns a mapping plus a source relation into canonical rows. Adding a provider means writing a
mapping, not code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maritime_forecast.config import CONFIG_DIR
from maritime_forecast.schemas.observation import StaticProvenance, VesselClass

SOURCES_DIR = CONFIG_DIR / "sources"

REQUIRED_FIELDS = ("vessel_id", "observed_at", "latitude_deg", "longitude_deg")
MAPPABLE_FIELDS = (
    *REQUIRED_FIELDS,
    "sog",
    "cog_deg",
    "heading_deg",
    "nav_status",
    "vessel_type",
    "length_m",
    "width_m",
    "draft_m",
    "transceiver_class",
    "destination",
    "eta",
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_+\-/:.%TZ ]*$")


class TimestampSpec(BaseModel):
    """How to parse observed_at (and eta).

    * `format=None, epoch_unit=None`: ISO-like strings or typed timestamps. A value with an
      explicit offset (`Z`, `+02`, `-05:00`) is honoured; a naive value is read in `timezone`.
    * `format`: strptime format for naive strings, read in `timezone`.
    * `epoch_unit`: numeric seconds or milliseconds since the Unix epoch (always UTC).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    timezone: str = "UTC"
    format: str | None = None
    epoch_unit: Literal["s", "ms"] | None = None

    @field_validator("timezone", "format")
    @classmethod
    def _safe(cls, v: str | None) -> str | None:
        if v is not None and not _SAFE_TOKEN.match(v):
            raise ValueError(f"unsupported characters in {v!r}")
        return v


class SourceMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(pattern=r"^[a-z0-9_]+$")
    version: int = Field(ge=1)
    description: str = ""
    file_format: Literal["csv", "parquet"] = "csv"
    csv_delimiter: str = Field(default=",", min_length=1, max_length=1)
    columns: dict[str, str] = Field(description="canonical field -> source column name")
    timestamp: TimestampSpec = TimestampSpec()
    eta_timestamp: TimestampSpec | None = None
    sog_unit: Literal["knots", "mps", "kmh"] = "knots"
    # AIS: SOG 102.3 = not available, 102.2 = 102.2 kn or more. Both are unusable as speeds.
    sog_invalid_at_or_above: float = 102.2  # in sog_unit
    null_values: dict[str, list[str]] = Field(
        default_factory=lambda: {"nav_status": ["15"], "vessel_type": ["0"]},
        description="Per-field string values that mean 'not available'",
    )
    drop_values: dict[Literal["transceiver_class", "nav_status", "vessel_type"], list[str]] = Field(
        default_factory=dict,
        description="Rows whose field equals one of these values (after null_values and "
        "value_maps) are dropped and counted as 'excluded_by_mapping', e.g. base stations",
    )
    value_maps: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="Per-field source value -> canonical value"
    )
    vessel_class_from: Literal["ais_code", "text_map"] = "ais_code"
    vessel_class_text_map: dict[str, VesselClass] = Field(default_factory=dict)
    static_fields_provenance: StaticProvenance = "unknown"
    verified: bool = Field(
        default=False, description="True once checked against a real file from the provider"
    )

    @model_validator(mode="after")
    def _check(self) -> SourceMapping:
        missing = [f for f in REQUIRED_FIELDS if f not in self.columns]
        if missing:
            raise ValueError(f"mapping lacks required fields: {missing}")
        unknown = set(self.columns) - set(MAPPABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown canonical fields: {sorted(unknown)}")
        if self.vessel_class_from == "text_map" and not self.vessel_class_text_map:
            raise ValueError("vessel_class_from=text_map needs vessel_class_text_map")
        return self

    @property
    def mapping_id(self) -> str:
        return f"{self.source_id}@v{self.version}"


def load_mapping(name_or_path: str | Path) -> SourceMapping:
    """Load a mapping by preset name (config/sources/<name>.yaml) or by file path."""
    p = Path(name_or_path)
    if not p.suffix:
        if not re.fullmatch(r"[a-z0-9_]+", str(name_or_path)):
            raise ValueError(f"invalid mapping name {name_or_path!r}")
        p = SOURCES_DIR / f"{name_or_path}.yaml"
    with open(p) as f:
        return SourceMapping.model_validate(yaml.safe_load(f))
