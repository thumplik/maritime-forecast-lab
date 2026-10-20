"""Ingestion through source mappings, on SYNTHETIC files (not real AIS)."""

from datetime import UTC, datetime

import duckdb
import pytest
from pydantic import ValidationError

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.config import BBox
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import SourceMapping, load_mapping
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.storage.parquet_store import ParquetStore

GENERIC = SourceMapping(
    source_id="synthetic_provider",
    version=1,
    columns={
        "vessel_id": "ship", "observed_at": "time", "latitude_deg": "lat",
        "longitude_deg": "lon", "sog": "speed_ms", "cog_deg": "course",
        "transceiver_class": "kind", "vessel_type": "type", "destination": "dest",
    },
    timestamp={"timezone": "America/Chicago"},
    sog_unit="mps",
    sog_invalid_at_or_above=50,
    null_values={"vessel_type": ["Undefined"]},
    value_maps={"transceiver_class": {"Class A": "A", "Class B": "B"}},
    vessel_class_from="text_map",
    vessel_class_text_map={"Cargo": "cargo", "Tug": "tug_tow"},
    static_fields_provenance="broadcast",
)  # fmt: skip

CSV = """ship;time;lat;lon;speed_ms;course;kind;type;dest
367000001;2023-06-01 07:00:00;29.30;-94.70;5.144444;90;Class A;Cargo;HOUSTON
367000001;2023-06-01T12:00:00Z;29.31;-94.69;5.144444;90;Class A;cargo;HOUSTON
367000001;2023-06-01T12:00:00Z;29.31;-94.69;5.144444;90;Class A;cargo;HOUSTON
367000002;2023-06-01 18:30:00;29.00;-95.00;99;400;Base Station;Undefined;
367000002;not-a-time;29.00;-95.00;1;10;Class B;Tug;
;2023-06-01 08:00:00;29.00;-95.00;1;10;Class B;Tug;
367000003;2023-06-01 08:00:00;0;0;1;10;Class B;Tug;
367000004;2023-06-01 08:00:00;95;-95;1;10;Class B;Tug;
367000005;2023-06-01 08:00:00;40.0;-70.0;1;10;Class B;Tug;
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "manifests")
    f = tmp_path / "in.csv"
    f.write_text(CSV)
    return (
        ParquetStore(tmp_path / "canonical"),
        f,
        GENERIC.model_copy(update={"csv_delimiter": ";"}),
    )


def test_generic_csv_rules_and_accounting(env):
    store, f, m = env
    box = BBox("gulf", 23.5, 31.0, -98.0, -81.0)
    report, written = ingest([f], m, "syn", store=store, region=box)
    assert report.rows_in == 9
    assert report.dropped == {"bad_coordinates": 1, "bad_timestamp": 1, "missing_vessel_id": 1,
                              "null_island": 1}  # fmt: skip
    # "07:00 Chicago" and the two "12:00Z" rows are one instant: 2 duplicates removed
    assert report.duplicates_removed == 2 and report.outside_region == 1 and report.rows_out == 2
    # 18:30 Chicago (CDT, -5) is 23:30 UTC, still 2023-06-01
    assert written == {"2023-06-01": 2}
    a1, b = store.scan("syn").pl().to_dicts()
    assert a1["observed_at"] == datetime(2023, 6, 1, 12, 0, tzinfo=UTC)  # 07:00 CDT
    assert a1["sog_knots"] == pytest.approx(10.0, rel=1e-6)  # 5.144 m/s
    assert a1["transceiver_class"] == "A" and a1["vessel_class"] == "cargo"
    assert a1["destination"] == "HOUSTON" and a1["static_fields_provenance"] == "broadcast"
    assert b["observed_at"] == datetime(2023, 6, 1, 23, 30, tzinfo=UTC)
    assert b["sog_knots"] is None and b["cog_deg"] is None  # 99 m/s >= 50 sentinel; 400 deg
    assert b["vessel_type"] is None and b["vessel_class"] == "unknown"
    assert b["transceiver_class"] is None
    assert set(b["quality_flags"]) == {"sog_sentinel", "bad_transceiver_class"}
    assert report.flags == {"bad_transceiver_class": 1, "sog_sentinel": 1}


def test_explicit_offset_honoured_over_declared_timezone(env):
    store, f, m = env
    ingest([f], m, "syn", store=store, write_manifest=False)
    t = store.scan("syn", vessel_ids=["367000001"]).pl()["observed_at"].to_list()
    # "07:00 Chicago" and "12:00Z" are the same instant: deduplicated to one row
    assert t == [datetime(2023, 6, 1, 12, 0, tzinfo=UTC)]


def test_parquet_epoch_ms(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "manifests")
    p = tmp_path / "in.parquet"
    duckdb.sql(
        "COPY (SELECT 367000001 AS mmsi, 1685577600000 AS ts_ms, 29.3 AS y, -94.7 AS x, 12.0 AS sog) "
        f"TO '{p}' (FORMAT parquet)"
    )
    m = SourceMapping(
        source_id="syn_parquet", version=1, file_format="parquet",
        columns={"vessel_id": "mmsi", "observed_at": "ts_ms", "latitude_deg": "y",
                 "longitude_deg": "x", "sog": "sog"},
        timestamp={"epoch_unit": "ms"},
    )  # fmt: skip
    store = ParquetStore(tmp_path / "c")
    _, written = ingest([p], m, "pq", store=store)
    row = store.scan("pq").pl().row(0, named=True)
    assert written == {"2023-06-01": 1}
    assert row["observed_at"] == datetime(2023, 6, 1, tzinfo=UTC) and row["sog_knots"] == 12.0
    assert row["static_fields_provenance"] == "unknown" and row["vessel_class"] == "unknown"


def test_wrong_column_count_is_an_error_not_a_skip(env):
    store, f, m = env
    f.write_text(CSV + "367000009;2023-06-01 08:00:00;29.0;-95.0;1;10;Class B;Tug;X;EXTRA\n")
    with pytest.raises(duckdb.Error):
        ingest([f], m, "syn", store=store)


def test_unparseable_value_is_dropped_and_counted(env):
    store, f, m = env
    f.write_text(CSV + '367000009;2023-06-01 08:00:00;"29.0;-95.0;1;10;Class B;Tug;\n')
    report, _ = ingest([f], m, "syn", store=store, write_manifest=False)
    assert report.rows_in == 10 and report.dropped["bad_coordinates"] == 2


def test_expect_days_refuses_spill_without_touching_existing(env):
    store, f, m = env
    ingest([f], m, "syn", store=store, write_manifest=False)
    before = store.scan("syn").pl()
    f.write_text(CSV.replace("2023-06-01 18:30:00", "2023-06-02 18:30:00"))
    with pytest.raises(ValueError, match="unexpected days"):
        ingest([f], m, "syn", store=store, expect_days={"2023-06-01"}, write_manifest=False)
    assert store.scan("syn").pl().equals(before)
    assert store.partitions("syn") == [datetime(2023, 6, 1).date()]


def test_manifest_accumulates_and_refuses_mixed_mappings(env, tmp_path):
    store, f, m = env
    ingest([f], m, "syn", store=store)
    ingest([f], m, "syn", store=store)  # re-ingest replaces the input entry
    mf = DatasetManifest.model_validate_json(
        (tmp_path / "manifests" / "syn.canonical.json").read_text()
    )
    assert len(mf.inputs) == 1 and mf.inputs[0].sha256 and mf.partitions == {"2023-06-01": 3}
    assert mf.mapping_id == "synthetic_provider@v1" and mf.static_fields_provenance == "broadcast"
    other = m.model_copy(update={"version": 2})
    with pytest.raises(ValueError, match="refusing to mix"):
        ingest([f], other, "syn", store=store)


def test_wrong_format_rejected(env):
    store, f, m = env
    with pytest.raises(ValueError, match="expected parquet"):
        ingest([f], m.model_copy(update={"file_format": "parquet"}), "syn", store=store)


@pytest.mark.parametrize(
    "bad",
    [
        {"columns": {"vessel_id": "a", "observed_at": "b", "latitude_deg": "c"}},  # missing lon
        {"columns": {**GENERIC.columns, "speed": "x"}},  # unknown canonical field
        {"timestamp": {"timezone": "UTC'; DROP TABLE x; --"}},
        {"vessel_class_from": "text_map", "vessel_class_text_map": {}},
        {"source_id": "Bad Id"},
    ],
)
def test_mapping_validation(bad):
    with pytest.raises(ValidationError):
        SourceMapping.model_validate(GENERIC.model_dump() | bad)


def test_presets_load():
    noaa = load_mapping("noaa_marinecadastre")
    assert noaa.verified and noaa.static_fields_provenance == "backfilled"
    dma = load_mapping("dma_denmark")
    assert not dma.verified and dma.vessel_class_from == "text_map"
    with pytest.raises(ValueError):
        load_mapping("../../etc/passwd")


def test_column_names_are_quoted(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MANIFEST_DIR", tmp_path / "m")
    f = tmp_path / "x.csv"
    f.write_text('# Timestamp,MMSI,Lat "deg",Lon\n01/06/2023 00:00:00,367000001,29.3,-94.7\n')
    m = SourceMapping(
        source_id="quoted", version=1,
        columns={"vessel_id": "MMSI", "observed_at": "# Timestamp", "latitude_deg": 'Lat "deg"',
                 "longitude_deg": "Lon"},
        timestamp={"format": "%d/%m/%Y %H:%M:%S"},
    )  # fmt: skip
    report, _ = ingest([f], m, "q", store=ParquetStore(tmp_path / "c"))
    assert report.rows_out == 1
