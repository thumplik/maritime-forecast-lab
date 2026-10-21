"""Retrieval segment index: causality and protocol eligibility (SYNTHETIC data only)."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from fixtures.synthetic import CSV_PATH, GAPS, MOVING, TURNING

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.data.segment_index import build_segment_index, eligible, pool_until
from maritime_forecast.schemas.manifest import (
    Box,
    Period,
    RegionSplit,
    SplitManifest,
    VesselAssignment,
)
from maritime_forecast.storage.parquet_store import ParquetStore

T0 = datetime(2023, 6, 1, tzinfo=UTC)
HEADER = "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselType,Status,Length,Width,Draft,TransceiverClass"
ADV_A, ADV_B = "999000101", "999000102"


def _row(mmsi, minute, lat, lon, sog=10):
    t = T0 + timedelta(minutes=minute)
    return f"{mmsi},{t:%Y-%m-%dT%H:%M:%S},{lat},{lon},{sog},0,511,70,0,200,30,9,A"


def _adversarial_csv(path):
    """SYNTHETIC tracks built to break naive causality:
    A: 00:00-01:00 every minute; a spike r at 01:20 (1° away); n at 01:45 back near A's end.
       In the full archive r is a spike (both neighbours far), so A ends at 01:00. Seen at
       01:30, r has no successor yet, is kept, and A seems to continue to 01:20.
    B: 00:00-01:00, its last report 30 km off its previous one; the next report is at 03:00,
       far away. Without causal_ends, that report (after the gap) would decide that B's last
       report is a spike."""
    rows = [HEADER]
    rows += [_row(ADV_A, k, 29.0 + k * 3e-3, -94.5) for k in range(61)]
    rows += [_row(ADV_A, 80, 30.2, -94.5), _row(ADV_A, 105, 29.18, -94.5)]
    rows += [_row(ADV_A, 105 + k, 29.18 + k * 3e-3, -94.5) for k in range(1, 30)]
    rows += [_row(ADV_B, k, 28.5 + k * 3e-3, -95.0) for k in range(60)]
    rows += [_row(ADV_B, 60, 28.5 + 59 * 3e-3 + 0.27, -95.0)]  # 30 km jump in 1 min
    rows += [_row(ADV_B, 180 + k, 26.0, -90.0) for k in range(10)]
    path.write_text("\n".join(rows) + "\n")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    adv = tmp_path / "SYNTHETIC_adversarial.csv"
    _adversarial_csv(adv)
    s = ParquetStore(tmp_path / "canonical")
    ingest([CSV_PATH, adv], load_mapping("noaa_marinecadastre"), "syn", store=s)
    return s


P_KNOWN = SplitManifest(
    protocol="P-known", dataset_id="syn", description="SYNTHETIC",
    periods=[Period(split="test", start=T0, end=T0 + timedelta(days=1))],
)  # fmt: skip
TRAIN_END = T0 + timedelta(hours=3)
P_KNOWN_TRAIN = SplitManifest(
    protocol="P-known", dataset_id="syn", description="SYNTHETIC",
    periods=[Period(split="train", start=T0, end=TRAIN_END),
             Period(split="test", start=TRAIN_END + timedelta(hours=1), end=T0 + timedelta(days=1))],
)  # fmt: skip


def _norm(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort("segment_id")


def _as_ofs():
    return [T0 + timedelta(minutes=m) for m in range(20, 7 * 60, 5)]


def test_eligible_from_full_archive_equals_eligible_from_data_up_to_as_of(store):
    full = build_segment_index(store, "syn")
    checked = 0
    for as_of in _as_ofs():
        truncated = build_segment_index(store, "syn", end=as_of + timedelta(microseconds=1))
        for m, pool in ((P_KNOWN, "rolling"), (P_KNOWN_TRAIN, "frozen")):
            a = _norm(eligible(full, as_of, "999999999", m, pool))
            b = _norm(eligible(truncated, as_of, "999999999", m, pool))
            assert a.equals(b), (as_of, pool, a["segment_id"].to_list(), b["segment_id"].to_list())
            checked += a.height
    assert checked > 50  # not vacuous: many eligible segments were compared


def test_the_equivalence_test_has_teeth(store):
    """With the naive rule (known one gap after the end) the adversarial spike leaks."""
    full = build_segment_index(store, "syn")
    naive = full.with_columns(known_at=pl.col("end_t") + timedelta(minutes=30))
    as_of = T0 + timedelta(minutes=90)
    truncated = build_segment_index(store, "syn", end=as_of + timedelta(microseconds=1))
    a = eligible(naive, as_of, "x", P_KNOWN, "rolling").filter(pl.col("vessel_id") == ADV_A)
    b = eligible(
        truncated.with_columns(known_at=pl.col("end_t") + timedelta(minutes=30)), as_of, "x",
        P_KNOWN, "rolling",
    ).filter(pl.col("vessel_id") == ADV_A)  # fmt: skip
    assert a.height == 1 and b.height == 0  # the future decided A's end under the naive rule


def test_without_causal_ends_the_equivalence_breaks(store):
    """Teeth for causal_ends: B's last report is judged by a report after the gap."""
    as_of = T0 + timedelta(minutes=120)
    full = build_segment_index(store, "syn", _causal_ends=False)
    trunc = build_segment_index(store, "syn", end=as_of + timedelta(microseconds=1),
                                _causal_ends=False)  # fmt: skip
    a = eligible(full, as_of, "x", P_KNOWN, "rolling").filter(pl.col("vessel_id") == ADV_B)
    b = eligible(trunc, as_of, "x", P_KNOWN, "rolling").filter(pl.col("vessel_id") == ADV_B)
    assert a["end_t"].to_list() != b["end_t"].to_list()


