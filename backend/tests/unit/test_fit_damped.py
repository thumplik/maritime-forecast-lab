"""Protocol-leakage tests for the damped-velocity tau fit, on the SYNTHETIC fixture.

The fit may read only the train split. Perturbing test-period data (P-known) or test-vessel
data (P-unseen) must leave the fit identical; perturbing train data must change it (so the
test is not vacuous).
"""

from datetime import UTC, datetime, timedelta

import pytest
from fixtures.synthetic import CSV_PATH, GAPS, MOVING

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.models.fit_damped import FitConfig, fit_damped_velocity
from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, tzinfo=UTC)
TRAIN_END = T0 + timedelta(hours=2, minutes=30)
TEST_START = T0 + timedelta(hours=3)
CFG = FitConfig(horizons_h=(0.25, 0.5), step_minutes=15, history_hours=0.5, min_per_class=1,
                sampler=SamplerConfig(motion_set="underway", min_spacing_hours=0.1,
                                      max_per_vessel_day=50))  # fmt: skip
PERIODS = [Period(split="train", start=T0, end=TRAIN_END),
           Period(split="test", start=TEST_START, end=T0 + timedelta(hours=6))]  # fmt: skip


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    s = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=s)
    return s


def _manifest(tmp_path, **kw):
    m = SplitManifest(dataset_id="syn", description="SYNTHETIC", periods=PERIODS, **kw)
    p = tmp_path / f"{m.protocol}.json"
    p.write_text(m.model_dump_json())
    return p


def _perturbed(store, name, where):
    src = (
        "read_parquet(["
        + ", ".join(f"'{f}'" for f in store.files("syn"))
        + "], hive_partitioning=false)"
    )
    store.write(
        name,
        f"""SELECT * REPLACE (
        CASE WHEN {where} THEN latitude_deg + 0.3 ELSE latitude_deg END AS latitude_deg,
        CASE WHEN {where} THEN 0.0 ELSE sog_knots END AS sog_knots) FROM {src}""",
    )
    return name


def _key(fit):
    return fit.taus_hours, fit.n_cutoffs, fit.objective


def test_p_known_fit_ignores_test_period(store, tmp_path):
    mp = _manifest(tmp_path, protocol="P-known")
    base = fit_damped_velocity(store, "syn", mp, CFG)
    assert base.split == "train" and base.n_cutoffs["default"] > 3
    t = f"TIMESTAMPTZ '{TEST_START.isoformat()}'"
    other = _perturbed(store, "syn_test_perturbed", f"observed_at >= {t}")
    assert _key(fit_damped_velocity(store, other, mp, CFG)) == _key(base)
    # non-vacuous: the same perturbation on train data changes the fit
    t_end = f"TIMESTAMPTZ '{(TRAIN_END - timedelta(hours=1)).isoformat()}'"
    train = _perturbed(
        store, "syn_train_perturbed", f"observed_at >= {t_end} AND observed_at < {t}"
    )
    assert _key(fit_damped_velocity(store, train, mp, CFG)) != _key(base)


def test_p_unseen_fit_ignores_test_vessels(store, tmp_path):
    # GAPS is a test vessel, MOVING a train vessel (buckets chosen from their hashes)
    va = next(
        v for v in (VesselAssignment(salt=f"s{i}", buckets={"train": (0, 500), "test": (500, 1000)})
                    for i in range(100))
        if v.assign(GAPS) != v.assign(MOVING)
    )  # fmt: skip
    assert va.assign(GAPS) != va.assign(MOVING)
    test_vessel = GAPS if va.assign(GAPS) == "test" else MOVING
    mp = _manifest(tmp_path, protocol="P-unseen", vessel_assignment=va.model_dump())
    base = fit_damped_velocity(store, "syn", mp, CFG)
    other = _perturbed(store, "syn_tv", f"vessel_id = '{test_vessel}'")
    assert _key(fit_damped_velocity(store, other, mp, CFG)) == _key(base)


def test_fit_records_provenance(store, tmp_path):
    import hashlib

    mp = _manifest(tmp_path, protocol="P-known")
    fit = fit_damped_velocity(store, "syn", mp, CFG)
    assert fit.manifest_sha256 == hashlib.sha256(mp.read_bytes()).hexdigest()
    assert "default" in fit.taus_hours and fit.config["tau_grid_h"][-1] == "inf"
