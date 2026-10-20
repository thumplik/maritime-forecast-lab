import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // Data comes from the local FastAPI backend at runtime; nothing is fetched at build time.
  output: "standalone",
};

export default config;
