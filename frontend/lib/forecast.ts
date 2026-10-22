// Convert forecast and evaluation responses into map layers (build plan §9, Screen B).
// Predictions are dashed and model-coloured; withheld truth is a separate, solid style and
// is only ever built from a ForecastEvaluation, never from the raw track after the cutoff.
import type { DatasetSummary, ForecastEvaluation, ForecastResponse, Track } from "@/lib/api/client";

type Position = [number, number];
interface Feature<G, P> {
  type: "Feature";
  geometry: G;
  properties: P;
}
interface FeatureCollection<G, P> {
  type: "FeatureCollection";
  features: Feature<G, P>[];
}
type LineString = { type: "LineString"; coordinates: Position[] };
type Point = { type: "Point"; coordinates: Position };
type Polygon = { type: "Polygon"; coordinates: Position[][] };
type MultiPolygon = { type: "MultiPolygon"; coordinates: Position[][][] };

export interface Anchor {
  t: string;
  lat: number;
  lon: number;
}

/** One fixed colour per model, so a model keeps its colour whatever else is selected
 * (colour follows the entity, never its rank). Hues from the validated reference categorical
 * order with blue and green left out: those mean "observed track" and "truth" on the map.
 * A model not listed here is drawn grey and still labelled; it never gets a generated hue. */
const MODEL_COLORS: Record<string, string> = {
  constant_turn_rate_v1: "#eb6834",
  constant_velocity_v1: "#1baf7a",
  damped_velocity_v1: "#eda100",
  persistence_v1: "#e87ba4",
  route_retrieval_v1: "#6250d6",
  route_retrieval_v2: "#e34948",
};
export const OTHER_MODEL_COLOR = "#6b7280";
export const TRUTH_COLOR = "#065f46";
export function modelColor(modelId: string): string {
  return MODEL_COLORS[modelId] ?? OTHER_MODEL_COLOR;
}

/** The track as it was known at as_of: reports after the cutoff are removed, not hidden. */
export function clipTrack(track: Track, asOf: string): Track {
  const cut = Date.parse(asOf);
  const p = track.points;
  let n = 0;
  while (n < p.observed_at.length && Date.parse(p.observed_at[n]!) <= cut) n++;
  const take = <T,>(xs: T[]) => xs.slice(0, n);
  return {
    ...track,
    to: asOf,
    n_points: n,
    segments: track.segments.filter((s) => Date.parse(s.start) <= cut),
    gaps: track.gaps.filter((g) => Date.parse(g.end) <= cut),
    points: {
      observed_at: take(p.observed_at),
      latitude_deg: take(p.latitude_deg),
      longitude_deg: take(p.longitude_deg),
      sog_knots: take(p.sog_knots),
      cog_deg: take(p.cog_deg),
      heading_deg: take(p.heading_deg),
      nav_status: take(p.nav_status),
      segment: take(p.segment),
      quality_flags: take(p.quality_flags),
    },
  };
}

export function anchorOf(track: Track): Anchor | null {
  const p = track.points;
  const i = p.observed_at.length - 1;
  return i < 0 ? null : { t: p.observed_at[i]!, lat: p.latitude_deg[i]!, lon: p.longitude_deg[i]! };
}

/** Longitude continued from `ref` by whole turns, so a dateline step stays short. */
function unwrap(lon: number, ref: number): number {
  while (lon - ref > 180) lon -= 360;
  while (lon - ref < -180) lon += 360;
  return lon;
}

const R_MEAN_M = 6_371_008.8;
/** Disc of `radiusM` around a point, as a polygon ring. Spherical destination formula: this is
 * for rendering only (radii are computed geodesically on WGS84 by the backend); the drawn
 * edge is within ~0.5% of the ellipsoidal distance. */
export function circle(lat: number, lon: number, radiusM: number, n = 72): Position[] {
  const φ1 = (lat * Math.PI) / 180;
  const λ1 = (lon * Math.PI) / 180;
  const δ = radiusM / R_MEAN_M;
  const ring: Position[] = [];
  for (let k = 0; k <= n; k++) {
    const θ = ((k % n) / n) * 2 * Math.PI;
    const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ) + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ));
    const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(δ) * Math.cos(φ1), Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2));
    ring.push([unwrap((λ2 * 180) / Math.PI, lon), (φ2 * 180) / Math.PI]);
  }
  return ring;
}

export function horizonMinutes(asOf: string, t: string): number {
  return Math.round((Date.parse(t) - Date.parse(asOf)) / 60000);
}