def test_segment_end_is_never_judged_across_a_gap(store):
    full = build_segment_index(store, "syn")
    b = full.filter(pl.col("vessel_id") == ADV_B).sort("start_t")
    assert b["end_t"][0] == T0 + timedelta(minutes=60)  # kept: its successor is 2 h later


def test_known_at_boundary(store):
    full = build_segment_index(store, "syn")
    seg = full.filter(pl.col("vessel_id") == MOVING).row(0, named=True)
    assert seg["known_at"] == seg["end_t"] + timedelta(minutes=60)
    ids = lambda t: set(eligible(full, t, "x", P_KNOWN, "rolling")["segment_id"])  # noqa: E731
    assert seg["segment_id"] not in ids(seg["known_at"] - timedelta(seconds=1))
    assert seg["segment_id"] in ids(seg["known_at"])


def test_tracks_stay_inside_their_segment_and_never_bridge_gaps(store):
    full = build_segment_index(store, "syn")
    gaps = full.filter(pl.col("vessel_id") == GAPS)
    assert gaps.height >= 2  # the GAPS vessel is split at its gaps
    for r in full.iter_rows(named=True):
        t = r["track_t"]
        assert t[0] == r["start_t"] and t[-1] <= r["end_t"]
        assert all(b - a == timedelta(minutes=10) for a, b in zip(t, t[1:], strict=False))
        assert r["track_complete"] and len(r["track_lat"]) == len(t)
        assert all(r["lat_min"] - 1e-9 <= x <= r["lat_max"] + 1e-9 for x in r["track_lat"])
    starts = gaps.sort("start_t")
    for a, b in zip(starts.iter_rows(named=True), starts[1:].iter_rows(named=True), strict=False):
        assert b["start_t"] - a["end_t"] > timedelta(minutes=30)
        assert a["track_t"][-1] <= a["end_t"] < b["track_t"][0]


def test_p_known_allows_the_targets_own_earlier_voyages(store):
    full = build_segment_index(store, "syn")
    late = T0 + timedelta(hours=7)
    e = eligible(full, late, MOVING, P_KNOWN, "rolling")
    assert MOVING in set(e["vessel_id"])
    # the target's segment still in progress at as_of is never eligible
    mid = T0 + timedelta(minutes=90)
    own = eligible(full, mid, MOVING, P_KNOWN, "rolling").filter(pl.col("vessel_id") == MOVING)
    assert own.height == 0
    assert (eligible(full, late, MOVING, P_KNOWN, "rolling")["known_at"] <= late).all()


