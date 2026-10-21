"""Protocol-leakage tests for the damped-velocity tau fit, on the SYNTHETIC fixture.

The fit may read only the train split. Any perturbation of data after the train period
(calibration, test, and the purge between) or of test-vessel data must leave the fit identical;
the same perturbation of train data must change it (so the tests are not vacuous). The hardest
manifest is used: calibration and test start immediately after train, with no purge gap.

The fixture is thinned to one report every 7 minutes so targets fall BETWEEN reports, and the
train period ends while vessels are still reporting: some train targets are bracketed by a
report after the train end. Without truth_grid's cap at the split end, these tests fail
(verified by removing the cap).
"""

from datetime import UTC, datetime, timedelta

import pytest
from fixtures.synthetic import CSV_PATH, MOVING, TURNING

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.models.fit_damped import FitConfig, fit_damped_velocity
from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, tzinfo=UTC)
TRAIN_END = T0 + timedelta(minutes=105)  # MOVING/TURNING report until 02:00
CAL_END = TRAIN_END + timedelta(minutes=15)
CFG = FitConfig(horizons_h=(0.25, 0.5), step_minutes=15, history_hours=0.5, min_per_class=1,
                sampler=SamplerConfig(motion_set="underway", min_spacing_hours=0.1,
                                      max_per_vessel_day=50))  # fmt: skip
# No purge gap: train truth must still never bracket with a calibration/test report.
PERIODS = [Period(split="train", start=T0, end=TRAIN_END),
           Period(split="calibration", start=TRAIN_END, end=CAL_END),
           Period(split="test", start=CAL_END, end=T0 + timedelta(hours=6))]  # fmt: skip


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    s = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "full", store=s)
    src = (
        "read_parquet(["
        + ", ".join(f"'{f}'" for f in s.files("full"))
        + "], hive_partitioning=false)"
    )
    s.write("syn", f"SELECT * FROM {src} WHERE minute(observed_at) % 7 = 0")  # sparse reports
    return s


def _manifest(tmp_path, **kw):
    m = SplitManifest(dataset_id="syn", description="SYNTHETIC", periods=PERIODS, **kw)
    p = tmp_path / f"{m.protocol}.json"
    p.write_text(m.model_dump_json())
    return p


def _src(store):
    return (
        "read_parquet(["
        + ", ".join(f"'{f}'" for f in store.files("syn"))
        + "], hive_partitioning=false)"
    )


def _shift(store, name, where):
    store.write(
        name,
        f"""SELECT * REPLACE (
        CASE WHEN {where} THEN latitude_deg + 0.3 ELSE latitude_deg END AS latitude_deg,
        CASE WHEN {where} THEN 0.0 ELSE sog_knots END AS sog_knots) FROM {_src(store)}""",
    )
    return name


def _shift_positions(store, name, where):
    """Move positions only (SOG kept), so cutoffs stay eligible but their truth changes."""
    store.write(
        name,
        f"""SELECT * REPLACE (
        CASE WHEN {where} THEN latitude_deg + 0.3 ELSE latitude_deg END AS latitude_deg)
        FROM {_src(store)}""",
    )
    return name


def _delete(store, name, where):
    store.write(name, f"SELECT * FROM {_src(store)} WHERE NOT ({where})")
    return name


def _flood(store, name, where):
    store.write(
        name,
        f"""SELECT * FROM {_src(store)} UNION ALL
        SELECT * REPLACE (observed_at + INTERVAL 7 SECOND AS observed_at,
                          latitude_deg - 0.2 AS latitude_deg, 0.0 AS sog_knots)
        FROM {_src(store)} WHERE {where}""",
    )
    return name


def _key(fit):
    return fit.taus_hours, fit.n_cutoffs, fit.objective


def _t(t):
    return f"TIMESTAMPTZ '{t.isoformat()}'"


@pytest.mark.parametrize("perturb", [_shift, _delete, _flood])
def test_p_known_fit_ignores_everything_after_train(store, tmp_path, perturb):
    mp = _manifest(tmp_path, protocol="P-known")
    base = fit_damped_velocity(store, "syn", mp, CFG)
    assert base.split == "train" and base.n_cutoffs["default"] > 3
    assert base.data_until == TRAIN_END
    after = perturb(store, f"after_{perturb.__name__}", f"observed_at >= {_t(TRAIN_END)}")
    assert _key(fit_damped_velocity(store, after, mp, CFG)) == _key(base)


def test_p_known_fit_reacts_to_train_data(store, tmp_path):
    mp = _manifest(tmp_path, protocol="P-known")
    base = fit_damped_velocity(store, "syn", mp, CFG)
    lo, hi = _t(TRAIN_END - timedelta(hours=1)), _t(TRAIN_END)
    train = _shift(store, "train_shift", f"observed_at >= {lo} AND observed_at < {hi}")
    assert _key(fit_damped_velocity(store, train, mp, CFG)) != _key(base)


def test_p_unseen_fit_ignores_test_vessels_and_reacts_to_train_vessels(store, tmp_path):
    va = next(
        v for v in (VesselAssignment(salt=f"s{i}", buckets={"train": (0, 500), "test": (500, 1000)})
                    for i in range(100))
        if v.assign(TURNING) == "test" and v.assign(MOVING) == "train"
    )  # fmt: skip
    test_vessel, train_vessel = TURNING, MOVING
    mp = _manifest(tmp_path, protocol="P-unseen", vessel_assignment=va.model_dump())
    base = fit_damped_velocity(store, "syn", mp, CFG)
    for perturb in (_shift, _delete, _flood):
        other = perturb(store, f"tv_{perturb.__name__}", f"vessel_id = '{test_vessel}'")
        assert _key(fit_damped_velocity(store, other, mp, CFG)) == _key(base)
    # non-vacuous: the test vessel would have contributed under P-known (it has train-period
    # candidates), and perturbing the train vessel changes the fit
    known = fit_damped_velocity(store, "syn", _manifest(tmp_path, protocol="P-known"), CFG)
    assert known.n_cutoffs["default"] > base.n_cutoffs["default"]
    tr = _shift_positions(
        store, "trv", f"vessel_id = '{train_vessel}' AND observed_at < {_t(TRAIN_END)}"
    )
    assert _key(fit_damped_velocity(store, tr, mp, CFG)) != _key(base)


def test_fit_records_provenance(store, tmp_path):
    import hashlib

    mp = _manifest(tmp_path, protocol="P-known")
    fit = fit_damped_velocity(store, "syn", mp, CFG)
    assert fit.manifest_sha256 == hashlib.sha256(mp.read_bytes()).hexdigest()
    assert "default" in fit.taus_hours and fit.config["tau_grid_h"][-1] == "inf"


def test_benchmark_refuses_the_fit_split(store, tmp_path, monkeypatch):
    import maritime_forecast.models.damped_velocity as dv
    from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
    from maritime_forecast.models.fit_damped import save_fit
    from maritime_forecast.storage.run_store import RunStore

    monkeypatch.setattr(dv, "FIT_DIR", tmp_path / "fits")
    mp = _manifest(tmp_path, protocol="P-known")
    save_fit(fit_damped_velocity(store, "syn", mp, CFG), dv.fit_path("P-known", "syn"))
    cfg = BenchmarkConfig(dataset_id="syn", manifest_path=mp, split="train",
                          model_ids=("damped_velocity_v1",), horizons_h=(0.25,), history_hours=0.5)  # fmt: skip
    with pytest.raises(ValueError, match="in-sample"):
        run_benchmark(cfg, store, RunStore(tmp_path / "runs"), run_id="insample")
