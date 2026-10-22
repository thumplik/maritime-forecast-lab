// Shape an immutable benchmark run (GET /benchmarks/{run_id}) for the dashboard (§9, Screen C).
// Pure functions only: the page renders what these return. Every cell is paired: all models
// in a (stratum, horizon) cell were scored on the same sample set.
import type { BenchmarkRun } from "@/lib/api/client";

export type CellResult = NonNullable<BenchmarkRun["results"]>[number];

export interface StratumInfo {
  stratum: string;
  motion: string;
  cls: string;
  /** Headline at any horizon (first-class, enough samples, failure rates in tolerance). */
  headline: boolean;
  /** Paired samples at the shortest scored horizon. */
  n_paired: number;
}

const ORDER = ["underway:ALL", "underway:TRANSITING", "stationary:ALL"];

/** Strata in reading order: the pooled and transiting headline strata first, then classes. */
export function strataOf(run: BenchmarkRun): StratumInfo[] {
  const by = new Map<string, StratumInfo & { h: number }>();
  for (const r of run.results ?? []) {
    const cur = by.get(r.stratum);
    const [motion = "", cls = ""] = r.stratum.split(":");
    if (!cur) by.set(r.stratum, { stratum: r.stratum, motion, cls, headline: r.headline, n_paired: r.n_paired, h: r.horizon_minutes });
    else {
      cur.headline ||= r.headline;
      if (r.horizon_minutes < cur.h) Object.assign(cur, { h: r.horizon_minutes, n_paired: r.n_paired });
    }
  }
  const rank = (s: string) => {
    const i = ORDER.indexOf(s);
    return i >= 0 ? i : ORDER.length;
  };
  return [...by.values()]
    .sort((a, b) => rank(a.stratum) - rank(b.stratum) || a.motion.localeCompare(b.motion) || a.cls.localeCompare(b.cls))
    .map(({ h: _h, ...s }) => s);
}

export interface HorizonCell {
  horizon_minutes: number;
  admission: string;
  headline: boolean;
  n_paired: number;
  n_vessels: number;
  best_baseline_id: string | null;
  sample_set_sha256: string;
  /** model_id -> its result in this paired cell */
  models: Record<string, CellResult>;
  /** Lowest median error in the cell (ties: first in model order). */
  lowest: string | null;
}

/** One stratum as horizon columns; horizons with no scored cell are absent (see dropped). */
export function stratumTable(run: BenchmarkRun, stratum: string): HorizonCell[] {
  const cells = new Map<number, HorizonCell>();
  for (const r of run.results ?? []) {
    if (r.stratum !== stratum) continue;
    let c = cells.get(r.horizon_minutes);
    if (!c) {
      c = {
        horizon_minutes: r.horizon_minutes,
        admission: r.admission,
        headline: r.headline,
        n_paired: r.n_paired,
        n_vessels: r.n_vessels,
        best_baseline_id: r.best_baseline_id ?? null,
        sample_set_sha256: r.sample_set_sha256,
        models: {},
        lowest: null,
      };
      cells.set(r.horizon_minutes, c);
    }
    c.models[r.model_id] = r;
  }
  const order = run.models.map((m) => m.model_id);
  for (const c of cells.values()) {
    let best = Infinity;
    for (const m of order) {
      const e = c.models[m]?.median_error_m.estimate;
      if (e != null && e < best) {
        best = e;
        c.lowest = m;
      }
    }
  }
  return [...cells.values()].sort((a, b) => a.horizon_minutes - b.horizon_minutes);
}

export interface SeriesPoint {
  horizon_minutes: number;
  est: number;
  lo: number;
  hi: number;
}

/** Median error with its 95% CI per model, in horizon order, for the lead-time chart. */
export function errorSeries(cells: HorizonCell[], modelIds: string[]): Record<string, SeriesPoint[]> {
  const out: Record<string, SeriesPoint[]> = {};
  for (const m of modelIds) {
    out[m] = cells
      .filter((c) => c.models[m])
      .map((c) => {
        const s = c.models[m]!.median_error_m;
        return { horizon_minutes: c.horizon_minutes, est: s.estimate, lo: s.ci_low, hi: s.ci_high };
      });
  }
  return out;
}

/** Skill verdict from the paired bootstrap CI of 1 − median/median(best baseline). */
export function skillVerdict(s: { estimate: number; ci_low: number; ci_high: number } | null | undefined): "better" | "worse" | "unclear" | null {
  if (!s) return null;
  if (s.ci_low > 0) return "better";
  if (s.ci_high < 0) return "worse";
  return "unclear";
}

export interface NotEvaluableRow {
  horizon_minutes: number;
  counts: Record<string, number>;
  total: number;
}

/** Run-level not_evaluable counts (all cutoffs, all strata), reasons as columns. */
export function notEvaluable(run: BenchmarkRun): { reasons: string[]; rows: NotEvaluableRow[] } {
  const raw = run.not_evaluable as Record<string, Record<string, number>>;
  const reasons = new Set<string>();
  const rows = Object.entries(raw)
    .map(([h, counts]) => {
      Object.keys(counts).forEach((r) => reasons.add(r));
      return { horizon_minutes: Number(h), counts, total: Object.values(counts).reduce((a, b) => a + b, 0) };
    })
    .sort((a, b) => a.horizon_minutes - b.horizon_minutes);
  return { reasons: [...reasons].sort(), rows };
}

