"""Fit damped-velocity tau per vessel class on a protocol's TRAIN split (build plan v0.3 §5).

Everything the fit reads goes through the same machinery a benchmark uses, pointed at the
train split: cutoffs whose whole window lies in the train split (window_splits), causal
histories, truth from the truth region. Test and calibration data are never read for fitting;
tests/unit/test_fit_damped.py proves it by perturbing them and requiring an identical fit.

Objective per class: mean over horizons of the median geodesic error, over a fixed tau grid.
Classes with fewer than `min_per_class` training cutoffs use the pooled 'default' tau.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.evaluation.runner import BenchmarkConfig, select_cutoffs, truth_grid
from maritime_forecast.geo.geodesic import direct, distance_m
from maritime_forecast.models.base import histories_for_cutoffs
from maritime_forecast.models.constant_velocity import select_anchor
from maritime_forecast.models.damped_velocity import (
    TAU_GRID_H,
    DampedVelocityFit,
    damped_distance_m,
    fit_path,
    history_vessel_class,
)
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore


@dataclass(frozen=True)
class FitConfig:
    horizons_h: tuple[float, ...] = (1, 2, 3, 6, 12, 24)
    step_minutes: int = 60
    history_hours: float = 24
    seed: int = 20230601
    max_per_class: int = 3000
    min_per_class: int = 300
    sampler: SamplerConfig = field(default_factory=lambda: SamplerConfig(motion_set="underway"))


DEFAULT_FIT = FitConfig()


def _tau_key(t: float | None) -> str:
    return "inf" if t is None else f"{t:g}"


def fit_damped_velocity(
    store: ParquetStore,
    dataset_id: str,
    manifest_path: Path,
    cfg: FitConfig = DEFAULT_FIT,
    data_provenance: str = "",
) -> DampedVelocityFit:
    manifest_path = Path(manifest_path)
    manifest = SplitManifest.model_validate_json(manifest_path.read_text())
    bcfg = BenchmarkConfig(
        dataset_id=dataset_id,
        manifest_path=manifest_path,
        split="train",
        horizons_h=cfg.horizons_h,
        step_minutes=cfg.step_minutes,
        history_hours=cfg.history_hours,
        seed=cfg.seed,
        samplers={"underway": replace(cfg.sampler, seed=cfg.seed)},
    )
    cutoffs = select_cutoffs(bcfg, store, manifest)
    if cutoffs.height == 0:
        raise ValueError("no training cutoffs in the train split; nothing to fit tau on")
    # Cap each class (seeded) so frequent classes don't dominate fitting time.
    cutoffs = (
        pl.concat(
            [
                g.sample(min(g.height, cfg.max_per_class), seed=cfg.seed)
                for _, g in cutoffs.sort("vessel_class", "vessel_id", "observed_at").group_by(
                    "vessel_class", maintain_order=True
                )
            ]
        )
        .sort("vessel_id", "observed_at")
        .drop("cutoff_id")
        .with_row_index("cutoff_id")
    )

    n = cutoffs.height
    lat0, lon0, sog, cog, offset = (np.full(n, np.nan) for _ in range(5))
    hist_class = np.full(n, "unknown", dtype=object)
    for cid, h in histories_for_cutoffs(store, dataset_id, cutoffs, cfg.history_hours):
        hist_class[cid] = history_vessel_class(h)
        a, _ = select_anchor(h)
        if a is None:
            continue
        lat0[cid], lon0[cid], sog[cid] = a["latitude_deg"], a["longitude_deg"], a["sog_knots"]
        cog[cid] = a["cog_deg"] if a["cog_deg"] is not None else 0.0
        offset[cid] = (h.as_of - a["observed_at"]).total_seconds()

    grid_steps = round(max(cfg.horizons_h) * 60 / cfg.step_minutes)
    train_end = max(p.end for p in manifest.periods if p.split == "train")
    truth = truth_grid(bcfg, store, cutoffs, grid_steps, truth_until=train_end)
    steps = [round(h * 60 / cfg.step_minutes) - 1 for h in cfg.horizons_h]
    t_lat, t_lon = truth["lat"][:, steps], truth["lon"][:, steps]
    ok = truth["evaluable"][:, steps] & np.isfinite(lat0)[:, None]
    lead = np.asarray(cfg.horizons_h)[None, :] * 3600.0 + offset[:, None]
    classes = hist_class.astype(str)  # same rule the model uses at prediction time

    errors = {}
    for tau in TAU_GRID_H:
        d = damped_distance_m(sog[:, None], lead, tau)
        la, lo = direct(lat0[:, None], lon0[:, None], cog[:, None], np.nan_to_num(d))
        errors[_tau_key(tau)] = np.where(ok, distance_m(la, lo, t_lat, t_lon), np.nan)

    def objective(mask: np.ndarray) -> dict[str, float]:
        out = {}
        for k, e in errors.items():
            meds = [
                np.nanmedian(e[mask, j]) if np.isfinite(e[mask, j]).any() else np.nan
                for j in range(len(cfg.horizons_h))
            ]
            out[k] = float(np.nanmean(meds)) if np.isfinite(meds).any() else float("nan")
        return out

    def best(obj: dict[str, float]) -> float | None:
        k = min((k for k in obj if np.isfinite(obj[k])), key=lambda k: obj[k])
        return None if k == "inf" else float(k)

    usable = ok.any(axis=1)
    objective_table = {"default": objective(usable)}
    taus = {"default": best(objective_table["default"])}
    counts = {"default": int(usable.sum())}
    for c in sorted(set(classes)):
        mask = usable & (classes == c)
        counts[c] = int(mask.sum())
        if counts[c] >= cfg.min_per_class:
            objective_table[c] = objective(mask)
            taus[c] = best(objective_table[c])

    lowest = _tau_key(TAU_GRID_H[0])
    boundary = sorted(c for c, t in taus.items() if _tau_key(t) == lowest)
    return DampedVelocityFit(
        at_grid_boundary=boundary,
        protocol=manifest.protocol,
        dataset_id=dataset_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        split="train",
        data_until=train_end,
        taus_hours=taus,
        n_cutoffs=counts,
        objective=objective_table,
        config={
            "horizons_h": list(cfg.horizons_h),
            "step_minutes": cfg.step_minutes,
            "history_hours": cfg.history_hours,
            "seed": cfg.seed,
            "max_per_class": cfg.max_per_class,
            "min_per_class": cfg.min_per_class,
            "sampler": cfg.sampler.as_dict(),
            "tau_grid_h": [_tau_key(t) for t in TAU_GRID_H],
        },
        data_provenance=data_provenance or dataset_id,
        git_sha=git_sha(),
        created_at=datetime.now(UTC).replace(microsecond=0),
    )


def save_fit(fit: DampedVelocityFit, path: Path | None = None) -> Path:
    path = path or fit_path(fit.protocol, fit.dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fit.model_dump_json(indent=2) + "\n")
    return path
