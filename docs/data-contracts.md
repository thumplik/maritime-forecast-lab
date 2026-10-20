# Data contracts

Canonical observation schema: build plan §4. The executable contract is `maritime_forecast.schemas.Observation`. A unit test asserts that canonical Parquet columns match its fields in order, and that a canonical row validates. This file records the concrete mapping and decisions.

## Source mappings (any provider → canonical)

Each provider is described by a versioned **source mapping** in `config/sources/<source_id>.yaml`, validated by `maritime_forecast.data.mapping.SourceMapping`. Ingestion (`data/ingest.py`) applies one rule set to every source, so adding a provider means writing YAML, not code.

| Mapping key | Meaning |
|---|---|
| `columns` | canonical field → source column. Required: `vessel_id`, `observed_at`, `latitude_deg`, `longitude_deg`. Optional: `sog`, `cog_deg`, `heading_deg`, `nav_status`, `vessel_type`, `length_m`, `width_m`, `draft_m`, `transceiver_class`, `destination`, `eta` |
| `file_format`, `csv_delimiter` | `csv` (read as text, so parsing follows the mapping) or `parquet` |
| `timestamp` | `timezone` for naive values (an explicit offset in the value always wins), or a strptime `format`, or `epoch_unit` `s`/`ms` |
| `sog_unit`, `sog_invalid_at_or_above` | `knots`/`mps`/`kmh`. Speeds at or above the threshold (in source units) are "not available" |
| `null_values` | per-field strings meaning "not available" (AIS default: nav_status `15`, vessel_type `0`) |
| `value_maps` | per-field value translation, e.g. `Class A` → `A` |
| `drop_values` | rows whose `transceiver_class`, `nav_status` or `vessel_type` equals a listed value are dropped and counted as `excluded_by_mapping` (e.g. base stations) |
| `vessel_class_from` | `ais_code` (numeric ship type, table below) or `text_map` with `vessel_class_text_map` |
| `static_fields_provenance` | `broadcast` / `backfilled` / `unknown`, stamped on every row |
| `verified` | true only after the mapping was checked against a real provider file |

Presets: `noaa_marinecadastre` (verified on June 2023) and `dma_denmark` v2 (Danish Maritime Authority, verified on 2023-06-01; see below). Import any mapped file with `scripts/import_ais.py`.

### Row accounting

Every ingest produces an `IngestReport` in which `rows_in = dropped (by reason) + duplicates_removed + outside_region + rows_out`. Ingestion fails if the counts do not balance.

- Drop reasons, first match wins: `missing_vessel_id`, `bad_timestamp`, `bad_coordinates`, `null_island` (0, 0), `excluded_by_mapping`.
- A CSV line with the wrong field count is an error. A value that cannot be parsed becomes NULL and the row is dropped or the field nulled, and either way it is counted.
- Quality flags: `bad_mmsi`, `sog_implausible` (> 60 kn), `sog_sentinel`, `bad_transceiver_class`.

## NOAA MarineCadastre → canonical

| Canonical | NOAA column | Rule |
|---|---|---|
| `vessel_id` | `MMSI` | string, 9 digits; rows with non-9-digit MMSI flagged `bad_mmsi` |
| `observed_at` | `BaseDateTime` | NOAA timestamps are UTC without offset → stored as `TIMESTAMP WITH TIME ZONE` UTC |
| `latitude_deg` / `longitude_deg` | `LAT` / `LON` | out of range → row dropped |
| `sog_knots` | `SOG` | 102.3 (not available) or < 0 → null; > 60 kn flagged `sog_implausible` |
| `cog_deg` | `COG` | 360 (not available) or outside [0, 360) → null |
| `heading_deg` | `Heading` | 511 (not available) or outside [0, 360) → null |
| `nav_status` | `Status` | integer code as string; 15 (undefined) → null |
| `vessel_type` | `VesselType` | integer code as string; 0 → null. Coarse class in `vessel_class` |
| `length_m`, `width_m`, `draft_m` | `Length`, `Width`, `Draft` | ≤ 0 → null |
| `transceiver_class` | `TransceiverClass` | `A` / `B` |
| `source` | — | `noaa_marinecadastre` |
| `static_fields_provenance` | — | always `backfilled` for NOAA (see §4 notes) |
| `quality_flags` | — | list of flag strings |

`destination` and `eta` are absent from NOAA point data and are null.

## Vessel classes (from AIS ship-type codes)

| Class | Codes |
|---|---|
| `fishing` | 30 |
| `tug_tow` | 21, 22, 31, 32, 52 |
| `passenger` | 60–69 |
| `cargo` | 70–79 |
| `tanker` | 80–89 |
| `pleasure_sail` | 36, 37 |
| `service` | 33–35, 50, 51, 53–59 |
| `hsc_wig` | 20, 23–29, 40–49 |
| `other` / `unknown` | 90–99 / null or 0 |

## Duplicates

Exact duplicates on (`vessel_id`, `observed_at`) keep the first row by input order and set no flag (they are NOAA/receiver duplicates, not information).

## DMA verification (2026-10-20)

Source file: `aisdk-2023-06-01.csv` from the monthly zip `2023/aisdk-2023-06.zip`, fetched with `scripts/fetch_dma.py` (range-read member, CRC32 `0x0e8cd484`, SHA-256 `b41f6ab4…243897`, 2.05 GB, 11,809,593 rows; `data/manifests/dma_fetch.json`). Ingested as dataset `dma_2023-06` with mapping `dma_denmark@v2`.

| Check | Result |
|---|---|
| Header | Matches the mapping exactly (26 columns, `# Timestamp` first) |
| Timestamp / ETA format | `dd/mm/YYYY HH:MM:SS`. All 11.8 M timestamps parse and span 00:00:00–23:59:58. 6.88 M ETAs parse |
| Row accounting | 11,809,593 in = 58,833 `bad_coordinates` (lat 91, lon 0 "not available") + 525,273 `excluded_by_mapping` + 117,345 duplicates + 11,108,142 out |
| Non-vessel stations | 550,920 rows (Base Station 473,687, AtoN 74,691, SAR Airborne 2,520, SART 20, EPIRB 2). 25,647 of them drop first as bad coordinates, the remaining 525,273 as `excluded_by_mapping` |
| Transceiver | Only `A` (10.0 M) and `B` (1.1 M) remain. No `bad_transceiver_class` flags |
| SOG | 102.2 is the "not available" value here (7,679 rows, nulled and flagged). 2,367 rows exceed 60 kn and are flagged |
| Heading | No 511. Values of 360 or more (up to 510) are nulled by the range rule |
| Ship type | All 25 labels are mapped. `Undefined` → unknown. `Spare 1/2` and `Not party to conflict` → other (added in v2) |
| Units and timing | Median implied speed between consecutive reports over reported SOG is **1.002** (Class A) and **0.995** (Class B), for underway vessels 5–40 kn with reports 5–120 s apart |
| Positions | 1st–99th percentile lat 53.9–57.9, lon 4.3–15.5 (Danish waters) |
| Cadence | Median report interval is 10 s (Class A) and 30 s (Class B). DMA is **not** one-minute down-sampled like NOAA, so resample before comparing sources |
| Static fields | Broadcast (AIS message 5), so `static_fields_provenance = broadcast` |

The v1 mapping was wrong in three ways, all fixed in v2:

1. It kept 525 k non-vessel rows.
2. It left ETA unmapped.
3. It missed three ship-type labels.

Not verified: that DMA timestamps are UTC. DMA documents them as UTC, and the speed check proves internal consistency but cannot detect a constant offset. Mapping coverage is checked for 2023 data only; check a later year's daily file before using one.
