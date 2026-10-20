"""Benchmark run record (build plan §6.1 step 6, §6.5, AGENTS.md rule 5): an immutable
description of what was run, on what, and the paired results."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from maritime_forecast.schemas.manifest import Protocol


class ProtocolRef(BaseModel):
    protocol: Protocol
    manifest_version: str
    dataset_id: str
    manifest_sha256: str = Field(description="SHA-256 of the split manifest file used")


class ModelRef(BaseModel):
    model_id: str
    model_version: str
    config: dict = Field(default_factory=dict)


class Stat(BaseModel):
    """Point estimate with a paired vessel-bootstrap 95% CI."""

    estimate: float
    ci_low: float
    ci_high: float


class CellResult(BaseModel):
    """One model x horizon x stratum cell, scored on the paired evaluable set."""

    model_id: str
    horizon_minutes: float
    stratum: str
    admission: Literal["first_class", "experimental"]
    n_paired: int
    n_vessels: int
    sample_set_sha256: str = Field(
        description="SHA-256 of the sorted (cutoff_id, horizon) keys scored in this cell; "
        "identical for every model in the cell (paired scoring)"
    )
    n_failed: int = Field(
        ge=0, description="Samples this model failed; excluded for every model in the cell"
    )
    failure_rate: float = Field(ge=0, le=1, description="n_failed / (n_paired + n_failed)")
    headline: bool = Field(
        description="Eligible for headline tables: admitted horizon and no model failures"
    )
    median_error_m: Stat
    mean_error_m: Stat
    p90_error_m: float
    median_abs_along_track_m: float | None = None
    median_abs_cross_track_m: float | None = None
    skill_vs_best_baseline: float | None = Field(
        None, description="1 - error/best-of(persistence, CV, damped velocity) median error"
    )

    @model_validator(mode="after")
    def _headline(self) -> CellResult:
        total = self.n_paired + self.n_failed
        if total and abs(self.failure_rate - self.n_failed / total) > 1e-9:
            raise ValueError("failure_rate must equal n_failed / (n_paired + n_failed)")
        if self.headline and (self.admission != "first_class" or self.n_failed > 0):
            raise ValueError("headline cells need an admitted horizon and zero failures")
        return self


class BenchmarkRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: AwareDatetime
    status: Literal["queued", "running", "succeeded", "failed"]
    git_sha: str
    dataset_id: str
    data_provenance: str = Field(description="Source and mapping id, e.g. noaa_marinecadastre@v1")
    protocol: ProtocolRef
    sampler: dict = Field(description="Sampler config incl. seed and version")
    models: list[ModelRef] = Field(min_length=1)
    horizons_minutes: list[float] = Field(min_length=1)
    operational: Literal[True] = Field(
        True, description="Truth-selected numbers (best-of-N) are never part of a run record"
    )
    not_evaluable: dict[str, dict[str, int]] = Field(
        default_factory=dict, description="horizon minutes (as string) -> reason -> count"
    )
    results: list[CellResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def _paired(self) -> BenchmarkRun:
        if self.status == "failed" and not self.error:
            raise ValueError("a failed run records its error")
        declared = {m.model_id for m in self.models}
        unknown = {r.model_id for r in self.results} - declared
        if unknown:
            raise ValueError(f"results for undeclared models {sorted(unknown)}")
        # Paired scoring: within a horizon x stratum cell every model saw the same samples.
        cells: dict[tuple[float, str], list[CellResult]] = {}
        for r in self.results:
            cells.setdefault((r.horizon_minutes, r.stratum), []).append(r)
        for key, rs in cells.items():
            shared = {(r.n_paired, r.n_vessels, r.sample_set_sha256, r.admission) for r in rs}
            if len(shared) > 1:
                raise ValueError(f"models scored on different sample sets in cell {key}")
            ids = [r.model_id for r in rs]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate model rows in cell {key}")
        if self.status == "succeeded":
            strata = {s for _, s in cells} or {"(none)"}
            missing = [
                (m, h, s) for m in declared for h in self.horizons_minutes for s in strata
                if (h, s) not in cells or m not in {r.model_id for r in cells[(h, s)]}
            ]  # fmt: skip
            if missing:
                raise ValueError(
                    f"a succeeded run covers every model x horizon x stratum; missing {missing[:5]}"
                )
        return self
