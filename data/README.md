# data/

Everything here except this file, `.gitkeep`s and `manifests/*.json` is gitignored.

| Path | Contents | Produced by |
|---|---|---|
| `raw/noaa_<region>/date=YYYY-MM-DD/part-0.parquet` | NOAA daily points, bbox-filtered, original columns typed | `scripts/fetch_noaa.py` |
| `processed/canonical/<dataset_id>/date=.../*.parquet` | Canonical §4 observations | `scripts/canonicalize.py` |
| `manifests/*.json` | Dataset manifests: source URLs, SHA-256, filter, row counts (committed) | fetch / canonicalize |
| `runs/<run_id>/` | Audit and benchmark outputs with JSON sidecars | audit / benchmark scripts |

## Sources and licensing

- **NOAA MarineCadastre AccessAIS** (U.S. Coast Guard terrestrial AIS, one-minute filtered). U.S. Government work, public domain. Metadata: https://www.fisheries.noaa.gov/inport/item/77594
  - Static fields (name, IMO, type, dimensions) are back-filled from the AIS Vessel Identification Database — not as-of data.
  - Terrestrial coverage only: vessels leave coverage offshore; gaps are receiver reach, not vessel behaviour.

To reproduce: `make fetch && make canonical`. Checksums of every source zip are in the manifest.
