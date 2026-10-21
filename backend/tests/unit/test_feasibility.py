"""Navigable mask and feasibility flags, on SYNTHETIC geometry and the SYNTHETIC fixture."""

from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
import shapely
from fixtures.synthetic import CSV_PATH, MOVING, TURNING

import maritime_forecast.data.ingest as ingest_mod
import maritime_forecast.evaluation.runner as runner_mod
from maritime_forecast.config import BBox
from maritime_forecast.data.cutoffs import SamplerConfig
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.evaluation.feasibility import FeasibilityConfig, feasibility_flags
from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
from maritime_forecast.geo.navigable_mask import (
    Grid,
    NavigableMask,
    dilate,
    rasterize_polygons,
    traffic_cells_sql,
    traffic_raster,
    traffic_source_sql,
)
from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore

BOX = BBox("t", 29.0, 29.1, -95.1, -95.0)  # 100 x 100 cells at 0.001°
GRID = Grid.for_bbox(BOX)


def _mask(land_polys, traffic=None) -> NavigableMask:
    land = rasterize_polygons(land_polys, GRID)
    return NavigableMask.build(GRID, land, traffic, "SYNTHETIC")


def test_rasterize_matches_point_in_polygon_and_envelope_path():
    tri = shapely.Polygon([(-95.09, 29.01), (-95.01, 29.01), (-95.05, 29.09)])
    rect = shapely.box(-95.08, 29.02, -95.06, 29.04)
    r = rasterize_polygons([tri], GRID)
    ys = GRID.lat0 + (np.arange(GRID.ny) + 0.5) * GRID.cell_deg
    xs = GRID.lon0 + (np.arange(GRID.nx) + 0.5) * GRID.cell_deg
    X, Y = np.meshgrid(xs, ys)
    assert (r == shapely.contains_xy(tri, X, Y)).all()
    rr = rasterize_polygons([rect], GRID)  # fast path for a whole rectangle
    assert (rr == shapely.contains_xy(rect, X, Y)).all() and rr.sum() == 20 * 20


def test_infeasible_is_land_minus_traffic_and_unknown_outside():
    land = shapely.box(-95.06, 29.0, -95.04, 29.1)  # a 2 km wide north-south strip
    traffic = np.zeros((GRID.ny, GRID.nx), bool)
    traffic[50, 45] = True  # one cell of the strip carried traffic
    m = _mask([land], traffic)
    lat = np.array([29.0505, 29.0705, 29.0505, 30.0, np.nan])
    lon = np.array([-95.0545, -95.0545, -95.0800, -95.05, -95.05])
    assert m.is_infeasible(lat, lon).tolist() == [False, True, False, False, False]
    assert m.is_infeasible(lat.reshape(5, 1), lon.reshape(5, 1)).shape == (5, 1)


def test_segment_through_thin_land_is_caught_between_water_endpoints():
    strip = shapely.box(-95.0505, 29.0, -95.0495, 29.1)  # ~100 m wide: one cell
    m = _mask([strip])
    lat0, lat1 = np.array([29.05, 29.05, 29.05]), np.array([29.05, 29.06, 29.05])
    lon0 = np.array([-95.09, -95.09, -95.09])
    lon1 = np.array([-95.01, -95.09, 179.0])  # across, parallel, antimeridian-sized jump
    assert not m.is_infeasible(lat0, lon0).any() and not m.is_infeasible(lat1, lon1[:1]).any()
    assert m.segment_infeasible(lat0, lon0, lat1, lon1).tolist() == [True, False, False]


def test_dilate_is_chebyshev():
    a = np.zeros((5, 5), bool)
    a[2, 2] = True
    assert dilate(a, 1).sum() == 9 and dilate(a, 2).sum() == 25 and dilate(a, 0).sum() == 1


def test_mask_round_trips(tmp_path):
    m = _mask([shapely.box(-95.06, 29.0, -95.04, 29.1)])
    m.save(tmp_path / "m.npz")
    back = NavigableMask.load(tmp_path / "m.npz")
    assert back.grid == m.grid and (back.infeasible == m.infeasible).all()


def test_feasibility_flags_speed_accel_and_failures():
    # rows: persistence from 20 kn; a 100 km jump in 10 min; a failed model (NaN)
    n_steps, step = 3, 600.0
    lat = np.array([[29.05] * 3, [29.95, 29.95, 29.95], [np.nan] * 3])
    lon = np.full((3, 3), -95.05)
    lon[2] = np.nan
    f = feasibility_flags(
        np.full(3, 29.05), np.full(3, -95.05), np.array([20.0, 10.0, 10.0]),
        np.full(3, step), lat, lon, step, mask=None, cfg=FeasibilityConfig(),
    )  # fmt: skip
    assert f["land_point"] is None and f["land_crossing"] is None  # no mask: not scored
    assert f["speed"].tolist() == [[False] * n_steps, [True, False, False], [False] * n_steps]
    # 20 kn -> 0 in 10 min is 2 kn/min: not above the bound; the jump is far above it
    assert f["accel"][0].tolist() == [False] * 3 and f["accel"][1, 0] and f["accel"][1, 1]
    assert not f["accel"][2].any()


