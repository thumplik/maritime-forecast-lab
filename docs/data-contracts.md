# Data contracts

Canonical observation schema: build plan §4. This file records the concrete mapping and decisions.

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
