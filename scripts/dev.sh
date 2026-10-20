#!/usr/bin/env bash
# Start the read API (:8000) and the Next.js dev server (:3000) together; Ctrl-C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/node/bin:$HOME/Library/Python/3.9/bin:$HOME/.local/bin:$PATH"

command -v uv >/dev/null || { echo "uv not found (see README quick start)"; exit 1; }
command -v npm >/dev/null || { echo "Node/npm not found (need Node 24; see README)"; exit 1; }
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && npm ci --no-audit --no-fund)

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && uv run uvicorn maritime_forecast.api.app:app --reload --port "${API_PORT:-8000}") &
pids+=($!)
(cd "$ROOT/frontend" && NEXT_PUBLIC_API_URL="http://localhost:${API_PORT:-8000}" npm run dev -- --port "${WEB_PORT:-3000}") &
pids+=($!)

echo "API  http://localhost:${API_PORT:-8000}/docs"
echo "Web  http://localhost:${WEB_PORT:-3000}"
# Exit (and stop the other) as soon as either service dies. Portable to macOS bash 3.2.
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do sleep 1; done
