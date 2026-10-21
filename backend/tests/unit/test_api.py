"""Read API on a SYNTHETIC store (not real AIS)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.api.app import ApiSettings, create_app, parse_bbox
from maritime_forecast.api.errors import ApiError
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, tzinfo=UTC)


def _csv_rows():
    rows = [
        "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselType,Status,Length,Width,Draft,TransceiverClass"
    ]
    # vessel A: every 5 min 00:00-02:00, a 2 h gap, then 04:00-05:00 (two segments)
    for k in list(range(0, 121, 5)) + list(range(240, 301, 5)):
        t = T0 + timedelta(minutes=k)
        rows.append(
            f"367000001,{t:%Y-%m-%dT%H:%M:%S},{29.0 + k * 1e-3},-94.5,10,0,511,70,0,200,30,9,A"
        )
    # vessel B: on 06-02, far east (outside the Houston bbox)
    for k in range(0, 60, 10):
        t = T0 + timedelta(days=1, minutes=k)
        rows.append(f"367000002,{t:%Y-%m-%dT%H:%M:%S},27.0,-85.0,5,90,90,52,0,30,10,,A")
    return "\n".join(rows) + "\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    manifests = tmp_path / "manifests"
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", manifests)
    f = tmp_path / "noaa.csv"
    f.write_text(_csv_rows())
    store = ParquetStore(tmp_path / "canonical")
    ingest([f], load_mapping("noaa_marinecadastre"), "syn", store=store)
    # a dataset without a manifest
    store.write("bare", "SELECT * FROM read_parquet('" + str(store.files("syn")[0]) + "')")
    runs = tmp_path / "runs" / "coverage_audit_20990101T000000Z"
    runs.mkdir(parents=True)
    (runs / "summary.json").write_text(json.dumps({
        "dataset_id": "syn", "admission_config": {"min_fraction": 0.5},
        "admission": [{"stratum": "underway:cargo", "horizon_h": 1, "status": "first_class"},
                      {"stratum": "underway:cargo", "horizon_h": 24, "status": "experimental"}],
    }))  # fmt: skip
    settings = ApiSettings(
        store_root=tmp_path / "canonical",
        manifest_dir=manifests,
        runs_dir=tmp_path / "runs",
        max_window_days=3,
        max_track_points=1000,
    )
    return TestClient(create_app(settings))


def err(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    return body["error"]


def test_health(client):
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["datasets"] == 2 and r["data_root_exists"]


def test_datasets(client):
    ds = {d["dataset_id"]: d for d in client.get("/datasets").json()["datasets"]}
    syn = ds["syn"]
    assert syn["has_manifest"] and syn["mapping_id"] == "noaa_marinecadastre@v1"
    assert syn["static_fields_provenance"] == "backfilled"
    assert (syn["first_day"], syn["last_day"], syn["days"], syn["rows"]) == (
        "2023-06-01",
        "2023-06-02",
        2,
        44,
    )
    assert syn["ingest"]["rows_in"] == 44
    assert syn["admission"]["strata"]["underway:cargo"] == {
        "1": "first_class",
        "24": "experimental",
    }
    assert ds["bare"]["has_manifest"] is False and ds["bare"]["admission"] is None


def test_vessels_pagination_bbox_window(client):
    r = client.get("/vessels", params={"dataset_id": "syn", "limit": 1}).json()
    assert r["total"] == 2 and [v["vessel_id"] for v in r["vessels"]] == ["367000001"]
    assert r["from"] == "2023-06-01T00:00:00Z" and r["to"] == "2023-06-03T00:00:00Z"
    r = client.get("/vessels", params={"dataset_id": "syn", "offset": 1}).json()
    assert [v["vessel_id"] for v in r["vessels"]] == ["367000002"]
    r = client.get("/vessels", params={"dataset_id": "syn", "bbox": "-96,28,-93.3,30.5"}).json()
    assert r["total"] == 1 and r["bbox"] == [-96.0, 28.0, -93.3, 30.5]
    r = client.get("/vessels", params={"dataset_id": "syn", "from": "2023-06-02T00:00:00Z"}).json()
    assert [v["vessel_id"] for v in r["vessels"]] == ["367000002"]
    # a non-UTC offset is converted, not rejected
    r = client.get(
        "/vessels", params={"dataset_id": "syn", "from": "2023-06-01T19:00:00-05:00"}
    ).json()
    assert r["from"] == "2023-06-02T00:00:00Z"


def test_track_segments_and_gaps(client):
    r = client.get("/vessels/367000001/track", params={"dataset_id": "syn"}).json()
    assert r["n_points"] == 25 + 13 and r["gap_minutes"] == 30
    assert [s["n_points"] for s in r["segments"]] == [25, 13]
    assert r["gaps"] == [
        {"start": "2023-06-01T02:00:00Z", "end": "2023-06-01T04:00:00Z", "duration_s": 7200.0}
    ]
    p = r["points"]
    assert len(p["observed_at"]) == len(p["latitude_deg"]) == len(p["segment"]) == 38
    assert p["segment"][24:26] == [0, 1] and p["heading_deg"][0] is None  # 511 sentinel
    assert any("back-filled" in w for w in r["warnings"])


def test_track_window_is_half_open(client):
    r = client.get(
        "/vessels/367000001/track",
        params={"dataset_id": "syn", "from": "2023-06-01T00:05:00Z", "to": "2023-06-01T00:15:00Z"},
    ).json()
    assert r["points"]["observed_at"] == ["2023-06-01T00:05:00Z", "2023-06-01T00:10:00Z"]


@pytest.mark.parametrize(
    "params,status,code",
    [
        ({"dataset_id": "nope"}, 404, "dataset_not_found"),
        ({"dataset_id": "../etc"}, 422, "invalid_parameter"),
        ({"dataset_id": "syn", "from": "2023-06-01T00:00:00"}, 422, "invalid_parameter"),  # naive
        (
            {"dataset_id": "syn", "from": "2023-06-02T00:00:00Z", "to": "2023-06-01T00:00:00Z"},
            422,
            "invalid_parameter",
        ),
        (
            {"dataset_id": "syn", "from": "2023-06-01T00:00:00Z", "to": "2023-06-10T00:00:00Z"},
            422,
            "window_too_large",
        ),
        ({"dataset_id": "syn", "bbox": "1,2,3"}, 422, "invalid_parameter"),
        ({"dataset_id": "syn", "bbox": "10,0,5,1"}, 422, "invalid_parameter"),
        ({"dataset_id": "syn", "limit": 5000}, 422, "invalid_parameter"),
        ({"dataset_id": "syn", "limit": 0}, 422, "invalid_parameter"),
        ({}, 422, "invalid_parameter"),
    ],
)
def test_vessels_errors(client, params, status, code):
    err(client.get("/vessels", params=params), status, code)


def test_track_errors(client, tmp_path):
    err(
        client.get("/vessels/999999999/track", params={"dataset_id": "syn"}),
        404,
        "vessel_not_found",
    )
    err(
        client.get("/vessels/bad id!/track", params={"dataset_id": "syn"}), 422, "invalid_parameter"
    )
    e = err(
        client.get("/vessels/" + "x" * 40 + "/track", params={"dataset_id": "syn"}),
        422,
        "invalid_parameter",
    )
    assert "x" * 40 not in json.dumps(e)  # input is never echoed back


def test_too_many_points(tmp_path, client):
    small = client.app.state.ctx.settings
    small.max_track_points = 10
    e = err(
        client.get("/vessels/367000001/track", params={"dataset_id": "syn"}), 413, "too_many_points"
    )
    assert e["details"] == {"points": 38, "limit": 10}


def test_parse_bbox_order():
    b = parse_bbox("-96,28,-93.3,30.5")
    assert (b.lon_min, b.lat_min, b.lon_max, b.lat_max) == (-96, 28, -93.3, 30.5)
    with pytest.raises(ApiError):
        parse_bbox("-96;28;-93;30")


def test_openapi_lists_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/datasets", "/vessels", "/vessels/{vessel_id}/track"} <= set(paths)


# -- models and forecasts ---------------------------------------------------------------------
def test_models_endpoint(client):
    r = client.get("/models", params={"dataset_id": "syn"}).json()
    by = {m["model_id"]: m for m in r["models"]}
    assert set(by) == {"persistence_v1", "constant_velocity_v1", "constant_velocity_paper_v1",
                       "constant_turn_rate_v1", "damped_velocity_v1"}  # fmt: skip
    assert by["constant_velocity_paper_v1"]["calibration_only"]
    assert not by["damped_velocity_v1"]["installed"]  # no fit for this dataset
    assert by["damped_velocity_v1"]["reads_beyond_own_history"]
    assert r["parameters_from"] == "P-known"
    err(client.get("/models", params={"dataset_id": "nope"}), 404, "dataset_not_found")


def _req(**kw):
    return {"dataset_id": "syn", "vessel_id": "367000001", "as_of": "2023-06-01T01:00:00Z",
            "horizon_hours": 1, "step_minutes": 10, "history_hours": 2,
            "model_ids": ["persistence_v1", "constant_velocity_v1", "damped_velocity_v1"]} | kw  # fmt: skip


def test_forecast_endpoint(client):
    r = client.post("/forecasts", json=_req())
    assert r.status_code == 200, r.text
    f = r.json()
    by = {x["model_id"]: x for x in f["forecasts"]}
    assert by["persistence_v1"]["status"] == "ok" and len(by["persistence_v1"]["points"]) == 6
    assert by["persistence_v1"]["points"][0]["timestamp"] == "2023-06-01T01:10:00Z"
    assert by["damped_velocity_v1"]["status"] == "unsupported"
    assert by["damped_velocity_v1"]["error"]["code"] == "not_fitted"
    cov = f["input_coverage"]
    assert cov["last_report"] == "2023-06-01T01:00:00Z" and cov["n_reports"] == 13
    assert "truth" not in json.dumps(f).lower() or "truth is never" in json.dumps(f)


def test_forecast_id_deterministic_and_cache_clean(client):
    a = client.post("/forecasts", json=_req()).json()
    b = client.post("/forecasts", json=_req(show_ground_truth=True)).json()
    c = client.post("/forecasts", json=_req()).json()
    assert a["forecast_id"] == b["forecast_id"] == c["forecast_id"]
    assert b["warnings"] and not c["warnings"]  # per-request warning not cached
    d = client.post("/forecasts", json=_req(as_of="2023-06-01T01:30:00Z")).json()
    assert d["forecast_id"] != a["forecast_id"]
    assert a["data_version"] == d["data_version"]


@pytest.mark.parametrize(
    "bad,code",
    [
        ({"model_ids": ["oracle_v9"]}, "unknown_model"),
        ({"as_of": "2023-06-01T01:00:00"}, "invalid_parameter"),
        ({"horizon_hours": 72, "step_minutes": 1}, "invalid_parameter"),
        ({"vessel_id": "../x"}, "invalid_parameter"),
        ({"model_ids": []}, "invalid_parameter"),
    ],
)
def test_forecast_errors(client, bad, code):
    err(client.post("/forecasts", json=_req(**bad)), 422, code)


def test_forecast_unknown_dataset(client):
    err(client.post("/forecasts", json=_req(dataset_id="nope")), 404, "dataset_not_found")


def test_data_version_changes_forecast_id(client, tmp_path):
    a = client.post("/forecasts", json=_req()).json()
    store = ParquetStore(tmp_path / "canonical")
    src = str(store.files("syn")[0])
    store.write(
        "syn",
        f"SELECT * REPLACE (latitude_deg + 0.001 AS latitude_deg) "
        f"FROM read_parquet('{src}', hive_partitioning=false)",
    )  # same row counts
    b = client.post("/forecasts", json=_req()).json()
    assert a["data_version"] != b["data_version"] and a["forecast_id"] != b["forecast_id"]
    pa = a["forecasts"][0]["points"][0]["latitude_deg"]
    pb = b["forecasts"][0]["points"][0]["latitude_deg"]
    assert pb == pytest.approx(pa + 0.001)  # not served from a stale cache


# -- POST /forecasts/evaluation (truth only after the forecast is made) ------------------
def _ev_req(**kw):
    return _req(model_ids=["persistence_v1", "constant_velocity_v1"]) | kw


def test_evaluation_scores_the_served_forecast(client):
    f = client.post("/forecasts", json=_ev_req()).json()
    r = client.post("/forecasts/evaluation", json=_ev_req())
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["forecast_id"] == f["forecast_id"] and e["data_version"] == f["data_version"]
    assert e["target_times"] == f["forecasts"][0]["target_times"]
    assert e["truth_region"] == "us_gulf" and e["max_bracket_gap_minutes"] == 30
    assert all(e["paired"]) and all(t is not None for t in e["truth"])
    # vessel A climbs 1e-3 deg/min: truth at 01:10 is 29.07; persistence stays at 29.06
    assert e["truth"][0]["latitude_deg"] == pytest.approx(29.07)
    pers = next(m for m in e["models"] if m["model_id"] == "persistence_v1")
    assert pers["scores"][0]["error_m"] == pytest.approx(1109, abs=5)
    assert pers["scores"][0]["horizon_minutes"] == 10 and pers["ade_m"] > 0
    # evaluating does not alter the forecast that is served afterwards
    assert client.post("/forecasts", json=_ev_req()).json() == f


def test_evaluation_reports_gaps_and_failures_per_target_time(client):
    # as_of 01:30, 1 h: reports stop at 02:00 and resume at 04:00, beyond the truth window
    # (horizon + 1 h, as in the benchmark) -> 02:10..02:30 have no report after them
    req = _req(as_of="2023-06-01T01:30:00Z")  # includes damped velocity (not fitted here)
    e = client.post("/forecasts/evaluation", json=req).json()
    assert e["truth_reasons"] == [None, None, None] + ["no_report_after"] * 3
    assert e["truth"][3] is None and not any(e["paired"])  # damped failed everywhere
    by = {m["model_id"]: m for m in e["models"]}
    assert by["damped_velocity_v1"]["status"] == "unsupported"
    assert [s["reason"] for s in by["damped_velocity_v1"]["scores"]] == (
        ["model_failed"] * 3 + ["no_report_after"] * 3
    )
    assert by["persistence_v1"]["scores"][2]["evaluable"]  # 02:00 is an exact report
    assert by["persistence_v1"]["ade_m"] is None  # nothing paired
    assert any("not comparable" in w for w in e["warnings"])


def test_evaluation_errors(client):
    err(client.post("/forecasts/evaluation", json=_ev_req(dataset_id="nope")), 404,
        "dataset_not_found")  # fmt: skip
    err(client.post("/forecasts/evaluation", json=_ev_req(model_ids=["oracle_v9"])), 422,
        "unknown_model")  # fmt: skip


def test_forecast_never_carries_truth_fields(client):
    f = client.post("/forecasts", json=_ev_req(show_ground_truth=True)).json()
    flat = json.dumps(f)
    assert "error_m" not in flat and "truth_reasons" not in flat
    assert "/forecasts/evaluation" in f["warnings"][0]


def test_evaluation_brackets_with_a_report_before_as_of(client):
    # as_of 01:02 sits between reports (01:00, 01:05); the first target (01:03) is bracketed
    # by 01:00, which lies before as_of. Truth must still find it (review finding).
    e = client.post(
        "/forecasts/evaluation",
        json=_ev_req(as_of="2023-06-01T01:02:00Z", step_minutes=1, horizon_hours=0.05),
    ).json()
    assert e["truth_reasons"] == [None, None, None] and all(e["paired"])
    assert e["truth"][0]["latitude_deg"] == pytest.approx(29.063)