export function formatH(min: number): string {
  return min % 60 === 0 ? `${min / 60} h` : `${min} min`;
}

export function kmCI(s: { estimate: number; ci_low: number; ci_high: number }): { est: string; ci: string } {
  const f = (m: number) => (m / 1000).toFixed(m < 10_000 ? 2 : 1);
  return { est: f(s.estimate), ci: `${f(s.ci_low)}–${f(s.ci_high)}` };
}

export function pct(x: number): string {
  return `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(0)}%`;
}

/** Log-scale ticks (1–2–5 per decade) covering [lo, hi]. */
export function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let d = Math.floor(Math.log10(lo)); d <= Math.ceil(Math.log10(hi)); d++) {
    for (const k of [1, 2, 5]) {
      const v = k * 10 ** d;
      if (v >= lo && v <= hi) out.push(v);
    }
  }
  return out;
}

/** Dev, calibration and diagnostic runs are not headline results (AGENTS.md rule 13). */
export function splitNote(split: string | null | undefined): string | null {
  if (!split || split === "test") return null;
  if (split === "dev") return "Development window: tuning record, not a held-out result.";
  if (split === "calibration") return "Calibration window: used to fit conformal radii, not a reported result.";
  return `Split "${split}": not a held-out result.`;
}

export interface SampleLink {
  dataset: string;
  vessel: string;
  as_of: string;
  models: string[];
  horizon_h: number;
  run: string;
  cutoff: number;
  /** The run's stored prediction for one model at one horizon: the map checks it reproduces. */
  /** Predicted position only: no truth or error travels in the link (errors come from evaluation). */
  expect?: { model_id: string; horizon_minutes: number; lat: number; lon: number };
}

/** Explorer URL that replays a benchmark sample: same vessel, cutoff, models and horizon. */
export function sampleHref(l: SampleLink): string {
  const p = new URLSearchParams({
    dataset: l.dataset,
    vessel: l.vessel,
    as_of: l.as_of,
    models: l.models.join(","),
    horizon: String(l.horizon_h),
    run: l.run,
    cutoff: String(l.cutoff),
  });
  if (l.expect) p.set("expect", [l.expect.model_id, l.expect.horizon_minutes, l.expect.lat, l.expect.lon].join(","));
  return `/?${p.toString()}`;
}

export function parseSampleLink(q: URLSearchParams): SampleLink | null {
  const dataset = q.get("dataset");
  const vessel = q.get("vessel");
  const as_of = q.get("as_of");
  if (!dataset || !vessel || !as_of || Number.isNaN(Date.parse(as_of))) return null;
  const e = q.get("expect")?.split(",");
  const n = e?.slice(1).map(Number);
  return {
    dataset,
    vessel,
    as_of,
    models: (q.get("models") ?? "").split(",").filter(Boolean),
    horizon_h: Number(q.get("horizon") ?? 6) || 6,
    run: q.get("run") ?? "",
    cutoff: Number(q.get("cutoff") ?? -1),
    expect: e && n && n.length === 3 && n.every(Number.isFinite) ? { model_id: e[0]!, horizon_minutes: n[0]!, lat: n[1]!, lon: n[2]! } : undefined,
  };
}

/** Points of a sample in a small local frame for a thumbnail: x east, y north (screen y down),
 * scaled to fit `size` with equal metres per pixel. Display only (equirectangular about the
 * anchor); errors themselves always come from the run record. */
export function sketch(s: { anchor_lat: number; anchor_lon: number; pred_lat?: number | null; pred_lon?: number | null; truth_lat: number; truth_lon: number; reference_pred_lat?: number | null; reference_pred_lon?: number | null }, size = 72, pad = 8) {
  const k = Math.cos((s.anchor_lat * Math.PI) / 180);
  const raw = (lat: number, lon: number): [number, number] => [(lon - s.anchor_lon) * k, lat - s.anchor_lat];
  const pts: Record<string, [number, number]> = { anchor: raw(s.anchor_lat, s.anchor_lon), truth: raw(s.truth_lat, s.truth_lon) };
  if (s.pred_lat != null && s.pred_lon != null) pts.pred = raw(s.pred_lat, s.pred_lon);
  if (s.reference_pred_lat != null && s.reference_pred_lon != null) pts.ref = raw(s.reference_pred_lat, s.reference_pred_lon);
  const xs = Object.values(pts).map((p) => p[0]);
  const ys = Object.values(pts).map((p) => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1e-9);
  const scale = (size - 2 * pad) / span;
  const out: Record<string, [number, number]> = {};
  for (const [name, [x, y]] of Object.entries(pts)) out[name] = [size / 2 + (x - cx) * scale, size / 2 - (y - cy) * scale];
  return out as { anchor: [number, number]; truth: [number, number]; pred?: [number, number]; ref?: [number, number] };
}
