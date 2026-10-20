@AGENTS.md

# frontend/ — Next.js + MapLibre workbench

Project rules in ../AGENTS.md and ../CLAUDE.md apply here too.

- Node 24 (`~/.local/node/bin` on this machine; CI uses actions/setup-node). Exact versions are pinned in package.json; `npm ci` only.
- API types are **generated**: `make gen-api` (backend OpenAPI → `lib/api/openapi.json` → `lib/api/schema.d.ts`). Never hand-edit either file; CI fails if they drift from the backend.
- All API access goes through `lib/api/client.ts` (typed, error envelope → `ApiError`).
- Map logic that decides what is drawn lives in pure functions (`lib/track.ts`) with Vitest tests; components only render.
- Display rules (build plan §9): observed segments and coverage gaps are separate layers with separate styles — never connect points across a gap. Truth and prediction (later) never share a style.
- MapLibre 6 loads its worker as a separate module: `scripts/copy-maplibre-worker.mjs` copies it to `public/maplibre/` (gitignored) and `TrackMap` calls `setWorkerUrl`. If the map is blank, check that first.
- Basemap: OpenFreeMap positron (OSM data, no key). Keep attribution visible.
- `npm run typecheck`, `npm test`, `npm run build` must pass before committing.
