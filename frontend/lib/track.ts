// Convert an API track into map layers. Observed segments become lines; coverage gaps
// become separate features so they can never be drawn as if the vessel was observed.
import type { Track } from "@/lib/api/client";

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

export interface SegmentProps {
  segment: number;
  n_points: number;
  start: string;
  end: string;
}
export interface GapProps {
  duration_s: number;
  label: string;
  start: string;
  end: string;
}
export interface PointProps {
  t: string;
  sog: number | null;
  segment: number;
  flagged: boolean;
}

export interface TrackGeo {
  segments: FeatureCollection<LineString, SegmentProps>;
  gaps: FeatureCollection<LineString, GapProps>;
  /** One point per gap at its midpoint, for always-visible duration labels. */
  gapLabels: FeatureCollection<Point, GapProps>;
  points: FeatureCollection<Point, PointProps>;
  bounds: [number, number, number, number] | null; // west, south, east, north
}

export function formatDuration(seconds: number): string {
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (h < 48) return rem ? `${h} h ${rem} min` : `${h} h`;
  return `${(h / 24).toFixed(1)} d`;
}

export function trackToGeo(track: Track): TrackGeo {
  const p = track.points;
  const n = p.observed_at.length;
  // Unwrap longitude along the track (e.g. 179.9 -> 180.1 instead of -179.9) so a dateline
  // crossing is a short step, not a line around the world. MapLibre renders lon beyond ±180.
  const coords: Position[] = [];
  for (let i = 0; i < n; i++) {
    let lon = p.longitude_deg[i]!;
    if (i > 0) {
      const prev = coords[i - 1]![0];
      // Shift by whole turns only when needed, so ordinary coordinates stay bit-identical.
      while (lon - prev > 180) lon -= 360;
      while (lon - prev < -180) lon += 360;
    }
    coords.push([lon, p.latitude_deg[i]!]);
  }

  const segments: Feature<LineString, SegmentProps>[] = [];
  const gaps: Feature<LineString, GapProps>[] = [];
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i < n && p.segment[i] === p.segment[i - 1]) continue;
    // [start, i) is one segment; a single report has no line, only its point.
    if (i - start >= 2) {
      segments.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords.slice(start, i) },
        properties: {
          segment: p.segment[start]!,
          n_points: i - start,
          start: p.observed_at[start]!,
          end: p.observed_at[i - 1]!,
        },
      });
    }
    if (i < n) {
      const a = p.observed_at[i - 1]!;
      const b = p.observed_at[i]!;
      const duration_s = (Date.parse(b) - Date.parse(a)) / 1000;
      gaps.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: [coords[i - 1]!, coords[i]!] },
        properties: { duration_s, label: `gap ${formatDuration(duration_s)}`, start: a, end: b },
      });
    }
    start = i;
  }

  const points: Feature<Point, PointProps>[] = coords.map((c, i) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: c },
    properties: {
      t: p.observed_at[i]!,
      sog: p.sog_knots[i] ?? null,
      segment: p.segment[i]!,
      flagged: (p.quality_flags[i]?.length ?? 0) > 0,
    },
  }));

  let bounds: TrackGeo["bounds"] = null;
  if (n > 0) {
    // A loop, not Math.min(...xs): spreading 10^5+ arguments overflows the call stack.
    bounds = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [lon, lat] of coords) {
      if (lon < bounds[0]) bounds[0] = lon;
      if (lat < bounds[1]) bounds[1] = lat;
      if (lon > bounds[2]) bounds[2] = lon;
      if (lat > bounds[3]) bounds[3] = lat;
    }
  }
  const gapLabels: Feature<Point, GapProps>[] = gaps.map((g) => {
    const [a, b] = g.geometry.coordinates as [Position, Position];
    return {
      type: "Feature",
      geometry: { type: "Point", coordinates: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] },
      properties: g.properties,
    };
  });

  return {
    segments: { type: "FeatureCollection", features: segments },
    gaps: { type: "FeatureCollection", features: gaps },
    gapLabels: { type: "FeatureCollection", features: gapLabels },
    points: { type: "FeatureCollection", features: points },
    bounds,
  };
}
