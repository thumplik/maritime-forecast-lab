"""Serve the read API on the SYNTHETIC fixture, for the Playwright smoke test (never real AIS).

Ingests backend/tests/fixtures/SYNTHETIC_tracks.csv into a throwaway store under a temporary
directory and runs uvicorn on it. No local NOAA data, models or network are needed, so CI can
run it. The dataset id says SYNTHETIC (AGENTS.md rule 4).

    uv run --project backend python scripts/e2e_api.py --port 8100 --web-origin http://localhost:3100
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import uvicorn

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.api.app import ApiSettings, create_app
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.storage.parquet_store import ParquetStore

FIXTURE = Path(__file__).resolve().parents[1] / "backend/tests/fixtures/SYNTHETIC_tracks.csv"
DATASET_ID = "SYNTHETIC_fixture"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--web-origin", default="http://localhost:3100", help="CORS origin of the app")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="mfl-e2e-") as tmp:
        root = Path(tmp)
        # ingest() records its dataset manifest under MANIFEST_DIR: keep it in the temp dir.
        ingest_mod.MANIFEST_DIR = root / "manifests"
        store = ParquetStore(root / "canonical")
        report, days = ingest_mod.ingest(
            [FIXTURE], load_mapping("noaa_marinecadastre"), DATASET_ID, store=store
        )
        print(f"{DATASET_ID}: {sum(days.values())} rows in {root}", file=sys.stderr)
        settings = ApiSettings(
            store_root=root / "canonical",
            manifest_dir=root / "manifests",
            runs_dir=root / "runs",
            conformal_dir=root / "conformal",
            cors_origins=[args.web_origin],
        )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