export function formatHorizon(min: number): string {
  if (min % 60 === 0) return `${min / 60} h`;
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

export interface PathProps {
  model_id: string;
  color: string;
}
export interface StepProps extends PathProps {
  t: string;
  horizon: string;
}
export interface CandidateProps extends PathProps {
  trajectory_id: string;
  similarity: number | null;
  central: boolean;
}
export interface DiscProps extends PathProps {
  radius_m: number;
  level: number;
}

export interface HexProps extends PathProps {
  area_km2: number;
  level: number;
  cells: number;
  /** Fill opacity: the more probable (inner) the band, the darker. */
  opacity: number;
}

/** Fill opacity per nested level: 50% core darkest, 90% outer band lightest. */
export function bandOpacity(level: number): number {
  return level <= 0.5 ? 0.5 : level <= 0.8 ? 0.3 : 0.14;
}

export interface ForecastGeo {
  paths: FeatureCollection<LineString, PathProps>;
  steps: FeatureCollection<Point, StepProps>;
  /** Positions at the selected target time. */
  markers: FeatureCollection<Point, StepProps>;
  candidates: FeatureCollection<LineString, CandidateProps>;
  /** Conformal discs at the selected target time, only where a radius was fitted. */
  discs: FeatureCollection<Polygon, DiscProps>;
  /** Calibrated hex prediction sets at the selected target time (GeoJSON outline of the cells). */
  hexes: FeatureCollection<Polygon | MultiPolygon, HexProps>;
  bounds: [number, number, number, number] | null;
}

/** Map layers for a forecast; `step` indexes the shared target-time grid. */
export function forecastToGeo(resp: ForecastResponse, anchor: Anchor | null, step: number, shown?: Set<string>): ForecastGeo {
  const geo: ForecastGeo = {
    paths: { type: "FeatureCollection", features: [] },
    steps: { type: "FeatureCollection", features: [] },
    markers: { type: "FeatureCollection", features: [] },
    candidates: { type: "FeatureCollection", features: [] },
    discs: { type: "FeatureCollection", features: [] },
    hexes: { type: "FeatureCollection", features: [] },
    bounds: null,
  };
  const ref = anchor?.lon ?? resp.forecasts.find((f) => f.points.length)?.points[0]?.longitude_deg ?? 0;
  const pos = (lat: number, lon: number): Position => [unwrap(lon, ref), lat];
  const head: Position[] = anchor ? [pos(anchor.lat, anchor.lon)] : [];
  const all: Position[] = [...head];

  for (const f of resp.forecasts) {
    if (f.status !== "ok" || (shown && !shown.has(f.model_id))) continue;
    const color = modelColor(f.model_id);
    const coords = f.points.map((p) => pos(p.latitude_deg, p.longitude_deg));
    all.push(...coords);
    geo.paths.features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [...head, ...coords] },
      properties: { model_id: f.model_id, color },
    });
    f.points.forEach((p, i) => {
      const props = { model_id: f.model_id, color, t: p.timestamp, horizon: formatHorizon(horizonMinutes(resp.as_of, p.timestamp)) };
      const feat: Feature<Point, StepProps> = { type: "Feature", geometry: { type: "Point", coordinates: coords[i]! }, properties: props };
      geo.steps.features.push(feat);
      if (i === step) geo.markers.features.push(feat);
    });
    for (const c of f.trajectories ?? []) {
      geo.candidates.features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: [...head, ...c.points.map((p) => pos(p.latitude_deg, p.longitude_deg))] },
        properties: { model_id: f.model_id, color, trajectory_id: c.trajectory_id, similarity: c.similarity ?? null, central: c.trajectory_id === f.central_trajectory_id },
      });
    }
    const hs = f.hex_sets?.[step];
    if (hs?.boundary) {
      // Nested bands when fitted (outer first, so inner bands draw on top), else the main set.
      const bands = (hs.layers ?? []).length
        ? [...hs.layers!].sort((a, b) => b.level - a.level).map((l) => ({ level: l.level, area: l.area_km2, n: l.n_cells, g: l.boundary }))
        : [{ level: hs.nominal_level, area: hs.area_km2, n: hs.cells.length, g: hs.boundary }];
      for (const band of bands) {
        if (!band.g) continue;
        const g = band.g as unknown as Polygon | MultiPolygon;
        geo.hexes.features.push({
          type: "Feature",
          geometry: g,
          properties: { model_id: f.model_id, color, area_km2: band.area, level: band.level, cells: band.n, opacity: bandOpacity(band.level) },
        });
      }
      const outer = hs.boundary as unknown as Polygon | MultiPolygon;
      const rings = outer.type === "Polygon" ? outer.coordinates : outer.coordinates.flat();
      for (const ring of rings) all.push(...ring.map(([lon, la]) => pos(la, lon)));
    }
    const r = f.uncertainty?.radii_m[step];
    const p = f.points[step];
    if (r != null && p) {
      const ring = circle(p.latitude_deg, unwrap(p.longitude_deg, ref), r);
      all.push(...ring);
      geo.discs.features.push({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [ring] },
        properties: { model_id: f.model_id, color, radius_m: r, level: f.uncertainty!.nominal_level },
      });
    }
  }
  geo.bounds = boundsOf(all);
  return geo;
}

