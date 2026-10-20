"""Dataset manifest: provenance of a canonical dataset in the Parquet store (§4, §6.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IngestReport(BaseModel):
    """What happened to every input row. rows_in = sum(dropped) + duplicates + outside + out."""

    model_config = ConfigDict(extra="forbid")
    rows_in: int = 0
    dropped: dict[str, int] = Field(default_factory=dict)  # reason -> rows
    duplicates_removed: int = 0
    outside_region: int = 0
    rows_out: int = 0
    flags: dict[str, int] = Field(default_factory=dict)  # quality flag -> rows carrying it

    def __add__(self, other: IngestReport) -> IngestReport:
        def merge(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
            return {k: a.get(k, 0) + b.get(k, 0) for k in sorted(set(a) | set(b))}

        return IngestReport(
            rows_in=self.rows_in + other.rows_in,
            dropped=merge(self.dropped, other.dropped),
            duplicates_removed=self.duplicates_removed + other.duplicates_removed,
            outside_region=self.outside_region + other.outside_region,
            rows_out=self.rows_out + other.rows_out,
            flags=merge(self.flags, other.flags),
        )


class InputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    bytes: int
    sha256: str | None = None
    report: IngestReport
    ingested_at: datetime


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str
    source_id: str
    mapping_id: str
    canonical_version: str
    ingest_version: str
    region: dict | None = None
    static_fields_provenance: str
    inputs: list[InputFile] = Field(default_factory=list)
    partitions: dict[str, int] = Field(default_factory=dict)  # UTC day -> rows
    notes: str = ""
    git_sha: str | None = None
    updated_at: datetime | None = None

    @property
    def report(self) -> IngestReport:
        total = IngestReport()
        for f in self.inputs:
            total = total + f.report
        return total

    @property
    def rows(self) -> int:
        return sum(self.partitions.values())
