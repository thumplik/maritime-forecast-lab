"""Benchmark endpoints on the SYNTHETIC fixture (never a real benchmark)."""

import os
import stat
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from fixtures.synthetic import CSV_PATH

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.api.app import ApiSettings, create_app
from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
from maritime_forecast.schemas.manifest import Period, SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore

DAY = datetime(2023, 6, 1, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    manifests = tmp_path / "m"
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", manifests)
    store = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=store)
    manifest = SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC test manifest",
        periods=[Period(split="train", start=DAY - timedelta(days=2), end=DAY - timedelta(days=1)),
                 Period(split="test", start=DAY, end=DAY + timedelta(days=1))],
    )  # fmt: skip
    mpath = manifests / "splits" / "P-known__syn.json"
    mpath.parent.mkdir(parents=True)
    mpath.write_text(manifest.model_dump_json())
    settings = ApiSettings(
        store_root=tmp_path / "canonical", manifest_dir=manifests, runs_dir=tmp_path / "runs"
    )
    runs = RunStore(tmp_path / "runs" / "benchmarks")
    loose = SamplerConfig(min_spacing_hours=0.25, max_per_vessel_day=50)
    cfg = BenchmarkConfig(
        dataset_id="syn", manifest_path=mpath, horizons_h=(0.25, 0.5), history_hours=1,
        model_ids=("persistence_v1", "constant_velocity_v1"), n_boot=100, min_cell_samples=1,
        samplers={k: SamplerConfig(**{**loose.__dict__, "motion_set": k})
                  for k in ("underway", "stationary")},
    )  # fmt: skip
    return TestClient(create_app(settings)), store, runs, cfg


def err(r, status, code):
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    return r.json()["error"]


def _body(**kw):
    return {"dataset_id": "syn", "protocol": "P-known", "horizons_hours": [0.25, 0.5],
            "history_hours": 1, "n_boot": 100, "step_minutes": 5,
            "model_ids": ["persistence_v1", "constant_velocity_v1"]} | kw  # fmt: skip


def test_get_finalized_run_and_summary(env):
    client, store, runs, cfg = env
    run = run_benchmark(cfg, store, runs, run_id="syn-run")
    st = client.get("/benchmarks/syn-run").json()
    assert st["status"] == "succeeded" and st["run"]["run_id"] == "syn-run"
    assert st["run"]["protocol"]["manifest_sha256"] == run.protocol.manifest_sha256
    s = client.get("/benchmarks/syn-run/summary").json()
    assert s["n_cutoffs"] == run.sampler["n_cutoffs"] and s["seed"] == cfg.seed
    assert len(s["cells"]) == len({(r.stratum, r.horizon_minutes) for r in run.results})
    assert s["max_cutoffs_per_set"] is None and not s["warnings"]
    for c in s["cells"]:  # one row per paired cell, every model side by side
        assert set(c["models"]) == set(cfg.model_ids)
        assert all(m["n_failed"] == 0 for m in c["models"].values())
        orig = [r for r in run.results
                if (r.stratum, r.horizon_minutes) == (c["stratum"], c["horizon_minutes"])]  # fmt: skip
        assert {r.sample_set_sha256 for r in orig} == {c["sample_set_sha256"]}
        for r in orig:
            assert c["models"][r.model_id]["median_error_m"] == r.median_error_m.model_dump()
    only = client.get("/benchmarks/syn-run/summary", params={"stratum": "underway:ALL"}).json()
    assert {c["stratum"] for c in only["cells"]} == {"underway:ALL"}
    head = client.get("/benchmarks/syn-run/summary", params={"headline_only": True}).json()
    assert all(c["headline"] for c in head["cells"])


def test_tampered_run_is_refused(env):
    client, store, runs, cfg = env
    run_benchmark(cfg, store, runs, run_id="t")
    d = runs.root / "t"
    os.chmod(d, stat.S_IRWXU)
    os.chmod(d / "report.md", stat.S_IRUSR | stat.S_IWUSR)
    (d / "report.md").write_text("edited")
    err(client.get("/benchmarks/t"), 409, "run_tampered")
    err(client.get("/benchmarks/t/summary"), 409, "run_tampered")


def test_unknown_and_unfinished_runs(env):
    client, _, runs, _ = env
    err(client.get("/benchmarks/nope"), 404, "run_not_found")
    err(client.get("/benchmarks/-x"), 422, "invalid_parameter")
    runs.create("half")  # a directory without run.json: CLI still going, or interrupted
    assert client.get("/benchmarks/half").json()["status"] == "incomplete"
    err(client.get("/benchmarks/half/summary"), 409, "run_not_finished")


@pytest.mark.parametrize(
    "bad,status,code",
    [
        ({"dataset_id": "nope"}, 404, "dataset_not_found"),
        ({"protocol": "P-unseen"}, 404, "manifest_not_found"),
        ({"protocol": "P-paper"}, 422, "invalid_parameter"),
        ({"model_ids": ["oracle_v9"]}, 422, "benchmark_refused"),
        ({"model_ids": ["damped_velocity_v1"]}, 422, "benchmark_refused"),  # not fitted
        ({"horizons_hours": [0.25, 0.25]}, 422, "invalid_parameter"),
        ({"horizons_hours": [0.3]}, 422, "invalid_parameter"),  # not whole 5-min steps
        ({"n_boot": 10}, 422, "invalid_parameter"),
        ({"cutoff_region": "x"}, 422, "invalid_parameter"),
    ],
)
def test_refused_before_anything_starts(env, bad, status, code):
    client, _, runs, _ = env
    err(client.post("/benchmarks", json=_body(**bad)), status, code)
    assert not runs.root.exists() or not any(runs.root.iterdir())


def test_post_runs_in_background_and_finalizes(env):
    client, _, runs, _ = env
    r = client.post("/benchmarks", json=_body())
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.headers["location"] == f"/benchmarks/{run_id}" and run_id.startswith("P-known_syn_")
    err(client.post("/benchmarks", json=_body()), 409, "benchmark_busy")  # one at a time
    deadline = time.monotonic() + 120
    while (st := client.get(f"/benchmarks/{run_id}").json())["status"] == "running":
        assert time.monotonic() < deadline, "benchmark did not finish"
        time.sleep(0.2)
    assert st["status"] == "succeeded", st
    assert st["run"]["sampler"]["seed"] == 20230601
    assert {m["model_id"] for m in st["run"]["models"]} == {"persistence_v1",
                                                             "constant_velocity_v1"}  # fmt: skip
    assert runs.load(run_id).run_id == run_id  # immutable record in the run store
    assert client.get(f"/benchmarks/{run_id}/summary").status_code == 200


def test_smoke_runs_are_labelled(env):
    client, store, runs, cfg = env
    from dataclasses import replace

    run_benchmark(replace(cfg, max_cutoffs_per_set=3), store, runs, run_id="smoke")
    s = client.get("/benchmarks/smoke/summary").json()
    assert s["max_cutoffs_per_set"] == 3 and any("smoke run" in w for w in s["warnings"])
