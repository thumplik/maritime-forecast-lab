"use client";

// MapLibre map showing one vessel track. Observed segments, coverage gaps and reports are
// separate layers with separate styles: a gap is never drawn as if the vessel was observed.
import { useEffect, useRef } from "react";
import type { GeoJSONSource, MapLayerMouseEvent, Map as MlMap } from "maplibre-gl";
import type { TrackGeo } from "@/lib/track";

// OpenFreeMap: free OSM vector tiles, no API key. Attribution is rendered by the style.
const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
const empty = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });

export const COLORS = { segment: "#1d4ed8", gap: "#b45309", flagged: "#dc2626" };

interface Props {
  geo: TrackGeo | null;
  onViewChange?: (bbox: [number, number, number, number]) => void;
}

export default function TrackMap({ geo, onViewChange }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MlMap | null>(null);
  const ready = useRef(false);
  const pending = useRef<TrackGeo | null>(null);
  const viewCb = useRef(onViewChange);
  viewCb.current = onViewChange;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const maplibregl = await import("maplibre-gl");
      if (cancelled || !container.current) return;
      // Served from public/ by scripts/copy-maplibre-worker.mjs (bundlers don't emit it).
      maplibregl.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");
      const m = new maplibregl.Map({
        container: container.current,
        style: STYLE_URL,
        center: [-94.8, 29.3],
        zoom: 8,
      });
      m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      m.addControl(new maplibregl.ScaleControl({ unit: "nautical" }), "bottom-right");
      map.current = m;
      const report = () => {
        const b = m.getBounds();
        viewCb.current?.([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
      };
      m.on("moveend", report);
      m.on("load", () => {
        m.addSource("gaps", { type: "geojson", data: empty() });
        m.addSource("gap-labels", { type: "geojson", data: empty() });
        m.addSource("segments", { type: "geojson", data: empty() });
        m.addSource("points", { type: "geojson", data: empty() });
        m.addLayer({
          id: "gaps",
          type: "line",
          source: "gaps",
          paint: { "line-color": COLORS.gap, "line-width": 1.5, "line-dasharray": [2, 3] },
        });
        m.addLayer({
          id: "gap-labels",
          type: "symbol",
          source: "gap-labels",
          layout: {
            "text-field": ["get", "label"],
            "text-size": 11,
            "text-font": ["Noto Sans Regular"],
            "text-offset": [0.8, 0],
            "text-anchor": "left",
            // A gap must always say it is a gap: never hidden by basemap label collisions.
            "text-allow-overlap": true,
            "text-ignore-placement": true,
          },
          paint: { "text-color": COLORS.gap, "text-halo-color": "#fff", "text-halo-width": 1.5 },
        });
        m.addLayer({
          id: "segments",
          type: "line",
          source: "segments",
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-color": COLORS.segment, "line-width": 2.5 },
        });
        m.addLayer({
          id: "points",
          type: "circle",
          source: "points",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 1.5, 14, 4],
            "circle-color": ["case", ["get", "flagged"], COLORS.flagged, COLORS.segment],
            "circle-stroke-color": "#fff",
            "circle-stroke-width": 0.5,
          },
        });
        m.on("click", "points", (e: MapLayerMouseEvent) => {
          const f = e.features?.[0];
          if (!f) return;
          const p = f.properties as { t: string; sog: number | null; segment: number; flagged: boolean };
          new maplibregl.Popup({ closeButton: false })
            .setLngLat(e.lngLat)
            .setText(`${p.t} · ${p.sog ?? "–"} kn · segment ${p.segment}${p.flagged ? " · flagged" : ""}`)
            .addTo(m);
        });
        m.on("mouseenter", "points", () => (m.getCanvas().style.cursor = "pointer"));
        m.on("mouseleave", "points", () => (m.getCanvas().style.cursor = ""));
        ready.current = true;
        report();
        if (pending.current) draw(m, pending.current);
      });
    })();
    return () => {
      cancelled = true;
      map.current?.remove();
      map.current = null;
      ready.current = false;
    };
  }, []);

  useEffect(() => {
    pending.current = geo;
    if (map.current && ready.current && geo) draw(map.current, geo);
  }, [geo]);

  return <div ref={container} className="map" data-testid="map" />;
}

function draw(m: MlMap, geo: TrackGeo) {
  (m.getSource("gaps") as GeoJSONSource).setData(geo.gaps);
  (m.getSource("gap-labels") as GeoJSONSource).setData(geo.gapLabels);
  (m.getSource("segments") as GeoJSONSource).setData(geo.segments);
  (m.getSource("points") as GeoJSONSource).setData(geo.points);
  if (geo.bounds) {
    const [w, s, e, n] = geo.bounds;
    m.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom: 13, duration: 600 });
  }
}
