"""Cutoff sampler (build plan §6.3; parameters documented in docs/cutoff-sampling.md)."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import polars as pl

SAMPLER_VERSION = "cutoff-sampler-v1"


@dataclass(frozen=True)
class SamplerConfig:
    motion_set: str = "underway"  # "underway" | "stationary"
    underway_min_sog_kn: float = 2.0
    stationary_max_sog_kn: float = 0.5
    min_history_reports: int = 6  # within the trailing 3 h
    min_spacing_hours: float = 3.0
    max_per_vessel_day: int = 2
    seed: int = 20230601

    def as_dict(self) -> dict:
        return asdict(self) | {"version": SAMPLER_VERSION}


TRANSITING_CLASSES = ("cargo", "tanker", "passenger")


def transiting_expr(min_mean_sog_kn: float = 2.0, min_sog_kn: float = 0.5) -> pl.Expr:
    """Build plan v0.3 §6.3 transiting stratum: large-ship classes underway for the last 30 min.

    Needs columns vessel_class, sog_mean_30m, sog_min_30m (trailing windows, no look-ahead).
    """
    return (
        pl.col("vessel_class").is_in(TRANSITING_CLASSES)
        & (pl.col("sog_mean_30m") >= min_mean_sog_kn)
        & (pl.col("sog_min_30m") >= min_sog_kn)
    ).fill_null(False)


def eligible(candidates: pl.DataFrame, cfg: SamplerConfig) -> pl.DataFrame:
    """Filter candidate rows (already restricted to the cutoff region) by motion set and history.

    Requires columns: vessel_id, observed_at, sog_mean_10m, n_reports_3h.
    """
    if cfg.motion_set == "underway":
        motion = pl.col("sog_mean_10m") >= cfg.underway_min_sog_kn
    elif cfg.motion_set == "stationary":
        motion = pl.col("sog_mean_10m") < cfg.stationary_max_sog_kn
    else:
        raise ValueError(f"unknown motion_set {cfg.motion_set!r}")
    return candidates.filter(motion & (pl.col("n_reports_3h") >= cfg.min_history_reports))


def sample_cutoffs(candidates: pl.DataFrame, cfg: SamplerConfig) -> pl.DataFrame:
    """Pick cutoffs: per vessel per UTC day, random order, greedy with spacing and daily cap.

    Spacing is enforced across the whole vessel history (not just within a day), so cutoffs
    on either side of midnight are still >= min_spacing apart.
    """
    elig = eligible(candidates, cfg)
    if elig.height == 0:
        return elig
    rng = np.random.default_rng(cfg.seed)
    elig = (
        elig.sort("vessel_id", "observed_at")
        .with_columns(
            pl.col("observed_at").dt.date().alias("_day"),
            pl.Series("_r", rng.random(elig.height)),
        )
        .sort("vessel_id", "_day", "_r")
    )
    spacing_us = int(cfg.min_spacing_hours * 3600 * 1e6)
    vid = elig["vessel_id"].to_list()
    day = elig["_day"].to_list()
    t_us = elig["observed_at"].dt.epoch("us").to_numpy()

    keep = np.zeros(elig.height, dtype=bool)
    accepted: dict[str, list[int]] = {}
    per_day: dict[tuple[str, object], int] = {}
    for i in range(elig.height):
        key = (vid[i], day[i])
        if per_day.get(key, 0) >= cfg.max_per_vessel_day:
            continue
        times = accepted.setdefault(vid[i], [])
        if any(abs(t_us[i] - t) < spacing_us for t in times):
            continue
        keep[i] = True
        times.append(t_us[i])
        per_day[key] = per_day.get(key, 0) + 1
    return elig.filter(pl.Series(keep)).drop("_day", "_r").sort("vessel_id", "observed_at")