# -- protocol-permitted traffic -------------------------------------------------------------
@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    s = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=s)
    return s


DAY = datetime(2023, 6, 1, tzinfo=UTC)


def _cells(store, manifest, split="train"):
    obs = store.scan("syn")
    box = BBox("all", float(obs["latitude_deg"].min()) - 1, float(obs["latitude_deg"].max()) + 1,
               float(obs["longitude_deg"].min()) - 1, float(obs["longitude_deg"].max()) + 1)  # fmt: skip
    src, cover = traffic_source_sql(store, "syn", manifest, split)
    return store.con.execute(f"SELECT DISTINCT vessel_id FROM {src}").pl(), cover, box


def test_traffic_uses_only_the_train_period(store):
    noon = DAY + timedelta(hours=1)
    m = SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC",
        periods=[Period(split="train", start=DAY, end=noon),
                 Period(split="test", start=noon + timedelta(hours=1), end=DAY + timedelta(days=1))],
    )  # fmt: skip
    src, cover = traffic_source_sql(store, "syn", m, "train")
    late = store.con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    allrows = store.scan("syn").filter(pl.col("quality_flags").list.len() == 0)
    assert late == allrows.filter(pl.col("observed_at") < noon).height > 0
    assert cover["data_until"] == noon.isoformat() and not cover["vessel_disjoint"]


def test_traffic_excludes_test_vessels_under_p_unseen(store):
    va = next(
        v for v in (VesselAssignment(salt=f"s{i}", buckets={"train": (0, 500), "test": (500, 1000)})
                    for i in range(100))
        if v.assign(TURNING) == "test" and v.assign(MOVING) == "train"
    )  # fmt: skip
    m = SplitManifest(
        protocol="P-unseen", dataset_id="syn", description="SYNTHETIC", vessel_assignment=va,
        periods=[Period(split="train", start=DAY, end=DAY + timedelta(days=1))],
    )  # fmt: skip
    vessels, cover, box = _cells(store, m)
    got = set(vessels["vessel_id"])
    assert MOVING in got and TURNING not in got and cover["vessel_disjoint"]
    src, _ = traffic_source_sql(store, "syn", m, "train")
    grid = Grid.for_bbox(box, 0.01)
    cells = store.con.execute(traffic_cells_sql(src, grid)).pl()
    assert traffic_raster(cells, grid, 1, 0).sum() == cells.height > 0


# -- runner integration -------------------------------------------------------------------
def test_runner_scores_feasibility_with_a_mask(store, tmp_path, monkeypatch):
    mpath = tmp_path / "m" / "splits" / "P-known__syn.json"
    mpath.parent.mkdir(parents=True)
    mpath.write_text(SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC",
        periods=[Period(split="train", start=DAY - timedelta(days=2), end=DAY - timedelta(days=1)),
                 Period(split="test", start=DAY, end=DAY + timedelta(days=1))],
    ).model_dump_json())  # fmt: skip
    # SYNTHETIC "land": everything east of where MOVING is at 01:15, so its 45° track runs
    # onto it within the horizon of the first cutoffs (cutoffs need 1 h of history)
    obs = store.scan("syn").filter(pl.col("vessel_id") == MOVING).sort("observed_at")
    lat0, lon0 = float(obs["latitude_deg"][75]), float(obs["longitude_deg"][75]) - 0.02
    box = BBox("t", lat0 - 1, lat0 + 1, lon0 - 1, lon0 + 1)
    grid = Grid.for_bbox(box, 0.01)
    mask = NavigableMask.build(
        grid, rasterize_polygons([shapely.box(lon0 + 0.02, lat0 - 1, lon0 + 1, lat0 + 1)], grid),
        None, "SYNTHETIC",
    )  # fmt: skip
    side = {"traffic": {"split": "train"}, "source_id": "SYNTHETIC", "mask_sha256": "x"}
    monkeypatch.setattr(runner_mod, "load_navigable_mask", lambda *a: (mask, side))
    loose = SamplerConfig(min_spacing_hours=0.25, max_per_vessel_day=50)
    cfg = BenchmarkConfig(
        dataset_id="syn", manifest_path=mpath, horizons_h=(0.25, 0.5), history_hours=1,
        model_ids=("persistence_v1", "constant_velocity_v1"), n_boot=100, min_cell_samples=1,
        samplers={k: SamplerConfig(**{**loose.__dict__, "motion_set": k})
                  for k in ("underway", "stationary")},
    )  # fmt: skip
    runs = RunStore(tmp_path / "runs")
    run = run_benchmark(cfg, store, runs, run_id="feas")
    assert run.feasibility["mask"] == "SYNTHETIC" and not run.warnings
    rec = runs.table("feas", "records")
    mv = rec.filter((pl.col("vessel_id") == MOVING) & (pl.col("horizon_h") == 0.5))
    cv = mv.filter(pl.col("model_id") == "constant_velocity_v1")
    pers = mv.filter(pl.col("model_id") == "persistence_v1")
    assert cv["feas_land_point"].any() and cv["feas_land_crossing"].any()
    # persistence stays at the anchor: flagged exactly when the anchor itself is on "land"
    # (cells are land from the box edge lon0 + 0.02 eastward)
    assert (pers["feas_land_point"] == (pers["pred_lon"] >= lon0 + 0.02)).all()
    assert not pers["feas_land_point"].all()
    # flags are cumulative: a land point by 15 min stays flagged at 30 min
    early = rec.filter(
        (pl.col("model_id") == "constant_velocity_v1") & (pl.col("horizon_h") == 0.25)
    )
    j = early.join(cv, on="cutoff_id", suffix="_30")
    assert (~j["feas_land_point"] | j["feas_land_point_30"]).all()
    # failed / unscored rows carry null flags, never False
    assert rec.filter(pl.col("status") != "ok")["feas_land_point"].null_count() == rec.filter(
        pl.col("status") != "ok").height  # fmt: skip
    cells = [r for r in run.results if r.model_id == "constant_velocity_v1"]
    assert all(r.feasibility is not None and r.feasibility.truth_on_land_rate is not None
               for r in cells)  # fmt: skip
    assert pers["pred_lat"].is_not_null().all() and mv["truth_lat"].is_not_null().any()


