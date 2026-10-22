// Dashboard shaping on a SYNTHETIC run record (not a real benchmark).
import { describe, expect, it } from "vitest";
import type { BenchmarkRun } from "@/lib/api/client";
import { errorSeries, logTicks, notEvaluable, skillVerdict, strataOf, stratumTable } from "@/lib/benchmark";

const stat = (e: number) => ({ estimate: e, ci_low: e * 0.9, ci_high: e * 1.1 });
function cell(model_id: string, stratum: string, h: number, median: number, extra: object = {}) {
  return {
    model_id, stratum, horizon_minutes: h, admission: "first_class", headline: stratum !== "underway:tug_tow",
    n_evaluable: 100, n_paired: h === 60 ? 100 : 80, n_vessels: 30, sample_set_sha256: `${stratum}${h}`, n_failed: 0, failure_rate: 0,
    median_error_m: stat(median), mean_error_m: stat(median), p90_error_m: median * 3, median_abs_along_track_m: null,
    median_abs_cross_track_m: null, n_decomposed: 0, median_ade_m: null, best_baseline_id: "a_v1", skill_vs_best_baseline: null, feasibility: null,
    ...extra,
  };
}

const run = {
  run_id: "syn",
  models: [{ model_id: "a_v1" }, { model_id: "b_v1" }],
  not_evaluable: { "120": { truth_gap: 5, no_report_after: 2 }, "60": { truth_gap: 3 } },
  results: [
    cell("a_v1", "underway:tug_tow", 60, 900),
    cell("a_v1", "stationary:ALL", 60, 10),
    cell("a_v1", "underway:ALL", 120, 2000),
    cell("b_v1", "underway:ALL", 120, 1500),
    cell("a_v1", "underway:ALL", 60, 1000),
    cell("b_v1", "underway:ALL", 60, 1000),
    cell("a_v1", "underway:TRANSITING", 60, 800),
  ],
} as unknown as BenchmarkRun;

describe("strataOf", () => {
  it("puts the headline strata first and reports the shortest-horizon pairing", () => {
    const s = strataOf(run);
    expect(s.map((x) => x.stratum)).toEqual(["underway:ALL", "underway:TRANSITING", "stationary:ALL", "underway:tug_tow"]);
    expect(s[0]!.n_paired).toBe(100);
    expect(s.at(-1)!.headline).toBe(false);
  });
});

describe("stratumTable", () => {
  it("pivots to horizon columns and marks the lowest median (ties: model order)", () => {
    const t = stratumTable(run, "underway:ALL");
    expect(t.map((c) => c.horizon_minutes)).toEqual([60, 120]);
    expect(t[0]!.lowest).toBe("a_v1");
    expect(t[1]!.lowest).toBe("b_v1");
    expect(Object.keys(t[1]!.models).sort()).toEqual(["a_v1", "b_v1"]);
  });
  it("feeds the lead-time series", () => {
    const s = errorSeries(stratumTable(run, "underway:ALL"), ["a_v1", "b_v1", "c_v1"]);
    expect(s.a_v1!.map((p) => p.est)).toEqual([1000, 2000]);
    expect(s.c_v1).toEqual([]);
  });
});

describe("helpers", () => {
  it("not_evaluable rows are sorted with totals", () => {
    const n = notEvaluable(run);
    expect(n.reasons).toEqual(["no_report_after", "truth_gap"]);
    expect(n.rows.map((r) => [r.horizon_minutes, r.total])).toEqual([[60, 3], [120, 7]]);
  });
  it("skill verdict follows the paired CI", () => {
    expect(skillVerdict({ estimate: 0.2, ci_low: 0.1, ci_high: 0.3 })).toBe("better");
    expect(skillVerdict({ estimate: -0.2, ci_low: -0.3, ci_high: -0.1 })).toBe("worse");
    expect(skillVerdict({ estimate: 0.1, ci_low: -0.1, ci_high: 0.3 })).toBe("unclear");
    expect(skillVerdict(null)).toBeNull();
  });
  it("log ticks", () => {
    expect(logTicks(0.3, 60)).toEqual([0.5, 1, 2, 5, 10, 20, 50]);
  });
});

describe("sample links", () => {
  it("round-trips through the explorer URL", async () => {
    const { sampleHref, parseSampleLink } = await import("@/lib/benchmark");
    const l = { dataset: "d", vessel: "367000001", as_of: "2023-06-28T19:47:04Z", models: ["a_v1", "b_v1"], horizon_h: 6, predict_h: 24, history_h: 24, step_min: 10, run: "r", cutoff: 7, expect: { model_id: "a_v1", horizon_minutes: 360, lat: 29.5, lon: -92.25 } };
    const href = sampleHref(l);
    expect(parseSampleLink(new URLSearchParams(href.slice(2)))).toEqual(l);
    expect(parseSampleLink(new URLSearchParams("dataset=d"))).toBeNull();
  });
  it("sketch keeps the anchor and fits the box", async () => {
    const { sketch } = await import("@/lib/benchmark");
    const p = sketch({ anchor_lat: 29, anchor_lon: -94, pred_lat: 29.1, pred_lon: -94, truth_lat: 29, truth_lon: -93.9 });
    for (const [x, y] of Object.values(p)) {
      expect(x).toBeGreaterThanOrEqual(8 - 1e-9);
      expect(y).toBeLessThanOrEqual(64 + 1e-9);
    }
    expect(p.pred![1]).toBeLessThan(p.anchor[1]); // north is up
    expect(p.truth[0]).toBeGreaterThan(p.anchor[0]); // east is right
  });
});
