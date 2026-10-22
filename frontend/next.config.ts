import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Data comes from the local FastAPI backend at runtime; nothing is fetched at build time.
  output: "standalone",
  // The Playwright smoke test (playwright.config.ts) runs its own dev server next to
  // `make dev`: a separate build directory keeps the two from colliding.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
};

export default config;
