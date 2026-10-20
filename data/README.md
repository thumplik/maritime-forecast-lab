# data/

Everything here except this file, `.gitkeep`s and `manifests/*.json` is gitignored.

| Path | Contents | Produced by |
|---|---|---|
| `raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet` | NOAA daily points, bbox-filtered, original columns typed | `scripts/fetch_noaa.py` |
| `processed/canonical/<dataset_id>/date=YYYY-MM-DD/part-0.parquet` | Canonical §4 observations, one partition per UTC day | `scripts/canonicalize.py`, `scripts/import_ais.py` |
| `processed/segmented/<dataset_id>/bucket-NN.parquet` | Segmented observations + motion features (16 vessel-hash buckets) | `scripts/coverage_audit.py` |
| `manifests/*.json` | Fetch manifests (source URLs, SHA-256, filter) and `<dataset_id>.canonical.json` dataset manifests (mapping, inputs with SHA-256 and ingest report, rows per day). Committed | fetch / canonicalize / import |
| `manifests/splits/*.json` | Protocol split manifests (committed) | `scripts/make_splits.py` |
| `runs/<run_id>/` | Audit and benchmark outputs with JSON sidecars | audit / benchmark scripts |

## Sources and licensing

- **NOAA MarineCadastre AccessAIS** (U.S. Coast Guard terrestrial AIS, one-minute filtered). U.S. Government work, public domain. Metadata: https://www.fisheries.noaa.gov/inport/item/77594
  - Static fields (name, IMO, type, dimensions) are back-filled from the AIS Vessel Identification Database — not as-of data.
  - Terrestrial coverage only: vessels leave coverage offshore; gaps are receiver reach, not vessel behaviour.

Other providers: write a mapping in `config/sources/` (see docs/data-contracts.md), then `make import MAPPING=<name> DATASET=<id> FILES="a.csv b.csv"`.

To reproduce: `make fetch && make canonical`. Checksums of every source zip are in the manifest.
