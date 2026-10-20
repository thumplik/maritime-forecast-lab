"""Write the API's OpenAPI schema to frontend/lib/api/openapi.json (source of the TS types).

Needs no data. CI regenerates it and fails if the committed file or the generated
TypeScript (frontend/lib/api/schema.d.ts) is out of date.

Usage:
    uv run --project backend python scripts/export_openapi.py
"""

from __future__ import annotations

import json

from maritime_forecast.api.app import create_app
from maritime_forecast.config import REPO_ROOT

OUT = REPO_ROOT / "frontend" / "lib" / "api" / "openapi.json"


def main() -> None:
    spec = create_app().openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({len(spec['paths'])} paths)")


if __name__ == "__main__":
    main()
