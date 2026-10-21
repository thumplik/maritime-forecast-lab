"""Evaluator runner and run store, end to end on the SYNTHETIC fixture."""

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fixtures.synthetic import CSV_PATH, TURNING

import maritime_forecast.data.ingest as ingest_mod
import maritime_forecast.models.registry as registry
from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
from maritime_forecast.models.base import (
    AISHistory,
    ForecastHorizon,
)
from maritime_forecast.models.persistence import Persistence
from maritime_forecast.schemas.manifest import Period, SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunExistsError, RunStore, RunTamperedError


@dataclass
class FlakyControl(Persistence):
    """CONTROL ONLY: persistence that fails for one vessel (to exercise paired scoring)."""

    model_id: str = "flaky_control"

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        if history.vessel_id == TURNING:
            raise RuntimeError("simulated failure")
        return super()._predict(history, horizon)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    store = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=store)
    day = datetime(2023, 6, 1, tzinfo=UTC)
    manifest = SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC test manifest",
        periods=[Period(split="train", start=day - timedelta(days=2), end=day - timedelta(days=1)),
                 Period(split="test", start=day, end=day + timedelta(days=1))],
    )  # fmt: skip
    mpath = tmp_path / "m" / "splits" / "P-known__syn.json"
    mpath.parent.mkdir(parents=True)
    mpath.write_text(manifest.model_dump_json())
    monkeypatch.setitem(registry._FACTORIES, "flaky_control", lambda ctx: FlakyControl())
    loose = SamplerConfig(min_spacing_hours=0.25, max_per_vessel_day=50)
    cfg = BenchmarkConfig(
        dataset_id="syn", manifest_path=mpath, horizons_h=(0.25, 0.5), history_hours=1,
        model_ids=("persistence_v1", "constant_velocity_v1", "flaky_control"),
        n_boot=200, min_cell_samples=1,
        samplers={"underway": SamplerConfig(**{**loose.__dict__, "motion_set": "underway"}),
                  "stationary": SamplerConfig(**{**loose.__dict__, "motion_set": "stationary"})},
    )  # fmt: skip
    return store, RunStore(tmp_path / "runs"), cfg


def test_runner_end_to_end_paired_and_immutable(env):
    store, runs, cfg = env
    run = run_benchmark(cfg, store, runs, run_id="syn-run")
    assert run.status == "succeeded" and run.sampler["n_cutoffs"] > 5
    assert {r.model_id for r in run.results} == set(cfg.model_ids)
    # Paired: TURNING cutoffs failed for flaky_control, so they are excluded for EVERY model
    rec = runs.table("syn-run", "records")
    turning = rec.filter((rec["vessel_id"] == TURNING) & rec["evaluable"])
    assert turning.height and not turning["paired"].any()
    # headline is a CELL property: the flaky model's failures shaped the set for everyone
    for r in run.results:
        cell = [
            c
            for c in run.results
            if (c.stratum, c.horizon_minutes) == (r.stratum, r.horizon_minutes)
        ]
        assert len({c.headline for c in cell}) == 1
        if max(c.failure_rate for c in cell) > 0.01:
            assert not r.headline
    # sample hash is built from vessel|as_of|horizon, so it is comparable across runs
    import hashlib

    r0 = run.results[0]
    cut = runs.table("syn-run", "cutoffs")
    rec0 = rec.filter(
        (rec["model_id"] == r0.model_id)
        & (rec["horizon_h"] == r0.horizon_minutes / 60)
        & rec["paired"]
    )
    if r0.stratum.endswith(":ALL"):
        rec0 = rec0.filter(rec0["motion_set"] == r0.stratum.split(":")[0])
        keys = sorted(
            f"{v}|{t.isoformat()}|{r0.horizon_minutes / 60}"
            for v, t in zip(rec0["vessel_id"], rec0["observed_at"], strict=True)
        )
        assert hashlib.sha256("\n".join(keys).encode()).hexdigest() == r0.sample_set_sha256
    assert cut.height
    flaky = [r for r in run.results if r.model_id == "flaky_control"]
    assert all(
        r.n_failed > 0 for r in flaky if "underway" in r.stratum and r.stratum.endswith("ALL")
    )
    assert all(r.n_failed == 0 for r in run.results if r.model_id == "persistence_v1")
    for reasons in run.not_evaluable.values():
        assert reasons["model_failed"] > 0
    # every stored error belongs to an evaluable sample of a model that delivered
    assert rec.filter(rec["error_m"].is_not_null() & ~rec["evaluable"]).height == 0
    # no NaN anywhere in the record (it must be valid JSON)
    assert "NaN" not in (runs.root / "syn-run" / "run.json").read_text()
    # immutable
    with pytest.raises(RunExistsError):
        run_benchmark(cfg, store, runs, run_id="syn-run")
    d = runs.root / "syn-run"
    assert not os.access(d / "records.parquet", os.W_OK)
    assert json.loads((d / "files.json").read_text()).keys() == {
        "cutoffs.parquet",
        "records.parquet",
        "report.md",
        "run.json",
    }
    assert "underway:ALL" in (d / "report.md").read_text()


