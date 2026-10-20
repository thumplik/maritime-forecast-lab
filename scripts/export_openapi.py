"""Write the API's OpenAPI schema to frontend/lib/api/openapi.json (source of the TS types).

Includes the forecast/benchmark contracts even before their endpoints exist. Needs no
data. CI regenerates it and fails if the committed file or the generated
TypeScript (frontend/lib/api/schema.d.ts) is out of date.

Usage:
    uv run --project backend python scripts/export_openapi.py
"""

from __future__ import annotations

import json

from pydantic.json_schema import models_json_schema

from maritime_forecast.api.app import create_app
from maritime_forecast.config import REPO_ROOT
from maritime_forecast.schemas.benchmark import BenchmarkRun
from maritime_forecast.schemas.forecast import ForecastEvaluation, ForecastRequest, ForecastResponse

OUT = REPO_ROOT / "frontend" / "lib" / "api" / "openapi.json"


# Contracts not yet served by an endpoint (Milestone 2), published so the frontend types exist.
CONTRACTS = [
    (ForecastRequest, "validation"),
    (ForecastResponse, "serialization"),
    (ForecastEvaluation, "serialization"),
    (BenchmarkRun, "serialization"),
]


def main() -> None:
    spec = create_app().openapi()
    _, defs = models_json_schema(CONTRACTS, ref_template="#/components/schemas/{model}")
    schemas = spec["components"]["schemas"]
    for name, schema in defs.get("$defs", {}).items():
        if name in schemas and schemas[name] != schema:
            raise SystemExit(f"schema name clash: {name}")
        schemas[name] = schema
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
