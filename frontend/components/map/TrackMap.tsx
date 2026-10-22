"use client";

// MapLibre map showing one vessel track. Observed segments, coverage gaps and reports are
// separate layers with separate styles: a gap is never drawn as if the vessel was observed.
// Forecasts are dashed and model-coloured; withheld truth is solid green and drawn on top.
import { useEffect, useRef } from "react";
import type { GeoJSONSource, MapLayerMouseEvent, Map as MlMap } from "maplibre-gl";
import type { TrackGeo } from "@/lib/track";
import { TRUTH_COLOR, type ForecastGeo, type TruthGeo } from "@/lib/forecast";

// OpenFreeMap: free OSM vector tiles, no API key. Attribution is rendered by the style.
const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
const empty = (): GeoJSON.FeatureCollection => ({ type: "FeatureCollection", features: [] });

export const COLORS = { segment: "#1d4ed8", gap: "#b45309", flagged: "#dc2626" };

interface Props {
  geo: TrackGeo | null;
  forecast?: ForecastGeo | null;
  truth?: TruthGeo | null;
  /** Changes when a new forecast arrives: the map refits to it (not on every slider move). */
  fitKey?: string | null;
  /** Changes when a different track is loaded: the map refits to it (not when it is clipped). */
  trackKey?: string | null;
  onViewChange?: (bbox: [number, number, number, number]) => void;
}

const FORECAST_SOURCES = ["hexes", "discs", "candidates", "paths", "steps", "markers"] as const;
const TRUTH_SOURCES = ["errors", "truth-lines", "truth-points"] as const;

export default function TrackMap({ geo, forecast = null, truth = null, fitKey = null, trackKey = null, onViewChange }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MlMap | null>(null);
  const ready = useRef(false);
  const pending = useRef<TrackGeo | null>(null);
  const pendingOverlay = useRef<{ forecast: ForecastGeo | null; truth: TruthGeo | null }>({ forecast: null, truth: null });
  const fitted = useRef<string | null>(null);
  const trackFitted = useRef<string | null>(null);
  const trackKeyRef = useRef(trackKey);
  trackKeyRef.current = trackKey;
  const drawTrack = (m: MlMap, g: TrackGeo) => {
    const refit = trackKeyRef.current === null || trackKeyRef.current !== trackFitted.current;
    trackFitted.current = trackKeyRef.current;
    draw(m, g, refit);
  };
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
        for (const id of [...FORECAST_SOURCES, ...TRUTH_SOURCES]) m.addSource(id, { type: "geojson", data: empty() });
        m.addLayer({
          id: "hexes-fill",
          type: "fill",
          source: "hexes",
          paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "opacity"] },
        });
        m.addLayer({
          id: "hexes-edge",
          type: "line",
          source: "hexes",
          paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.8 },
        });
        m.addLayer({
          id: "discs-fill",
          type: "fill",
          source: "discs",
          paint: { "fill-color": ["get", "color"], "fill-opacity": 0.08 },
        });
        m.addLayer({
          id: "discs-edge",
          type: "line",
          source: "discs",
          paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.7 },
        });
        m.addLayer({
          id: "candidates",
          type: "line",
          source: "candidates",
          paint: {
            "line-color": ["get", "color"],
            "line-width": 1,
            // Opacity follows similarity rank only; similarity is not a probability.
            "line-opacity": ["interpolate", ["linear"], ["coalesce", ["get", "similarity"], 0], 0, 0.15, 0.5, 0.45],
          },
        });
        m.addLayer({
          id: "paths",
          type: "line",
          source: "paths",
          layout: { "line-join": "round" },
          paint: { "line-color": ["get", "color"], "line-width": 2.5, "line-dasharray": [3, 2] },
        });
        m.addLayer({
          id: "steps",
          type: "circle",
          source: "steps",
          paint: { "circle-radius": 2, "circle-color": ["get", "color"] },
        });
        m.addLayer({
          id: "errors",
          type: "line",
          source: "errors",
          paint: { "line-color": "#6b7280", "line-width": 1, "line-dasharray": [1, 1.5] },
        });
        m.addLayer({
          id: "truth-lines",
          type: "line",
          source: "truth-lines",
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-color": TRUTH_COLOR, "line-width": 3 },
        });
        m.addLayer({
          id: "truth-points",
          type: "circle",
          source: "truth-points",
          paint: { "circle-radius": 3, "circle-color": "#fff", "circle-stroke-color": TRUTH_COLOR, "circle-stroke-width": 2 },
        });
        m.addLayer({
          id: "markers",
          type: "circle",
          source: "markers",
          paint: { "circle-radius": 6, "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 },
        });
        for (const layer of ["steps", "markers", "truth-points"]) {
          m.on("click", layer, (e: MapLayerMouseEvent) => {
            const p = e.features?.[0]?.properties as { t: string; horizon: string; model_id?: string } | undefined;
            if (!p) return;
            new maplibregl.Popup({ closeButton: false })
              .setLngLat(e.lngLat)
              .setText(`${p.model_id ?? "withheld truth"} · +${p.horizon} · ${p.t}`)
              .addTo(m);
          });
        }
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
        if (pending.current) drawTrack(m, pending.current);
        drawOverlay(m, pendingOverlay.current.forecast, pendingOverlay.current.truth);
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
    if (map.current && ready.current && geo) drawTrack(map.current, geo);
  }, [geo]);

  useEffect(() => {
    pendingOverlay.current = { forecast, truth };
    const m = map.current;
    if (!m || !ready.current) return;
    drawOverlay(m, forecast, truth);
    if (fitKey && fitKey !== fitted.current && forecast?.bounds) {
      fitted.current = fitKey;
      const [w, s, e, n] = forecast.bounds;
      m.fitBounds([[w, s], [e, n]], { padding: 80, maxZoom: 12, duration: 600 });
    }
  }, [forecast, truth, fitKey]);

  return <div ref={container} className="map" data-testid="map" />;
}

function draw(m: MlMap, geo: TrackGeo, refit: boolean) {
  (m.getSource("gaps") as GeoJSONSource).setData(geo.gaps);
  (m.getSource("gap-labels") as GeoJSONSource).setData(geo.gapLabels);
  (m.getSource("segments") as GeoJSONSource).setData(geo.segments);
  (m.getSource("points") as GeoJSONSource).setData(geo.points);
  if (refit && geo.bounds) {
    const [w, s, e, n] = geo.bounds;
    m.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom: 13, duration: 600 });
  }
}

function drawOverlay(m: MlMap, forecast: ForecastGeo | null, truth: TruthGeo | null) {
  const set = (id: string, data: GeoJSON.FeatureCollection | undefined) =>
    (m.getSource(id) as GeoJSONSource).setData(data ?? empty());
  set("hexes", forecast?.hexes as GeoJSON.FeatureCollection);
  set("discs", forecast?.discs as GeoJSON.FeatureCollection);
  set("candidates", forecast?.candidates as GeoJSON.FeatureCollection);
  set("paths", forecast?.paths as GeoJSON.FeatureCollection);
  set("steps", forecast?.steps as GeoJSON.FeatureCollection);
  set("markers", forecast?.markers as GeoJSON.FeatureCollection);
  set("errors", truth?.errors as GeoJSON.FeatureCollection);
  set("truth-lines", truth?.lines as GeoJSON.FeatureCollection);
  set("truth-points", truth?.points as GeoJSON.FeatureCollection);
}