def test_tampering_is_detected(env):
    store, runs, cfg = env
    run_benchmark(cfg, store, runs, run_id="t")
    d = runs.root / "t"
    os.chmod(d, stat.S_IRWXU)
    os.chmod(d / "report.md", stat.S_IRUSR | stat.S_IWUSR)
    (d / "report.md").write_text("edited")
    with pytest.raises(RunTamperedError):
        runs.load("t")


def test_runner_is_deterministic(env):
    store, runs, cfg = env
    a = run_benchmark(cfg, store, runs, run_id="a")
    b = run_benchmark(cfg, store, runs, run_id="b")
    assert a.not_evaluable == b.not_evaluable
    assert len(a.results) == len(b.results)
    for ca, cb in zip(a.results, b.results, strict=True):
        da, db = ca.model_dump(), cb.model_dump()
        diff = {k: (da[k], db[k]) for k in da if da[k] != db[k]}
        assert not diff, (ca.model_id, ca.stratum, ca.horizon_minutes, diff)


def test_cutoffs_respect_the_split(env):
    store, runs, cfg = env
    run_benchmark(cfg, store, runs, run_id="s")
    c = runs.table("s", "cutoffs")
    day = datetime(2023, 6, 1, tzinfo=UTC)
    assert (c["observed_at"] - timedelta(hours=cfg.history_hours) >= day).all()
    assert (c["observed_at"] + timedelta(hours=max(cfg.horizons_h)) < day + timedelta(days=1)).all()


def test_batched_histories_equal_store_history(env):
    import polars as pl

    from maritime_forecast.models.base import histories_for_cutoffs

    store, _, _ = env
    obs = store.scan("syn")
    # cutoffs at report times, between reports, and at a gap edge, for several vessels
    picks = obs.gather_every(37).select("vessel_id", "observed_at")
    picks = pl.concat([picks, picks.with_columns(pl.col("observed_at") + timedelta(seconds=30))])
    cutoffs = picks.with_row_index("cutoff_id")
    n = 0
    for cid, h in histories_for_cutoffs(store, "syn", cutoffs, hours=1.5):
        c = cutoffs.row(cid, named=True)
        ref = store.history("syn", c["vessel_id"], c["observed_at"], 1.5)
        assert h.reports.equals(ref), (c, h.reports.height, ref.height)
        n += 1
    assert n == cutoffs.height


def test_small_cells_are_recorded_not_silently_dropped(env):
    store, runs, cfg = env
    from dataclasses import replace

    run = run_benchmark(replace(cfg, min_cell_samples=10_000), store, runs, run_id="small")
    assert run.results == [] or all(r.n_evaluable >= 10_000 for r in run.results)
    assert run.dropped_cells and all(d.n_evaluable < 10_000 for d in run.dropped_cells)
    assert "flaky_control" in run.dropped_cells[0].n_failed


def test_failed_run_is_recorded_and_finalized(env, monkeypatch):
    import maritime_forecast.evaluation.runner as runner_mod

    store, runs, cfg = env

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(runner_mod, "select_cutoffs", boom)
    with pytest.raises(RuntimeError):
        run_benchmark(cfg, store, runs, run_id="failed")
    rec = runs.load("failed")
    assert rec.status == "failed" and "disk on fire" in rec.error and rec.results == []


