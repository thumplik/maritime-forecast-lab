// trackToGeo on a SYNTHETIC track (not real AIS).
import { describe, expect, it } from "vitest";
import type { Track } from "@/lib/api/client";
import { formatDuration, trackToGeo } from "@/lib/track";

function track(times: string[], segment: number[]): Track {
  const n = times.length;
  return {
    dataset_id: "syn",
    vessel_id: "367000001",
    from: "2023-06-01T00:00:00Z",
    to: "2023-06-02T00:00:00Z",
    gap_minutes: 30,
    n_points: n,
    vessel_class: "cargo",
    static_fields_provenance: "backfilled",
    warnings: [],
    segments: [],
    gaps: [],
    points: {
      observed_at: times,
      latitude_deg: times.map((_, i) => 29 + i * 0.01),
      longitude_deg: times.map((_, i) => -94 - i * 0.01),
      sog_knots: times.map(() => 10),
      cog_deg: times.map(() => 90),
      heading_deg: times.map(() => null),
      nav_status: times.map(() => null),
      segment,
      quality_flags: times.map((_, i) => (i === 1 ? ["sog_implausible"] : [])),
    },
  };
}

const T = (m: number) => new Date(Date.UTC(2023, 5, 1, 0, m)).toISOString();

describe("trackToGeo", () => {
  it("splits lines at gaps and emits each gap as its own feature", () => {
    const g = trackToGeo(track([T(0), T(10), T(20), T(80), T(90)], [0, 0, 0, 1, 1]));
    expect(g.segments.features.map((f) => f.geometry.coordinates.length)).toEqual([3, 2]);
    expect(g.gaps.features).toHaveLength(1);
    const gap = g.gaps.features[0]!;
    expect(gap.properties.label).toBe("gap 1 h");
    // the gap joins the last point of segment 0 to the first point of segment 1 ...
    expect(gap.geometry.coordinates).toEqual([
      g.segments.features[0]!.geometry.coordinates.at(-1),
      g.segments.features[1]!.geometry.coordinates[0],
    ]);
    // the label sits at the gap midpoint
    const mid = g.gapLabels.features[0]!.geometry.coordinates;
    expect(mid[0]).toBeCloseTo(-94.025, 6);
    expect(mid[1]).toBeCloseTo(29.025, 6);
    // ... and no observed line crosses it
    for (const f of g.segments.features) expect(f.properties.end <= gap.properties.start || f.properties.start >= gap.properties.end).toBe(true);
  });

  it("draws no line for a single-report segment but keeps its point", () => {
    const g = trackToGeo(track([T(0), T(10), T(60), T(120), T(130)], [0, 0, 1, 2, 2]));
    expect(g.segments.features.map((f) => f.properties.segment)).toEqual([0, 2]);
    expect(g.gaps.features).toHaveLength(2);
    expect(g.points.features).toHaveLength(5);
  });

  it("flags points and computes bounds", () => {
    const g = trackToGeo(track([T(0), T(10)], [0, 0]));
    expect(g.points.features.map((f) => f.properties.flagged)).toEqual([false, true]);
    expect(g.bounds).toEqual([-94.01, 29, -94, 29.01]);
  });

  it("handles a 200k-point track (the API maximum)", () => {
    const n = 200_000;
    const times = Array.from({ length: n }, (_, i) => new Date(Date.UTC(2023, 5, 1) + i * 10_000).toISOString());
    const g = trackToGeo(track(times, Array(n).fill(0)));
    expect(g.bounds).not.toBeNull();
    expect(g.segments.features[0]!.geometry.coordinates).toHaveLength(n);
  });

  it("handles an empty track", () => {
    const g = trackToGeo(track([], []));
    expect(g.bounds).toBeNull();
    expect(g.segments.features).toHaveLength(0);
  });
});

describe("formatDuration", () => {
  it.each([
    [1860, "31 min"],
    [3600, "1 h"],
    [5400, "1 h 30 min"],
    [3 * 86400, "3.0 d"],
  ])("%d s -> %s", (s, out) => expect(formatDuration(s)).toBe(out));
});
