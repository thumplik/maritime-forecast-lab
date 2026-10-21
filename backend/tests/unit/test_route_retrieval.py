"""route_retrieval_v1 on a SYNTHETIC lane fixture: matching, continuation, provenance,
protocol eligibility and leakage invariance. Not real AIS (AGENTS.md rule 4)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import maritime_forecast.data.ingest as ingest_mod
import maritime_forecast.models.route_retrieval as rr
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.evaluation.invariance import PERTURBATIONS, check_invariance
from maritime_forecast.geo.geodesic import direct, distance_m, inverse, knots_to_mps
from maritime_forecast.models.base import ForecastHorizon, ModelContext, history_from_store
from maritime_forecast.models.registry import get_model
from maritime_forecast.models.route_retrieval import RetrievalConfig, RouteRetrieval
from maritime_forecast.schemas.manifest import Period, SplitManifest, VesselAssignment
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, tzinfo=UTC)
TRAIN_END = T0 + timedelta(days=1)
START = (29.0, -94.5)
HEADER = "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselType,Status,Length,Width,Draft,TransceiverClass"
# Historical voyages (day 1): north for 1 h at 12 kn, then EAST (3 vessels) or WEST (1).
EAST = ["999100001", "999100002", "999100003"]
WEST = ["999100004"]
TARGET = "999100009"  # day-2 voyage north; also made one EAST voyage on day 1 (own history)
FAR = "999100010"  # day 2, far from every lane
DAY2 = "999100011"  # day 2, WEST, finished before AS_OF: test-period data before the cutoff


def _voyage(mmsi, t_start, branch, vtype=70, start=START):
    """1-min reports: 60 min north, then 120 min on `branch` (90 or 270 deg), at 12 kn."""
    rows, (lat, lon) = [], start
    for k in range(181):
        cog = 0.0 if k < 60 else branch
        t = t_start + timedelta(minutes=k)
        rows.append(f"{mmsi},{t:%Y-%m-%dT%H:%M:%S},{lat:.6f},{lon:.6f},12.0,{cog:.1f},511,"
                    f"{vtype},0,180,30,9.5,A")  # fmt: skip
        la, lo = direct(lat, lon, cog, knots_to_mps(12.0) * 60)
        lat, lon = float(la), float(lo)
    return rows


def _csv(path):
    rows = [HEADER]
    for i, m in enumerate(EAST):
        rows += _voyage(m, T0 + timedelta(hours=1 + 4 * i), 90.0)
    rows += _voyage(WEST[0], T0 + timedelta(hours=14), 270.0)
    rows += _voyage(TARGET, T0 + timedelta(hours=18), 90.0)
    rows += _voyage(TARGET, TRAIN_END + timedelta(hours=10), 90.0)
    rows += _voyage(FAR, TRAIN_END + timedelta(hours=10), 90.0, start=(26.0, -90.0))
    rows += _voyage(DAY2, TRAIN_END + timedelta(hours=4), 270.0)
    path.write_text("\n".join(rows) + "\n")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rr")
    mp = pytest.MonkeyPatch()
    mp.setattr(ingest_mod, "MANIFEST_DIR", tmp / "m")
    csv = tmp / "SYNTHETIC_lanes.csv"
    _csv(csv)
    s = ParquetStore(tmp / "canonical")
    ingest([csv], load_mapping("noaa_marinecadastre"), "lanes", store=s)
    yield s
    mp.undo()


def _manifest(vessel_disjoint=False):
    va = None
    if (
        vessel_disjoint
    ):  # every vessel is train-assigned (TARGET too: the per-query exclusion drops it)
        va = VesselAssignment(salt="t", buckets={"train": (0, 1000)})
    return SplitManifest(
        protocol="P-unseen" if vessel_disjoint else "P-known", dataset_id="lanes",
        description="SYNTHETIC",
        periods=[Period(split="train", start=T0, end=TRAIN_END),
                 Period(split="test", start=TRAIN_END, end=T0 + timedelta(days=3))],
        vessel_assignment=va,
    )  # fmt: skip


AS_OF = TRAIN_END + timedelta(hours=10, minutes=30)  # target half-way up the north leg


def _model(store, manifest=None, **cfg):
    params = {"manifest": manifest or _manifest(), "config": cfg}
    return get_model("route_retrieval_v1", ModelContext(store, "lanes", params=params))


def _forecast(store, vessel=TARGET, as_of=AS_OF, horizon_h=2.0, **kw):
    h = history_from_store(store, "lanes", vessel, as_of, 3)
    return _model(store, **kw).predict(h, ForecastHorizon(as_of, horizon_h, 10))


def test_follows_the_majority_branch_of_the_lane(store):
    f = _forecast(store)
    assert f.status == "ok", f.error
    assert [p.timestamp for p in f.points] == ForecastHorizon(AS_OF, 2.0, 10).target_times
    # After 30 more min north the voyage turns east: at +90 min it is ~12 kn * 60 min east of
    # the corner. The corner is 12 nmi north of START.
    corner = direct(*START, 0.0, knots_to_mps(12) * 3600)
    want = direct(float(corner[0]), float(corner[1]), 90.0, knots_to_mps(12) * 3600)
    p = f.points[8]  # +90 min
    assert distance_m(p.latitude_deg, p.longitude_deg, *want) < 600
    _, brg = inverse(*corner, p.latitude_deg, p.longitude_deg)
    assert 60 < float(brg) < 120  # east, not west


def test_candidates_carry_similarity_and_provenance(store):
    f = _forecast(store)
    ids = [t.reference_segment_ids[0] for t in f.trajectories]
    assert len(ids) == len(set(ids)) == 5  # 4 day-1 voyages of other vessels + target's own
    assert any(i.startswith(WEST[0]) for i in ids)  # the minority branch is a candidate
    assert f.central_trajectory_id in {t.trajectory_id for t in f.trajectories}
    sims = [t.similarity for t in f.trajectories]
    assert all(0 < s <= 1 for s in sims) and sims == sorted(sims, reverse=True)
    central = next(t for t in f.trajectories if t.trajectory_id == f.central_trajectory_id)
    assert central.points == f.points
    assert any("target's own earlier voyages" in w for w in f.warnings)
    assert f.config["fit_split"] == "train" and f.config["pool"] == "frozen"


def test_reference_end_is_held_and_recorded(store):
    f = _forecast(store, horizon_h=6.0)  # references end 2.5 h after the alignment point
    assert f.status == "ok"
    for t in f.trajectories:
        assert t.reference_ends_at is not None
        held = [p for p in t.points if p.timestamp > t.reference_ends_at]
        assert held and len({(p.latitude_deg, p.longitude_deg) for p in held}) == 1
    assert any("hold its last position" in w for w in f.warnings)


def test_p_unseen_never_uses_the_target_or_non_train_vessels(store):
    f = _forecast(store, manifest=_manifest(vessel_disjoint=True))
    assert f.status == "ok"
    ids = [t.reference_segment_ids[0] for t in f.trajectories]
    assert ids and not any(i.startswith(TARGET) for i in ids)
    m = _manifest(vessel_disjoint=True)  # now put the east-bound vessels in test
    m = m.model_copy(update={"vessel_assignment": VesselAssignment(
        salt="t", buckets={"test": (0, 1000)})})  # fmt: skip
    assert _forecast(store, manifest=m).error.code == "no_references"


def test_frozen_pool_holds_only_segments_known_by_train_end(store):
    cfg = RetrievalConfig()
    gap = timedelta(minutes=cfg.index.segment.gap_minutes)
    late = TRAIN_END + timedelta(hours=20)  # after the day-2 voyages have ended
    frozen = rr.load_pool(store, "lanes", _manifest(), rr.pool_until(late, _manifest()), cfg)
    ends = frozen.t0 + (frozen.n - 1) * frozen.step_s + 2 * gap.total_seconds()
    assert frozen.size == 5 and (ends <= TRAIN_END.timestamp()).all()
    rolling = rr.load_pool(store, "lanes", _manifest(), late, cfg)
    assert rolling.size == 8  # + the target's, FAR's and DAY2's day-2 voyages
    # The target's day-2 voyage is never its own reference while it is still under way.
    f = _forecast(store, pool="rolling")
    assert f.status == "ok"
    assert not any(t.reference_segment_ids[0].startswith(f"{TARGET}|2023-06-02")
                   for t in f.trajectories)  # fmt: skip


def test_no_reference_nearby_is_a_typed_failure(store):
    f = _forecast(store, vessel=FAR)
    assert f.status == "failed" and f.error.code == "no_references" and not f.points


def test_unavailable_without_a_manifest_and_refuses_p_region(store):
    m = get_model("route_retrieval_v1", ModelContext(store, "lanes"))
    assert not m.capabilities().installed
    caps = get_model("route_retrieval_v1").capabilities()
    assert caps.reads_beyond_own_history and caps.uses_store_at_predict
    from maritime_forecast.schemas.manifest import Box, RegionSplit

    box = Box(name="b", lat_min=0, lat_max=1, lon_min=0, lon_max=1)
    pr = _manifest().model_copy(update={"protocol": "P-region", "region_split": RegionSplit(
        test_region=box, buffer_km=0)})  # fmt: skip
    assert not _model(store, manifest=pr).capabilities().installed


def test_pool_cache_is_keyed_by_data_version(store, tmp_path):
    a = rr.load_pool(store, "lanes", _manifest(), TRAIN_END, RetrievalConfig())
    assert rr.load_pool(store, "lanes", _manifest(), TRAIN_END, RetrievalConfig()) is a
    other = ParquetStore(tmp_path / "copy", con=store.con)
    other.write("lanes", f"SELECT * FROM read_parquet({[str(f) for f in store.files('lanes')]})"
                " WHERE vessel_id <> '999100001'")  # fmt: skip
    b = rr.load_pool(other, "lanes", _manifest(), TRAIN_END, RetrievalConfig())
    assert b is not a and b.size == a.size - 1


def test_retrieval_is_invariant_to_post_cutoff_data(store, tmp_path):
    params = {"manifest": _manifest(), "config": {}}
    pu = {"manifest": _manifest(vessel_disjoint=True), "config": {"pool": "rolling"}}
    factories = {
        "route_retrieval_v1": lambda s, ds: RouteRetrieval.from_context(
            ModelContext(s, ds, params=params)),
        "route_retrieval_v1_unseen_rolling": lambda s, ds: RouteRetrieval.from_context(
            ModelContext(s, ds, params=pu)),
    }  # fmt: skip
    cases = [
        (TARGET, AS_OF),
        (TARGET, TRAIN_END + timedelta(hours=11, minutes=5, seconds=30)),  # turning
        (WEST[0], T0 + timedelta(hours=14, minutes=50)),  # inside the train period (rolling)
    ]
    rep = check_invariance(factories, store, "lanes", cases, history_hours=3,
                           grids=[(1, 1), (6, 10)], workdir=tmp_path)  # fmt: skip
    assert rep.ok, rep.violations
    assert rep.vacuous == []
    assert rep.checks == len(cases) * len(factories) * 2 * (len(PERTURBATIONS) + 1)
    assert rep.ok_fraction("route_retrieval_v1") >= 0.6, rep.ok_by_model
    assert rep.ok_fraction("route_retrieval_v1_unseen_rolling") >= 0.6, rep.ok_by_model


def test_local_frame_round_trip_and_distances():
    from maritime_forecast.geo.geodesic import LocalFrame

    for lat0, lon0 in [(29.0, -94.5), (75.0, 20.0), (0.0, 179.99), (-40.0, -179.99)]:
        fr = LocalFrame(lat0, lon0)
        la, lo = direct(lat0, lon0, np.array([0, 90, 180, 270, 45.0]), 10_000.0)
        x, y = fr.to_xy(la, lo)
        assert np.allclose(np.hypot(x, y), 10_000.0, atol=1e-3)
        bla, blo = fr.to_latlon(x, y)
        assert np.allclose(distance_m(bla, blo, la, lo), 0, atol=1e-3)


def _comparable(f):
    return f.model_dump(mode="json", exclude={"runtime_ms"})


def test_frozen_forecast_ignores_test_period_data_before_the_cutoff(store, tmp_path):
    """The invariance harness only perturbs rows after as_of. Under the frozen pool, rows in
    (train end, as_of] of OTHER vessels must not matter either (§5 U1)."""
    src = f"read_parquet({[str(f) for f in store.files('lanes')]})"
    other = ParquetStore(tmp_path / "pruned", con=store.con)
    other.write("lanes", f"SELECT * FROM {src} WHERE vessel_id = '{TARGET}' "
                f"OR observed_at <= TIMESTAMPTZ '{TRAIN_END.isoformat()}'")  # fmt: skip
    shifted = ParquetStore(tmp_path / "shifted", con=store.con)
    shifted.write("lanes", f"SELECT * REPLACE (CASE WHEN vessel_id <> '{TARGET}' AND observed_at "
                  f"> TIMESTAMPTZ '{TRAIN_END.isoformat()}' THEN latitude_deg - 0.05 "
                  f"ELSE latitude_deg END AS latitude_deg) FROM {src}")  # fmt: skip
    for kw in ({}, {"manifest": _manifest(vessel_disjoint=True)}):
        ref = _comparable(_forecast(store, **kw))
        assert ref["status"] == "ok"
        assert _comparable(_forecast(other, **kw)) == ref
        assert _comparable(_forecast(shifted, **kw)) == ref
    # Teeth: the rolling pool does use DAY2's test-period voyage.
    roll = _forecast(store, pool="rolling")
    assert any(t.reference_segment_ids[0].startswith(DAY2) for t in roll.trajectories)
    assert _comparable(_forecast(other, pool="rolling")) != _comparable(roll)


def test_pool_build_is_deterministic_without_the_cache(store):
    cfg = RetrievalConfig()
    rr._POOL_CACHE.clear()
    a = rr.load_pool(store, "lanes", _manifest(), TRAIN_END, cfg)
    rr._POOL_CACHE.clear()
    b = rr.load_pool(store, "lanes", _manifest(), TRAIN_END, cfg)
    assert a is not b
    for name in a.__dataclass_fields__:
        x, y = getattr(a, name), getattr(b, name)
        assert (
            np.array_equal(x, y, equal_nan=x.dtype.kind == "f") if hasattr(x, "dtype") else x == y
        )


def test_variants_are_diagnostic_only_and_class_is_labelled_backfilled(store):
    assert not _model(store).config["diagnostic_only"]
    assert _model(store, pool="rolling").config["diagnostic_only"]
    assert _model(store, index_days=1).config["diagnostic_only"]
    assert _model(store).config["class_source"] == "backfilled"
    ctx = ModelContext(store, "lanes", protocol="P-unseen", params={"manifest": _manifest()})
    assert not get_model("route_retrieval_v1", ctx).capabilities().installed  # mismatch
