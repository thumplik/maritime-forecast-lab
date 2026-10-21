# data/

Everything here except this file, `.gitkeep`s and `manifests/*.json` is gitignored.

| Path | Contents | Produced by |
|---|---|---|
| `raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet` | NOAA daily points, bbox-filtered, original columns typed | `scripts/fetch_noaa.py` |
| `processed/canonical/<dataset_id>/date=YYYY-MM-DD/part-0.parquet` | Canonical §4 observations, one partition per UTC day | `scripts/canonicalize.py`, `scripts/import_ais.py` |
| `processed/segmented/<dataset_id>/bucket-NN.parquet` | Segmented observations + motion features (16 vessel-hash buckets) | `scripts/coverage_audit.py` |
| `processed/segment_index/<dataset_id>/segments.parquet` | Retrieval segment index: completed segments with 10-min tracks (sidecar in `manifests/segment_index/`) | `scripts/build_segment_index.py` (`make index`) |
| `manifests/*.json` | Fetch manifests (source URLs, SHA-256, filter) and `<dataset_id>.canonical.json` dataset manifests (mapping, inputs with SHA-256 and ingest report, rows per day). Committed | fetch / canonicalize / import |
| `manifests/splits/*.json` | Protocol split manifests (committed) | `scripts/make_splits.py` |
| `runs/<run_id>/` | Audit and benchmark outputs with JSON sidecars | audit / benchmark scripts |

## Sources and licensing

- **NOAA MarineCadastre AccessAIS** (U.S. Coast Guard terrestrial AIS, one-minute filtered). U.S. Government work, public domain. Metadata: https://www.fisheries.noaa.gov/inport/item/77594
  - Static fields (name, IMO, type, dimensions) are back-filled from the AIS Vessel Identification Database — not as-of data.
  - Terrestrial coverage only: vessels leave coverage offshore; gaps are receiver reach, not vessel behaviour.

Other providers: write a mapping in `config/sources/` (see docs/data-contracts.md), then `make import MAPPING=<name> DATASET=<id> FILES="a.csv b.csv"`.

- **Danish Maritime Authority (DMA)** historical AIS, http://aisdata.ais.dk/. Daily zips for recent years, monthly zips for 2023 and earlier. `scripts/fetch_dma.py` range-reads one day out of a monthly zip. Terrestrial, not down-sampled (Class A about every 10 s). Static fields are broadcast. Check DMA's terms of use before redistributing anything derived from it. One verified day: `dma_2023-06` (see docs/data-contracts.md).

- **OpenStreetMap land polygons** (`land-polygons-split-4326` from osmdata.openstreetmap.de), © OpenStreetMap contributors, ODbL 1.0. Attribute OSM in anything shown. Only polygons near the truth region are kept (`processed/geo/osm_land_us_gulf.parquet`). The source zip's SHA-256 and Last-Modified are in `manifests/osm_land_us_gulf.json`. Navigable masks built from it plus train-split traffic are `processed/geo/navigable_<protocol>__<dataset>.npz`; their sidecars, `manifests/masks/*.json`, are committed.

To reproduce: `make fetch && make canonical`. Checksums of every source zip are in the manifest.
