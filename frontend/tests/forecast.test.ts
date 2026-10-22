// forecast map layers on SYNTHETIC responses (not real AIS).
import { describe, expect, it } from "vitest";
import type { DatasetSummary, ForecastEvaluation, ForecastResponse, Track } from "@/lib/api/client";
import { circle, clipTrack, forecastToGeo, horizonStatus, tableSteps, truthToGeo } from "@/lib/forecast";

const T = (m: number) => new Date(Date.UTC(2023, 5, 1, 0, m)).toISOString().replace(".000Z", "Z");
const AS_OF = T(60);
const times = [T(70), T(80), T(90)];
const pt = (i: number, dx = 0) => ({ timestamp: times[i]!, latitude_deg: 29 + i * 0.01, longitude_deg: -94 + dx });

function forecast(): ForecastResponse {
  const base = { model_version: "1", config: {}, error: null, target_times: times, runtime_ms: 1, warnings: [], central_trajectory_id: null, trajectories: [], uncertainty: null };
  return {
    forecast_id: "f",
    dataset_id: "syn",
    vessel_id: "367000001",
    as_of: AS_OF,
    source: "syn",
    data_version: "v",
    warnings: [],
    input_coverage: { n_reports: 2, first_report: T(0), last_report: AS_OF, last_report_age_s: 0, n_gaps: 0, motion_state: "underway", vessel_class: "cargo" },
    forecasts: [
      {
        ...base,
        model_id: "b_model",
        status: "ok",
        points: [pt(0), pt(1), pt(2)],
        uncertainty: {
          kind: "conformal_radius", nominal_level: 0.9, calibration_set_id: "cal", class_source: "backfilled",
          fitted_horizons_h: [0.5], radii_m: [null, null, 5000], strata: [null, null, "underway:cargo"], calibration_n: [null, null, 300], calibration_vessels: [null, null, 80],
        },
        trajectories: [{ trajectory_id: "ref1", points: [pt(0, 0.1)], similarity: 0.4, reference_ends_at: null, reference_segment_ids: ["s1"] }],
      },
      { ...base, model_id: "a_model", status: "unsupported", points: [], error: { code: "not_fitted", message: "x" } },
    ],
  };
}

describe("clipTrack", () => {
  it("removes every report after as_of", () => {
    const track = {
      dataset_id: "syn", vessel_id: "1", from: T(0), to: T(200), gap_minutes: 30, n_points: 3, vessel_class: null, static_fields_provenance: "backfilled", warnings: [],
      segments: [{ segment: 0, n_points: 3, start: T(0), end: T(120) }],
      gaps: [],
      points: { observed_at: [T(0), T(60), T(120)], latitude_deg: [1, 2, 3], longitude_deg: [1, 2, 3], sog_knots: [1, 1, 1], cog_deg: [0, 0, 0], heading_deg: [null, null, null], nav_status: [null, null, null], segment: [0, 0, 0], quality_flags: [[], [], []] },
    } as Track;
    const c = clipTrack(track, AS_OF);
    expect(c.points.observed_at).toEqual([T(0), T(60)]);
    expect(c.points.latitude_deg).toEqual([1, 2]);
    expect(c.n_points).toBe(2);
  });
});

describe("forecastToGeo", () => {
  const anchor = { t: AS_OF, lat: 28.99, lon: -94 };
  it("draws ok models from the anchor and skips failed ones", () => {
    const g = forecastToGeo(forecast(), anchor, 0);
    expect(g.paths.features).toHaveLength(1);
    expect(g.paths.features[0]!.geometry.coordinates[0]).toEqual([-94, 28.99]);
    expect(g.paths.features[0]!.geometry.coordinates).toHaveLength(4);
    expect(g.steps.features.map((f) => f.properties.horizon)).toEqual(["10 min", "20 min", "30 min"]);
    expect(g.candidates.features[0]!.properties.similarity).toBe(0.4);
  });
  it("draws a disc only at target times with a fitted radius", () => {
    expect(forecastToGeo(forecast(), anchor, 0).discs.features).toHaveLength(0);
    const d = forecastToGeo(forecast(), anchor, 2).discs.features;
    expect(d).toHaveLength(1);
    expect(d[0]!.properties.radius_m).toBe(5000);
  });
  it("draws a hex set only at target times that have one", () => {
    const r = forecast();
    const ring: [number, number][] = [[-94, 29], [-93.9, 29], [-93.9, 29.1], [-94, 29]];
    r.forecasts[0]!.hex_sets = [null, null, {
      kind: "h1_hex_set", nominal_level: 0.9, calibration_set_id: "cal", resolution: 6, cells: ["a", "b"], area_km2: 72,
      stratum: "underway:ALL", calibration_n: 300, calibration_vessels: 90, n_candidates: 12,
      boundary: { type: "Polygon", coordinates: [ring] }, adoption: "pending",
    }];
    expect(forecastToGeo(r, anchor, 0).hexes.features).toHaveLength(0);
    const h = forecastToGeo(r, anchor, 2).hexes.features;
    expect(h).toHaveLength(1);
    expect(h[0]!.properties.area_km2).toBe(72);
  });
  it("honours the model toggle", () => {
    expect(forecastToGeo(forecast(), anchor, 0, new Set()).paths.features).toHaveLength(0);
  });
});

describe("truthToGeo", () => {
  it("never bridges a target time without truth", () => {
    const resp = forecast();
    const ev = {
      as_of: AS_OF, target_times: times, truth: [pt(0), null, pt(2)], truth_reasons: [null, "truth_gap", null],
      models: [{ model_id: "b_model", status: "ok", ade_m: 1, feasibility: null, scores: times.map((_, i) => ({ horizon_minutes: 10 * (i + 1), evaluable: i !== 1, error_m: i === 1 ? null : 100, along_track_m: null, cross_track_m: null, reason: null })) }],
    } as unknown as ForecastEvaluation;
    const g = truthToGeo(ev, resp, { t: AS_OF, lat: 28.99, lon: -94 }, 2);
    // anchor→truth[0] is one line; truth[2] stands alone (no line across the gap)
    expect(g.lines.features.map((f) => f.geometry.coordinates.length)).toEqual([2]);
    expect(g.points.features).toHaveLength(2);
    expect(g.errors.features).toHaveLength(1);
    expect(truthToGeo(ev, resp, null, 1).errors.features).toHaveLength(0);
  });
});

describe("helpers", () => {
  it("circle radius is close to the requested distance", () => {
    const ring = circle(29, -94, 10_000);
    const [lon, lat] = ring[0]!; // bearing 0: due north
    expect(lon).toBeCloseTo(-94, 9);
    expect((lat - 29) * 111_000).toBeGreaterThan(9_900);
    expect((lat - 29) * 111_000).toBeLessThan(10_100);
    expect(ring.at(-1)).toEqual(ring[0]);
  });
  it("looks up admission by motion × class", () => {
    const ds = { admission: { run_id: "r", rule: {}, strata: { "underway:cargo": { "1": "first_class", "24": "experimental" } } } } as unknown as DatasetSummary;
    expect(horizonStatus(ds, "underway", "cargo", 60)).toBe("first_class");
    expect(horizonStatus(ds, "underway", "cargo", 1440)).toBe("experimental");
    expect(horizonStatus(ds, "underway", "cargo", 30)).toBe("not_audited");
    expect(horizonStatus(undefined, "underway", "cargo", 60)).toBe("not_audited");
  });
  it("table steps are whole hours plus the last", () => {
    expect(tableSteps(AS_OF, [T(90), T(120), T(150)])).toEqual([1, 2]);
  });
});