def test_under_capable_model_refused_before_any_run_exists(env, monkeypatch):
    store, runs, cfg = env

    @dataclass
    class Short(Persistence):
        model_id: str = "short_control"

        def capabilities(self):
            c = super().capabilities()
            return c.model_copy(update={"model_id": self.model_id, "max_horizon_hours": 0.1})

    monkeypatch.setitem(registry._FACTORIES, "short_control", lambda ctx: Short())
    from dataclasses import replace

    with pytest.raises(ValueError, match="supports horizons up to"):
        run_benchmark(replace(cfg, model_ids=("short_control",)), store, runs, run_id="short")
    assert not (runs.root / "short").exists()


def test_run_json_tampering_is_detected(env):
    store, runs, cfg = env
    run_benchmark(cfg, store, runs, run_id="rj")
    d = runs.root / "rj"
    os.chmod(d, stat.S_IRWXU)
    os.chmod(d / "run.json", stat.S_IRUSR | stat.S_IWUSR)
    (d / "run.json").write_text(
        (d / "run.json").read_text().replace('"headline": false', '"headline": true')
    )
    with pytest.raises(RunTamperedError):
        runs.load("rj")


def test_window_spanning_another_period_is_excluded():
    from maritime_forecast.schemas.manifest import Period, SplitManifest

    d = datetime(2023, 6, 1, tzinfo=UTC)
    m = SplitManifest(protocol="P-known", dataset_id="x", description="", periods=[
        Period(split="test", start=d, end=d + timedelta(hours=10)),
        Period(split="calibration", start=d + timedelta(hours=10), end=d + timedelta(hours=11)),
        Period(split="test", start=d + timedelta(hours=11), end=d + timedelta(hours=20)),
    ])  # fmt: skip
    assert m.window_split("v", d + timedelta(hours=8), d + timedelta(hours=12)) is None
    assert m.window_split("v", d + timedelta(hours=1), d + timedelta(hours=5)) == "test"


def test_region_window_ok_uses_context_for_test_and_labels_for_train(tmp_path, monkeypatch):
    """SYNTHETIC tracks around a test box [29.0, 29.5] x [-95, -94] with a 10 km buffer."""
    import polars as pl

    import maritime_forecast.data.ingest as ingest_mod
    from maritime_forecast.data.ingest import ingest
    from maritime_forecast.data.mapping import load_mapping
    from maritime_forecast.evaluation.runner import region_window_ok
    from maritime_forecast.schemas.manifest import Box, RegionSplit

    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    t0 = datetime(2023, 6, 5, tzinfo=UTC)
    head = "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselType,Status,Length,Width,Draft,TransceiverClass"
    rows = [head]

    def track(mmsi, lats):  # one report per 10 min from t0, at lon -94.5 (or far east)
        for k, (la, lo) in enumerate(lats):
            t = t0 + timedelta(minutes=10 * k)
            rows.append(f"{mmsi},{t:%Y-%m-%dT%H:%M:%S},{la},{lo},8,0,511,70,0,100,20,5,A")

    inside, buffer_, far = (29.2, -94.5), (29.55, -94.5), (29.2, -90.0)
    track("999200001", [inside] * 24)  # test side throughout
    track("999200002", [buffer_] * 3 + [inside] * 21)  # context starts in the buffer
    track("999200003", [far] * 24)  # train side throughout
    track("999200004", [far] * 12 + [buffer_] * 12)  # train context, label enters the buffer
    csv = tmp_path / "SYNTHETIC_region.csv"
    csv.write_text("\n".join(rows) + "\n")
    store = ParquetStore(tmp_path / "canonical")
    ingest([csv], load_mapping("noaa_marinecadastre"), "reg", store=store)
    rs = RegionSplit(test_region=Box(name="t", lat_min=29.0, lat_max=29.5, lon_min=-95.0,
                                     lon_max=-94.0), buffer_km=10)  # fmt: skip
    as_of = t0 + timedelta(hours=1, minutes=50)
    cut = pl.DataFrame({"vessel_id": ["999200001", "999200002", "999200003", "999200004"],
                        "observed_at": [as_of] * 4}).with_columns(
        pl.col("observed_at").dt.cast_time_unit("us"))  # fmt: skip
    hist, max_h = timedelta(hours=2), timedelta(hours=1)
    test_ok = region_window_ok(store, "reg", cut, rs, "test", hist, max_h).to_list()
    assert test_ok == [True, False, False, False]
    train_ok = region_window_ok(store, "reg", cut, rs, "train", hist, max_h).to_list()
    assert train_ok == [False, False, True, False]  # 4: its label reports enter the buffer
