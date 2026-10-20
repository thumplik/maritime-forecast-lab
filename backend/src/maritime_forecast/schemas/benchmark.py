"""Benchmark run record (build plan §6.1 step 6, §6.5, AGENTS.md rule 5): an immutable
description of what was run, on what, and the paired results."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from maritime_forecast.schemas.manifest import Protocol

# A cell can be headline only on an admitted horizon and with at most this failure rate for
# the model (e.g. CV fails where COG is missing). The rate is always reported alongside.
HEADLINE_MAX_FAILURE_RATE = 0.01


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
    n_evaluable: int = Field(description="Cutoffs in the cell with evaluable truth")
    n_paired: int = Field(description="Evaluable cutoffs where every model delivered")
    n_vessels: int
    sample_set_sha256: str = Field(
        description="SHA-256 of the sorted 'vessel_id|as_of|horizon' keys scored in this cell; "
        "identical for every model in the cell and comparable across runs"
    )
    n_failed: int = Field(
        ge=0, description="Samples this model failed; excluded for every model in the cell"
    )
    failure_rate: float = Field(ge=0, le=1, description="n_failed / (n_paired + n_failed)")
    headline: bool = Field(
        description="Eligible for headline tables: admitted horizon and failure_rate <= "
        f"{HEADLINE_MAX_FAILURE_RATE}"
    )
    median_error_m: Stat
    mean_error_m: Stat
    p90_error_m: float
    median_abs_along_track_m: float | None = None
    median_abs_cross_track_m: float | None = None
    n_decomposed: int = Field(
        0,
        description="Paired samples with a defined true course (along/cross medians use these; "
        "samples where the vessel is stationary at the target have no along/cross split)",
    )
    median_ade_m: float | None = Field(
        None, description="Median of per-sample ADE over paired grid points up to the horizon"
    )
    best_baseline_id: str | None = Field(
        None, description="Baseline with the lowest median error in this cell"
    )
    skill_vs_best_baseline: Stat | None = Field(
        None,
        description="1 - median error / best baseline's median error, with a PAIRED bootstrap CI "
        "(same vessel resamples). Null unless every registered baseline is in the run, or when "
        "the best baseline's error is ~0 (undefined)",
    )

    @model_validator(mode="after")
    def _headline(self) -> CellResult:
        total = self.n_paired + self.n_failed
        if total and abs(self.failure_rate - self.n_failed / total) > 1e-9:
            raise ValueError("failure_rate must equal n_failed / (n_paired + n_failed)")
        if self.headline and (
            self.admission != "first_class" or self.failure_rate > HEADLINE_MAX_FAILURE_RATE
        ):
            raise ValueError(
                f"headline cells need an admitted horizon and failure_rate <= "
                f"{HEADLINE_MAX_FAILURE_RATE}"
            )
        return self


class DroppedCell(BaseModel):
    """A (stratum, horizon) that had cutoffs but too few evaluable samples to score."""

    stratum: str
    horizon_minutes: float
    n_cutoffs: int
    n_evaluable: int
    n_paired: int
    n_failed: dict[str, int] = Field(description="model_id -> failures among evaluable samples")


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
    dropped_cells: list[DroppedCell] = Field(
        default_factory=list, description="Never silent: every unscored (stratum, horizon)"
    )
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
            # Every cell carries every declared model (no silent per-model drop), and every
            # declared horizon was scored somewhere. Rare strata may lack long horizons.
            missing = [
                (m, k) for k, rs in cells.items() for m in declared
                if m not in {r.model_id for r in rs}
            ]  # fmt: skip
            if missing:
                raise ValueError(
                    f"a succeeded run covers every model in every cell; missing {missing[:5]}"
                )
            unscored = (
                set(self.horizons_minutes)
                - {h for h, _ in cells}
                - {d.horizon_minutes for d in self.dropped_cells}
            )
            if unscored:
                raise ValueError(
                    f"a succeeded run covers every declared horizon (scored or recorded as "
                    f"dropped); {unscored}"
                )
        return self
