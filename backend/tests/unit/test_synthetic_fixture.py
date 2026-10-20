"""The SYNTHETIC fixture through the whole pipeline: ingest, store, segmentation, sampler,
truth, baselines, API. Not real AIS (see tests/fixtures/README.md)."""

from datetime import timedelta

import duckdb
import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient
from fixtures.synthetic import (
    ANTIMERIDIAN,
    CSV_PATH,
    DIRTY,
    GAP_MINUTES,
    GAPS,
    MOVING,
    STOPPING,
    T0,
    TURNING,
    csv_text,
)

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.api.app import ApiSettings, create_app
from maritime_forecast.data.cutoffs import SamplerConfig, sample_cutoffs
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.data.segment import gap_segments, segmented_sql
from maritime_forecast.evaluation.truth import truth_sql
from maritime_forecast.evaluation.windows import grid_sql
from maritime_forecast.geo.geodesic import distance_m
from maritime_forecast.models.baselines import constant_velocity
from maritime_forecast.storage.parquet_store import ParquetStore


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("syn")
    mp = pytest.MonkeyPatch()
    mp.setattr(ingest_mod, "MANIFEST_DIR", tmp / "manifests")
    store = ParquetStore(tmp / "canonical")
    report, _ = ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=store)
    obs = store.scan("syn")
    con = store.con
    con.register("obs_df", obs.to_arrow())
    con.execute("CREATE TABLE obs AS SELECT * FROM obs_df")
    seg = con.execute(f"{segmented_sql('obs')} ORDER BY vessel_id, observed_at").pl()
    yield {"tmp": tmp, "store": store, "report": report, "obs": obs, "seg": seg, "con": con}
    mp.undo()


def track(df: pl.DataFrame, vessel: str) -> pl.DataFrame:
    return df.filter(pl.col("vessel_id") == vessel).sort("observed_at")


def test_committed_csv_matches_generator():
    assert CSV_PATH.read_text() == csv_text()


def test_ingest_accounts_for_dirty_rows(env):
    r = env["report"]
    assert r.rows_in == 521
    assert r.dropped == {"bad_coordinates": 1} and r.duplicates_removed == 1
    assert r.rows_out == 519 and r.flags == {"sog_sentinel": 1}
    dirty = track(env["obs"], DIRTY)
    assert dirty["observed_at"].is_sorted()  # the out-of-order row is in place
    assert dirty.height == 19 and dirty["heading_deg"].null_count() == 0  # 180 kept
    assert track(env["obs"], MOVING)["heading_deg"].to_list()[0] == 45.0
    assert track(env["obs"], ANTIMERIDIAN)["longitude_deg"].is_between(-180, 180).all()


