"""Per-stratum horizon admission (build plan v0.3 §6.4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import polars as pl

ADMISSION_VERSION = "admission-v2-per-stratum"


@dataclass(frozen=True)
class AdmissionConfig:
    min_fraction: float = 0.5  # share of the stratum's cutoffs with valid truth
    # Evaluable mean SOG vs the stratum's population mean. Means, not medians: with coverage
    # >= 50% the evaluable set is the majority, so a median comparison barely moves.
    max_mean_sog_drop: float = 0.25
    min_evaluable: int = 100
    min_vessels: int = 20

    def as_dict(self) -> dict:
        return asdict(self) | {"version": ADMISSION_VERSION}


DEFAULT_ADMISSION = AdmissionConfig()


def admit_by_stratum(
    recs: pl.DataFrame,
    stratum: str = "stratum",
    sog_col: str = "sog_mean_10m",
    cfg: AdmissionConfig = DEFAULT_ADMISSION,
) -> pl.DataFrame:
    """One row per (stratum, horizon_h) with the three checks and the resulting status.

    `recs` has one row per (cutoff_id, horizon_h) with columns cutoff_id, horizon_h, vessel_id,
    evaluable, `stratum` and `sog_col` (the cutoff's SOG feature). A cutoff may belong to
    several strata by appearing in several rows with different `stratum` values.
    """
    pop = recs.group_by(stratum).agg(
        n_cutoffs=pl.col("cutoff_id").n_unique(),
        pop_mean_sog=pl.col(sog_col).mean(),
    )
    ev = (
        recs.filter(pl.col("evaluable"))
        .group_by(stratum, "horizon_h")
        .agg(
            n_evaluable=pl.len(),
            n_vessels=pl.col("vessel_id").n_unique(),
            ev_mean_sog=pl.col(sog_col).mean(),
        )
    )
    grid = recs.select(stratum, "horizon_h").unique()
    out = (
        grid.join(pop, on=stratum)
        .join(ev, on=[stratum, "horizon_h"], how="left")
        .with_columns(pl.col("n_evaluable", "n_vessels").fill_null(0))
        .with_columns(frac=pl.col("n_evaluable") / pl.col("n_cutoffs"))
        .with_columns(
            coverage_ok=pl.col("frac") >= cfg.min_fraction,
            speed_ok=(
                pl.col("ev_mean_sog") >= (1 - cfg.max_mean_sog_drop) * pl.col("pop_mean_sog")
            ).fill_null(False),
            size_ok=(pl.col("n_evaluable") >= cfg.min_evaluable)
            & (pl.col("n_vessels") >= cfg.min_vessels),
        )
        .with_columns(
            status=pl.when(pl.col("coverage_ok") & pl.col("speed_ok") & pl.col("size_ok"))
            .then(pl.lit("first_class"))
            .otherwise(pl.lit("experimental"))
        )
        .sort(stratum, "horizon_h")
    )
    return out
