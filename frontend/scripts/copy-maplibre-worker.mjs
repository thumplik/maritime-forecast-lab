// MapLibre 6 runs its web worker from a separate ES module that bundlers do not emit.
// Copy it (and the shared chunk it imports) to public/ so the app can serve it; see
// setWorkerUrl in components/map/TrackMap.tsx. Runs on postinstall, predev and prebuild.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(root, "node_modules", "maplibre-gl", "dist");
const dst = join(root, "public", "maplibre");
mkdirSync(dst, { recursive: true });
for (const f of ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"]) copyFileSync(join(src, f), join(dst, f));
console.log(`copied MapLibre worker to ${dst}`);