def test_gaps_split_segments_and_are_never_filled(env):
    g = track(env["obs"], GAPS)
    seg = gap_segments(g["observed_at"].to_list(), 30)
    assert max(seg) == 2  # three segments
    dts = g["observed_at"].diff().drop_nulls()
    long = sorted(int(d.total_seconds() // 60) for d in dts if d > timedelta(minutes=30))
    assert long == [GAP_MINUTES[0] + 1, GAP_MINUTES[1] + 1]
    # segmentation SQL agrees with the display segmentation
    assert track(env["seg"], GAPS)["segment_no"].n_unique() == 3


def test_spike_removed_and_antimeridian_not_a_spike(env):
    assert track(env["seg"], DIRTY).height == 18  # the 55 km one-report spike is dropped
    am = track(env["seg"], ANTIMERIDIAN)
    assert am.height == 61 and am["segment_no"].n_unique() == 1


def test_motion_states(env):
    s = track(env["seg"], STOPPING)
    assert (
        s.filter(pl.col("observed_at") >= T0 + timedelta(minutes=52))["sog_mean_10m"] < 0.5
    ).all()
    assert (track(env["seg"], MOVING)["sog_mean_10m"] >= 2).all()
    cand = env["seg"].filter(pl.col("n_reports_3h") >= 6)
    stat = sample_cutoffs(cand, SamplerConfig(motion_set="stationary", min_spacing_hours=0.1))
    assert set(stat["vessel_id"]) == {STOPPING}
    assert stat["observed_at"].min() >= T0 + timedelta(minutes=40)


def test_history_is_causal_on_fixture(env):
    as_of = T0 + timedelta(minutes=30, seconds=30)
    h = env["store"].history("syn", TURNING, as_of, hours=1)
    assert h.height == 31 and h["observed_at"].max() <= as_of


def test_truth_interpolates_across_antimeridian(env):
    con = env["con"]
    t = T0 + timedelta(minutes=12, seconds=30)  # halfway between 179.997E and 179.999W
    con.execute(f"CREATE OR REPLACE TABLE tg AS SELECT 1 AS cutoff_id, '{ANTIMERIDIAN}' AS vessel_id, "
                f"TIMESTAMPTZ '{t.isoformat()}' AS target_t")  # fmt: skip
    lat, lon = con.execute(
        f"SELECT truth_lat, truth_lon FROM ({truth_sql('tg', 'obs')})"
    ).fetchone()
    assert abs(lon) > 179.99, f"interpolated across the globe: {lon}"
    assert lat == pytest.approx(-10.0, abs=1e-4)


def test_grid_interpolates_across_antimeridian(env):
    con = env["con"]
    # keep odd minutes only, so the 1-min grid must interpolate across the crossing
    con.execute("CREATE OR REPLACE TABLE am AS SELECT *, 0 AS segment_no FROM obs "
                f"WHERE vessel_id = '{ANTIMERIDIAN}' AND minute(observed_at) % 2 = 1")  # fmt: skip
    g = con.execute(grid_sql("am", 1)).pl()
    assert g.height == 59  # 00:01 .. 00:59
    # 14 kn for 1 h is ~0.24 deg: every grid point stays within 0.25 deg of the dateline
    assert (g["lon"].abs() > 179.75).all() and g["lon"].is_between(-180, 180).all()


def test_baselines_on_fixture(env):
    mv = track(env["obs"], MOVING)
    a, b = mv.row(0, named=True), mv.row(60, named=True)
    la, lo = constant_velocity(a["latitude_deg"], a["longitude_deg"], 12.0, 45.0, [3600.0])
    # The fixture chains 60 one-minute legs each restarting at COG 45 (close to a rhumb line);
    # CV follows one geodesic. They differ by ~15 m over 22 km.
    assert distance_m(la[0], lo[0], b["latitude_deg"], b["longitude_deg"]) < 50
    tr = track(env["obs"], TURNING)
    a, b = tr.row(0, named=True), tr.row(60, named=True)
    la, lo = constant_velocity(a["latitude_deg"], a["longitude_deg"], 10.0, a["cog_deg"], [3600.0])
    assert distance_m(la[0], lo[0], b["latitude_deg"], b["longitude_deg"]) > 1000  # turns away
    am = track(env["obs"], ANTIMERIDIAN)
    a = am.row(0, named=True)
    la, lo = constant_velocity(a["latitude_deg"], a["longitude_deg"], 14.0, 90.0, [3600.0])
    z = am.row(60, named=True)
    assert lo[0] < 0 and distance_m(la[0], lo[0], z["latitude_deg"], z["longitude_deg"]) < 50


def test_api_replays_fixture_without_fabricated_points(env):
    tmp = env["tmp"]
    client = TestClient(create_app(ApiSettings(
        store_root=tmp / "canonical", manifest_dir=tmp / "manifests", runs_dir=tmp / "runs")))  # fmt: skip
    r = client.get(f"/vessels/{GAPS}/track", params={"dataset_id": "syn"}).json()
    raw = track(env["obs"], GAPS)
    assert r["n_points"] == raw.height  # nothing interpolated or invented
    assert [g["duration_s"] / 60 for g in r["gaps"]] == [GAP_MINUTES[0] + 1, GAP_MINUTES[1] + 1]
    am = client.get(f"/vessels/{ANTIMERIDIAN}/track", params={"dataset_id": "syn"}).json()
    lons = np.array(am["points"]["longitude_deg"])
    assert len(am["segments"]) == 1 and (np.abs(lons) <= 180).all()


def test_fixture_ids_are_unmistakably_synthetic():
    assert all(r.split(",")[0].startswith("999") for r in CSV_PATH.read_text().splitlines()[1:])
    _ = duckdb  # imported for the env fixture's connection type


def test_spike_distance_wraps_at_antimeridian(env):
    # A lone report across the dateline (both neighbours on the other side, 0.01 deg away)
    # must not look like a spike.
    con = env["con"]
    con.execute("""CREATE OR REPLACE TABLE am3 AS SELECT * FROM (VALUES
        ('999000099', TIMESTAMPTZ '2023-06-01 00:00:00+00', -10.0, 179.995, 10.0),
        ('999000099', TIMESTAMPTZ '2023-06-01 00:01:00+00', -10.0, -179.999, 10.0),
        ('999000099', TIMESTAMPTZ '2023-06-01 00:02:00+00', -10.0, 179.999, 10.0)
    ) t(vessel_id, observed_at, latitude_deg, longitude_deg, sog_knots)""")
    assert con.execute(f"SELECT count(*) FROM ({segmented_sql('am3')})").fetchone()[0] == 3


def test_motion_state_labels(env):
    s = env["seg"]
    assert set(track(s, MOVING)["motion_state"]) == {"underway"}
    stop = track(s, STOPPING)
    assert stop.filter(pl.col("observed_at") >= T0 + timedelta(minutes=52))[
        "motion_state"
    ].unique().to_list() == ["stationary"]
    assert {"underway", "slow", "stationary"} <= set(stop["motion_state"])


def _spread_m(df):
    lat, lon = df["latitude_deg"].to_numpy(), df["longitude_deg"].to_numpy()
    return float(np.max(distance_m(lat, lon, lat.mean(), lon.mean())))


def test_dejitter_reduces_jitter_keeps_rows_and_moving_untouched(env):
    from maritime_forecast.data.dejitter import dejitter_stationary

    out = dejitter_stationary(env["seg"])
    assert out.height == env["seg"].height

    def moored(d):
        return track(d, STOPPING).filter(pl.col("observed_at") >= T0 + timedelta(minutes=70))

    assert _spread_m(moored(env["seg"])) > 5 and _spread_m(moored(out)) < 0.5 * _spread_m(
        moored(env["seg"])
    )
    assert track(out, MOVING).select("latitude_deg", "longitude_deg").equals(
        track(env["seg"], MOVING).select("latitude_deg", "longitude_deg"))  # fmt: skip
    assert (
        track(out, STOPPING)["dejittered"].sum()
        == (track(env["seg"], STOPPING)["motion_state"] == "stationary").sum()
    )


def test_dejitter_is_causal(env):
    from maritime_forecast.data.dejitter import dejitter_stationary

    cut = T0 + timedelta(minutes=80)
    seg = track(env["seg"], STOPPING)
    perturbed = seg.with_columns(
        latitude_deg=pl.when(pl.col("observed_at") > cut).then(pl.col("latitude_deg") + 0.01)
        .otherwise(pl.col("latitude_deg")))  # fmt: skip
    a = dejitter_stationary(seg).filter(pl.col("observed_at") <= cut)
    b = dejitter_stationary(perturbed).filter(pl.col("observed_at") <= cut)
    assert a.select("latitude_deg", "longitude_deg").equals(
        b.select("latitude_deg", "longitude_deg")
    )


def test_dejitter_antimeridian_mooring():
    from maritime_forecast.data.dejitter import dejitter_stationary

    df = pl.DataFrame({
        "vessel_id": ["999000098"] * 3, "segment_no": [1] * 3,
        "observed_at": [T0 + timedelta(minutes=k) for k in range(3)],
        "latitude_deg": [-10.0] * 3, "longitude_deg": [179.9999, -179.9999, 179.9999],
        "motion_state": ["stationary"] * 3,
    })  # fmt: skip
    lons = dejitter_stationary(df)["longitude_deg"].to_list()
    assert all(abs(abs(x) - 180) < 1e-3 for x in lons)  # never averaged to ~0


def test_dejitter_prescribed_path_ignores_post_cutoff_spike(env):
    """segment(obs <= as_of) -> dejitter is invariant to anything after as_of, including a
    position spike right after the cutoff (which the full-archive path would react to)."""
    from maritime_forecast.data.dejitter import dejitter_stationary

    con = env["con"]
    as_of = T0 + timedelta(minutes=90)
    stop = track(env["obs"], STOPPING)

    def prescribed(obs: pl.DataFrame) -> pl.DataFrame:
        con.register("ctx_df", obs.filter(pl.col("observed_at") <= as_of).to_arrow())
        seg = con.execute(f"{segmented_sql('ctx_df')} ORDER BY observed_at").pl()
        return dejitter_stationary(seg).select("observed_at", "latitude_deg", "longitude_deg")

    spiked = stop.with_columns(
        latitude_deg=pl.when(pl.col("observed_at") == as_of + timedelta(minutes=1))
        .then(pl.col("latitude_deg") + 0.5).otherwise(pl.col("latitude_deg")))  # fmt: skip
    assert prescribed(stop).equals(prescribed(spiked))
