"use client";

// Failure gallery (build plan §9 Screen C, Milestone 5): one paired cell's worst samples for a
// model, by its error or by its loss to the cell's best baseline on the same sample. Each card
// opens the sample on the map, where the forecast is re-run and checked against the record.
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError, type BenchmarkRun, type BenchmarkSamples } from "@/lib/api/client";
import { formatH, sampleHref, sketch, type HorizonCell } from "@/lib/benchmark";
import { modelColor, TRUTH_COLOR } from "@/lib/forecast";

interface Props {
  run: BenchmarkRun;
  stratum: string;
  cells: HorizonCell[];
}

const km = (m: number | null | undefined) => (m == null ? "–" : (m / 1000).toFixed(m < 10_000 ? 2 : 1));

export default function FailureGallery({ run, stratum, cells }: Props) {
  const modelIds = run.models.map((m) => m.model_id);
  const [model, setModel] = useState(() => (modelIds.includes("route_retrieval_v2") ? "route_retrieval_v2" : modelIds.at(-1)!));
  const [horizon, setHorizon] = useState(() => (cells.some((c) => c.horizon_minutes === 360) ? 360 : (cells[0]?.horizon_minutes ?? 60)));
  const cell = cells.find((c) => c.horizon_minutes === horizon);
  const ref = cell?.best_baseline_id ?? null;
  const canLoss = ref !== null && ref !== model;
  const [sortPref, setSortPref] = useState<"loss" | "error">("loss");
  const sort = canLoss ? sortPref : "error";
  const [data, setData] = useState<BenchmarkSamples | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!cell) return;
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    api
      .samples(run.run_id, { stratum, horizon_minutes: horizon, model_id: model, sort, limit: 24 }, ac.signal)
      .then(setData)
      .catch((e) => {
        if (e.name === "AbortError") return;
        setData(null);
        setError(e instanceof ApiError && e.code === "holdout_test_period" ? `Per-sample records withheld: ${e.message}` : (e as Error).message);
      })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, [run.run_id, stratum, horizon, model, sort, cell]);

  return (
    <div className="gallery" data-testid="gallery">
      <div className="controls">
        <label>
          Model
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {modelIds.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label>
          Horizon
          <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
            {cells.map((c) => (
              <option key={c.horizon_minutes} value={c.horizon_minutes}>
                {formatH(c.horizon_minutes)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Worst by
          <select value={sort} onChange={(e) => setSortPref(e.target.value as "loss" | "error")}>
            <option value="loss" disabled={!canLoss}>
              loss to {ref ?? "best baseline"}
            </option>
            <option value="error">own error</option>
          </select>
        </label>
      </div>
      <p className="muted small">
        {loading
          ? "Loading samples…"
          : data
            ? `${data.n_failed ? `${data.n_failed} evaluable sample${data.n_failed === 1 ? "" : "s"} where ${model} failed (listed first), then the worst` : "Worst"} of ${data.n_paired.toLocaleString()} paired samples in ${stratum} at ${formatH(horizon)}.` +
              (data.sort === "loss" ? ` Loss = ${model} error − ${data.reference_id} error on the same sample.` : "") +
              " A gallery of worst cases describes failures, not accuracy."
            : ""}
        {!canLoss && " No other skill reference in this cell, so samples are ranked by own error."}
      </p>
      {error && <p className="warning small">{error}</p>}
      {data && (
        <ul className="cards">
          {data.samples.map((s) => {
            const p = sketch(s);
            const href = sampleHref({
              dataset: data.dataset_id,
              vessel: s.vessel_id,
              as_of: s.as_of,
              models: data.reference_id && data.reference_id !== model ? [model, data.reference_id] : [model],
              horizon_h: [1, 2, 3, 6, 12, 24].find((h) => h * 60 >= horizon) ?? 24,
              // The runner predicted once at its longest horizon with this history and step
              // (retrieval's path depends on the horizon asked for): replay exactly that.
              predict_h: Math.max(...run.horizons_minutes) / 60,
              history_h: Number((run.sampler as Record<string, unknown>).history_hours) || undefined,
              step_min: Number((run.sampler as Record<string, unknown>).step_minutes) || undefined,
              run: data.run_id,
              cutoff: s.cutoff_id,
              expect: s.pred_lat != null && s.pred_lon != null ? { model_id: model, horizon_minutes: horizon, lat: s.pred_lat, lon: s.pred_lon } : undefined,
            });
            return (
              <li key={s.cutoff_id} className={s.status === "ok" ? "" : "failed"}>
                <svg viewBox="0 0 72 72" width={72} height={72} aria-label="anchor, prediction, baseline and truth (not to map scale)">
                  {p.ref && <line x1={p.anchor[0]} y1={p.anchor[1]} x2={p.ref[0]} y2={p.ref[1]} stroke={modelColor(data.reference_id ?? "")} strokeWidth={1.5} strokeDasharray="2 2" />}
                  {p.pred && <line x1={p.anchor[0]} y1={p.anchor[1]} x2={p.pred[0]} y2={p.pred[1]} stroke={modelColor(model)} strokeWidth={2} strokeDasharray="3 2" />}
                  <line x1={p.anchor[0]} y1={p.anchor[1]} x2={p.truth[0]} y2={p.truth[1]} stroke={TRUTH_COLOR} strokeWidth={2} />
                  <circle cx={p.anchor[0]} cy={p.anchor[1]} r={3} fill="#111827" />
                  {p.pred && <circle cx={p.pred[0]} cy={p.pred[1]} r={3.5} fill={modelColor(model)} stroke="#fff" strokeWidth={1} />}
                  <circle cx={p.truth[0]} cy={p.truth[1]} r={3.5} fill="#fff" stroke={TRUTH_COLOR} strokeWidth={2} />
                </svg>
                <div className="small">
                  <div className="mono">{s.vessel_id}</div>
                  <div className="muted">
                    {s.as_of.replace("T", " ").replace("Z", "")} · {s.vessel_class ?? "unknown"} · {s.sog_mean_10m?.toFixed(1) ?? "–"} kn
                  </div>
                  <div>
                    {model.replace(/_v\d+$/, "")}{" "}
                    {s.status === "ok" ? <strong>{km(s.error_m)} km</strong> : <strong className="error">{s.status}: {s.error_code ?? "no forecast"}</strong>}
                    {s.reference_error_m != null && data.reference_id !== model && (
                      <span className="muted">
                        {" "}
                        · {data.reference_id?.replace(/_v\d+$/, "")} {km(s.reference_error_m)} km
                      </span>
                    )}
                  </div>
                  <div className="muted">
                    along {km(s.along_m)} · cross {km(s.cross_m)}
                    {s.land_point ? " · on land" : s.land_crossing ? " · crosses land" : ""}
                  </div>
                  <Link href={href} className="open">
                    Open on map →
                  </Link>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
