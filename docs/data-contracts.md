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
| `vessel_class_from` | `ais_code` (numeric ship type, table below) or `text_map` with `vessel_class_text_map` |
| `static_fields_provenance` | `broadcast` / `backfilled` / `unknown`, stamped on every row |
| `verified` | true only after the mapping was checked against a real provider file |

Presets: `noaa_marinecadastre` (verified on June 2023) and `dma_denmark` (Danish Maritime Authority, **unverified**). Import any mapped file with `scripts/import_ais.py`.

### Row accounting

Every ingest produces an `IngestReport` in which `rows_in = dropped (by reason) + duplicates_removed + outside_region + rows_out`. Ingestion fails if the counts do not balance.

- Drop reasons, first match wins: `missing_vessel_id`, `bad_timestamp`, `bad_coordinates`, `null_island` (0, 0).
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