export interface TruthProps {
  t: string;
  horizon: string;
}
export interface ErrorProps extends PathProps {
  error_m: number;
}
export interface TruthGeo {
  /** Lines only between consecutive target times that both have truth: never bridged. */
  lines: FeatureCollection<LineString, Record<string, never>>;
  points: FeatureCollection<Point, TruthProps>;
  /** Prediction → truth at the selected target time. */
  errors: FeatureCollection<LineString, ErrorProps>;
}

export function truthToGeo(ev: ForecastEvaluation, resp: ForecastResponse, anchor: Anchor | null, step: number, shown?: Set<string>): TruthGeo {
  const ref = anchor?.lon ?? 0;
  const pos = (lat: number, lon: number): Position => [unwrap(lon, ref), lat];
  const geo: TruthGeo = {
    lines: { type: "FeatureCollection", features: [] },
    points: { type: "FeatureCollection", features: [] },
    errors: { type: "FeatureCollection", features: [] },
  };
  let run: Position[] = anchor && ev.truth[0] ? [pos(anchor.lat, anchor.lon)] : [];
  const flush = () => {
    if (run.length >= 2) geo.lines.features.push({ type: "Feature", geometry: { type: "LineString", coordinates: run }, properties: {} });
    run = [];
  };
  ev.truth.forEach((p, i) => {
    if (!p) return flush();
    const c = pos(p.latitude_deg, p.longitude_deg);
    run.push(c);
    geo.points.features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: c },
      properties: { t: p.timestamp, horizon: formatHorizon(horizonMinutes(ev.as_of, p.timestamp)) },
    });
  });
  flush();

  const truth = ev.truth[step];
  if (truth) {
    for (const f of resp.forecasts) {
      const p = f.points[step];
      const s = ev.models.find((m) => m.model_id === f.model_id)?.scores[step];
      if (!p || s?.error_m == null || (shown && !shown.has(f.model_id))) continue;
      geo.errors.features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: [pos(p.latitude_deg, p.longitude_deg), pos(truth.latitude_deg, truth.longitude_deg)] },
        properties: { model_id: f.model_id, color: modelColor(f.model_id), error_m: s.error_m },
      });
    }
  }
  return geo;
}

function boundsOf(coords: Position[]): [number, number, number, number] | null {
  if (!coords.length) return null;
  const b: [number, number, number, number] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [lon, lat] of coords) {
    if (lon < b[0]) b[0] = lon;
    if (lat < b[1]) b[1] = lat;
    if (lon > b[2]) b[2] = lon;
    if (lat > b[3]) b[3] = lat;
  }
  return b;
}

export type HorizonStatus = "first_class" | "experimental" | "not_audited";

/** §6.4 admission of a horizon for the forecast's stratum (motion × class). A horizon the
 * coverage audit did not measure, or a dataset without an audit, is "not_audited". */
export function horizonStatus(dataset: DatasetSummary | undefined, motion: string | null | undefined, cls: string | null | undefined, minutes: number): HorizonStatus {
  const strata = dataset?.admission?.strata;
  if (!strata || !motion || minutes % 60 !== 0) return "not_audited";
  const s = strata[`${motion}:${cls ?? "unknown"}`];
  return s?.[String(minutes / 60)] ?? "not_audited";
}

/** Target-time indices worth a table column: whole hours, plus the last step. */
export function tableSteps(asOf: string, targetTimes: string[]): number[] {
  const out: number[] = [];
  targetTimes.forEach((t, i) => {
    if (horizonMinutes(asOf, t) % 60 === 0 || i === targetTimes.length - 1) out.push(i);
  });
  return out;
}

export function km(m: number | null | undefined): string {
  return m == null ? "–" : (m / 1000).toFixed(m < 10_000 ? 2 : 1);
}
