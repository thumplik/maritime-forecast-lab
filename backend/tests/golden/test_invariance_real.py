"""Leakage invariance on REAL NOAA cutoffs (needs local data; runs via `make calibrate`).

Cutoffs come from the latest coverage-audit sample: underway, stationary and transiting,
spread across vessel classes. Every report after each cutoff, for every vessel in the scoped
partitions, is deleted / shifted / spiked / flooded; no registered model may change.
"""

import pytest

from maritime_forecast.config import PROCESSED_DIR, RUNS_DIR
from maritime_forecast.evaluation.invariance import check_invariance, registry_factories
from maritime_forecast.storage.parquet_store import ParquetStore

pytestmark = pytest.mark.golden
DATASET = "noaa_us_gulf_2023-06"


def _cases(n_per_group: int = 2):
    import polars as pl

    runs = sorted(RUNS_DIR.glob("coverage_audit_*/cutoffs.parquet"))
    if not runs or not (PROCESSED_DIR / "canonical" / DATASET).exists():
        pytest.skip("needs local NOAA data and a coverage audit run")
    c = pl.read_parquet(runs[-1])
    groups = [
        c.filter(pl.col("motion_set") == "underway"),
        c.filter(pl.col("motion_set") == "stationary"),
        c.filter(pl.col("transiting")) if "transiting" in c.columns else c.head(0),
    ]
    picked = pl.concat([g.sample(min(n_per_group, g.height), seed=7) for g in groups if g.height])
    return [(r["vessel_id"], r["observed_at"]) for r in picked.iter_rows(named=True)]


def test_registered_models_invariant_on_real_cutoffs(tmp_path):
    cases = _cases()
    from maritime_forecast.models.damped_velocity import DampedVelocityFit, fit_path

    fp = fit_path("P-known", DATASET)
    if not fp.exists():
        pytest.skip("needs the P-known damped-velocity fit (make fit PROTOCOL=P-known)")
    from maritime_forecast.config import MANIFEST_DIR

    params = {
        "damped_velocity_v1": {
            "taus_hours": DampedVelocityFit.model_validate_json(fp.read_text()).taus_hours
        },
        # Retrieval builds its pool from the (perturbed) store. A rolling pool over the last
        # day keeps the per-case build small while exercising the same code path.
        "route_retrieval_v1": {
            "manifest": (MANIFEST_DIR / "splits" / f"P-known__{DATASET}.json").read_text(),
            "config": {"pool": "rolling", "index_days": 1},
        },
    }
    rep = check_invariance(registry_factories(params), ParquetStore(), DATASET, cases,
                           history_hours=24, workdir=tmp_path, wide_days=2)  # fmt: skip
    assert rep.cases == len(cases) >= 4
    assert rep.ok, rep.violations
    assert rep.vacuous == [], rep.vacuous  # every perturbation changed each target's future
    assert set(rep.ok_by_model) == set(registry_factories(params))
    for model_id in rep.ok_by_model:
        assert rep.ok_fraction(model_id) >= 0.8, (model_id, rep.ok_fraction(model_id))