def test_frozen_pool_uses_only_segments_known_by_the_train_end(store):
    full = build_segment_index(store, "syn")
    late = T0 + timedelta(hours=7)  # a test-period cutoff
    assert pool_until(late, P_KNOWN_TRAIN) == TRAIN_END
    frozen = eligible(full, late, "x", P_KNOWN_TRAIN)
    rolling = eligible(full, late, "x", P_KNOWN_TRAIN, "rolling")
    assert frozen.height and (frozen["known_at"] <= TRAIN_END).all()
    assert rolling.height > frozen.height  # segments completed after the train end exist
    early = T0 + timedelta(hours=2)  # a train-period cutoff: frozen is causal at as_of
    assert (eligible(full, early, "x", P_KNOWN_TRAIN)["known_at"] <= early).all()
    with pytest.raises(ValueError, match="no train period"):
        eligible(full, late, "x", P_KNOWN)  # a frozen pool needs a train period


def test_vessel_disjoint_protocols_use_only_train_vessels_and_never_the_target(store):
    full = build_segment_index(store, "syn")
    va = next(
        v for v in (VesselAssignment(salt=f"s{i}", buckets={"train": (0, 500), "test": (500, 1000)})
                    for i in range(200))
        if v.assign(TURNING) == "test" and v.assign(MOVING) == "train" and v.assign(GAPS) == "train"
    )  # fmt: skip
    m = SplitManifest(
        protocol="P-unseen", dataset_id="syn", description="SYNTHETIC", vessel_assignment=va,
        periods=[Period(split="train", start=T0, end=T0 + timedelta(hours=8)),
                 Period(split="test", start=T0 + timedelta(hours=9), end=T0 + timedelta(days=1))],
    )  # fmt: skip
    late = T0 + timedelta(hours=7)
    e = eligible(full, late, TURNING, m)
    got = set(e["vessel_id"])
    assert got and all(va.assign(v) == "train" for v in got) and TURNING not in got
    # a train-assigned target (e.g. a calibration-split cutoff) is excluded as well
    assert GAPS not in set(eligible(full, late, GAPS, m)["vessel_id"])
    assert MOVING in got


def test_class_is_deterministic_and_provenance_is_carried(store):
    a = build_segment_index(store, "syn")
    b = build_segment_index(store, "syn")
    assert a.equals(b)
    assert set(a["static_fields_provenance"]) == {"backfilled"}
    assert a["vessel_class"].null_count() == 0


def test_p_region_keeps_only_segments_wholly_beyond_the_buffer(store):
    full = build_segment_index(store, "syn")
    rs = RegionSplit(
        test_region=Box(name="t", lat_min=29.1, lat_max=31, lon_min=-100, lon_max=-80),
        buffer_km=5,
    )
    m = SplitManifest(
        protocol="P-region", dataset_id="syn", description="SYNTHETIC",
        periods=[Period(split="test", start=T0, end=T0 + timedelta(days=1))], region_split=rs,
    )  # fmt: skip
    as_of = T0 + timedelta(hours=7)
    got = eligible(full, as_of, MOVING, m, "rolling")
    known = full.filter(pl.col("known_at") <= as_of)
    a, b, c, d = rs.outer
    clear = (pl.col("lat_max") < a) | (pl.col("lat_min") > b)
    clear = clear | (pl.col("lon_max") < c) | (pl.col("lon_min") > d)
    assert got.height and got.height < known.height  # not vacuous either way
    assert got.sort("segment_id").equals(known.filter(clear).sort("segment_id"))
    # Every track point of an eligible segment is on the train side, none in the buffer.
    for la, lo in zip(got["track_lat"], got["track_lon"], strict=True):
        assert all(rs.side(x, y) == "train" for x, y in zip(la, lo, strict=True) if x is not None)