def test_mask_fitted_on_the_scored_split_is_refused(store, tmp_path, monkeypatch):
    mpath = tmp_path / "m" / "splits" / "P-known__syn.json"
    mpath.parent.mkdir(parents=True)
    mpath.write_text(SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC",
        periods=[Period(split="test", start=DAY, end=DAY + timedelta(days=1))],
    ).model_dump_json())  # fmt: skip
    side = {"traffic": {"split": "test"}, "source_id": "S", "mask_sha256": "x"}
    monkeypatch.setattr(runner_mod, "load_navigable_mask", lambda *a: (None, side))
    cfg = BenchmarkConfig(dataset_id="syn", manifest_path=mpath, horizons_h=(0.5,),
                          model_ids=("persistence_v1",))  # fmt: skip
    with pytest.raises(ValueError, match="navigable mask was fitted on the 'test' split"):
        run_benchmark(cfg, store, RunStore(tmp_path / "runs"), run_id="x")
    assert not (tmp_path / "runs" / "x").exists()


def test_geojson_export_never_draws_truth_as_a_line(store, tmp_path, monkeypatch):
    from maritime_forecast.evaluation.export import write_run_geojson

    mpath = tmp_path / "m" / "splits" / "P-known__syn.json"
    mpath.parent.mkdir(parents=True)
    mpath.write_text(SplitManifest(
        protocol="P-known", dataset_id="syn", description="SYNTHETIC",
        periods=[Period(split="test", start=DAY, end=DAY + timedelta(days=1))],
    ).model_dump_json())  # fmt: skip
    loose = SamplerConfig(min_spacing_hours=0.25, max_per_vessel_day=50)
    cfg = BenchmarkConfig(
        dataset_id="syn", manifest_path=mpath, horizons_h=(0.25, 0.5), history_hours=1,
        model_ids=("persistence_v1", "constant_velocity_v1"), n_boot=100, min_cell_samples=1,
        samplers={k: SamplerConfig(**{**loose.__dict__, "motion_set": k})
                  for k in ("underway", "stationary")},
    )  # fmt: skip
    runs = RunStore(tmp_path / "runs")
    run_benchmark(cfg, store, runs, run_id="gj")
    out = write_run_geojson(runs, "gj", ["underway:ALL"], paired_only=True, root=tmp_path / "ex")
    import json

    fc = json.loads(out.read_text())
    kinds = {}
    for f in fc["features"]:
        kinds.setdefault(f["properties"]["kind"], []).append(f)
    assert set(kinds) == {"anchor", "truth", "prediction"}
    assert all(f["geometry"]["type"] == "MultiPoint" for f in kinds["truth"])
    assert all(f["properties"]["motion_set"] == "underway" for f in fc["features"])
    preds = kinds["prediction"]
    assert len(preds) == 2 * len(kinds["anchor"])  # one per model per cutoff
    p = preds[0]
    assert (
        p["geometry"]["coordinates"][0]
        == next(
            a
            for a in kinds["anchor"]
            if a["properties"]["cutoff_id"] == p["properties"]["cutoff_id"]
        )["geometry"]["coordinates"]
    )  # the path starts at the anchor report
    assert len(p["properties"]["error_m"]) == len(p["properties"]["horizons_h"]) == 2
    side = json.loads(out.with_suffix(".json").read_text())
    assert side["run_id"] == "gj" and side["filter"]["paired_only"]
