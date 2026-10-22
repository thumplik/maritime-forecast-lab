// Playwright smoke test (build plan §7 Milestone 5, §10 "one end-to-end smoke test on the
// fixture"). Starts the API on the SYNTHETIC fixture (scripts/e2e_api.py, :8100) and a Next.js
// dev server on :3100 with its own build directory, so it can run beside `make dev`.
// Needs `uv` on PATH (the Makefile's `make e2e` sets it). No NOAA data, no network for data.
import { defineConfig, devices } from "@playwright/test";

const API = "http://127.0.0.1:8100"; // uvicorn binds IPv4 only
const WEB = "http://localhost:3100";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: WEB, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run --project ../backend python ../scripts/e2e_api.py --port 8100 --web-origin ${WEB}`,
      url: `${API}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "node scripts/copy-maplibre-worker.mjs && next dev --port 3100",
      url: WEB,
      env: { NEXT_PUBLIC_API_URL: API, NEXT_DIST_DIR: ".next-e2e" },
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
