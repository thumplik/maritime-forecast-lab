"use client";

// Screen B (build plan §9): forecast comparison for one vessel at a cutoff. The map only shows
// reports at or before the cutoff; withheld truth is fetched from /forecasts/evaluation, and
// only after a forecast exists for the exact same request.
import { useEffect, useMemo, useRef, useState } from "react";
import type { SampleLink } from "@/lib/benchmark";
import { api, ApiError, type DatasetSummary, type ForecastEvaluation, type ForecastRequest, type ForecastResponse, type ModelInfo, type Track } from "@/lib/api/client";
import {
  anchorOf,
  forecastToGeo,
  formatHorizon,
  horizonMinutes,
  horizonStatus,
  km,
  modelColor,
  tableSteps,
  truthToGeo,
  TRUTH_COLOR,
  type ForecastGeo,
  type TruthGeo,
} from "@/lib/forecast";

const HORIZONS = [1, 2, 3, 6, 12, 24];
const DEFAULT_MODELS = ["persistence_v1", "constant_velocity_v1", "damped_velocity_v1", "route_retrieval_v2"];

interface Props {
  dataset: DatasetSummary | undefined;
  /** The track clipped to the cutoff (never the reports after it). */
  history: Track;
  asOf: string;
  onOverlay: (forecast: ForecastGeo | null, truth: TruthGeo | null, fitKey: string | null) => void;
  /** A benchmark sample being replayed: its models and horizon, run once, checked against the record. */
  link?: SampleLink | null;
}

function message(e: unknown): string {
  if (e instanceof ApiError) return `${e.message} (${e.code})`;
  return (e as Error).message;
}

export default function ForecastPanel({ dataset, history, asOf, onOverlay, link = null }: Props) {
  const datasetId = history.dataset_id;
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selected, setSelected] = useState<string[]>(() => (link?.models.length ? link.models : DEFAULT_MODELS));
  const [horizon, setHorizon] = useState(() => (link && HORIZONS.includes(link.horizon_h) ? link.horizon_h : 6));
  const autoRan = useRef(false);
  const [level, setLevel] = useState<0.8 | 0.9 | 0.95 | null>(0.9);
  const [resp, setResp] = useState<ForecastResponse | null>(null);
  const [respKey, setRespKey] = useState<string | null>(null);
  const [ev, setEv] = useState<ForecastEvaluation | null>(null);
  const [evError, setEvError] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"forecast" | "truth" | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    api
      .models(datasetId, ac.signal)
      .then((r) => {
        const usable = r.models.filter((m) => !m.calibration_only && m.installed);
        setModels(usable);
        setSelected((s) => s.filter((id) => usable.some((m) => m.model_id === id)));
      })
      .catch((e) => e.name !== "AbortError" && setError(message(e)));
    return () => ac.abort();
  }, [datasetId]);

  const req: ForecastRequest = useMemo(
    () => ({
      dataset_id: datasetId,
      vessel_id: history.vessel_id,
      as_of: asOf,
      history_hours: link?.history_h ?? 24,
      horizon_hours: horizon,
      step_minutes: link?.step_min ?? 10,
      // Replaying a benchmark sample: ask the models what the run asked them.
      ...(link?.predict_h && link.predict_h >= horizon ? { predict_horizon_hours: link.predict_h } : {}),
      model_ids: [...selected].sort(),
      conformal_level: level,
      show_ground_truth: false,
      hex_sets: true,
    }),
    [datasetId, history.vessel_id, asOf, horizon, selected, level, link],
  );
  const key = JSON.stringify(req);
  // Anything that changes the request invalidates the forecast and, with it, the truth.
  const current = resp && respKey === key ? resp : null;
  const truth = current && ev && ev.forecast_id === current.forecast_id ? ev : null;
  useEffect(() => {
    setEv(null);
    setEvError(null);
  }, [key]);

  const anchor = useMemo(() => anchorOf(history), [history]);
  const shown = useMemo(() => new Set(selected.filter((m) => !hidden.has(m))), [selected, hidden]);
  const nSteps = current?.forecasts[0]?.target_times.length ?? 0;
  const s = Math.min(step, Math.max(0, nSteps - 1));
  useEffect(() => {
    if (!current) return onOverlay(null, null, null);
    onOverlay(forecastToGeo(current, anchor, s, shown), truth ? truthToGeo(truth, current, anchor, s, shown) : null, current.forecast_id);
  }, [current, truth, anchor, s, shown, onOverlay]);

  useEffect(() => {
    if (!link || autoRan.current || !models.length) return;
    autoRan.current = true;
    void runForecast();
  }, [link, models]); // eslint-disable-line react-hooks/exhaustive-deps

  async function runForecast() {
    setBusy("forecast");
    setError(null);
    try {
      const r = await api.forecast(req);
      setResp(r);
      setRespKey(key);
      const tt = r.forecasts[0]?.target_times ?? [];
      const e = link?.expect;
      const at = e ? tt.findIndex((t) => horizonMinutes(r.as_of, t) === e.horizon_minutes) : -1;
      setStep(at >= 0 ? at : Math.max(0, tt.length - 1));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  }

  async function showTruth() {
    if (!current) return;
    setBusy("truth");
    setEvError(null);
    try {
      setEv(await api.evaluate(req));
    } catch (e) {
      setEvError(e instanceof ApiError && e.code === "holdout_test_period" ? `Truth withheld: ${e.message}` : message(e));
    } finally {
      setBusy(null);
    }
  }

  const cov = current?.input_coverage;
  const times = current?.forecasts[0]?.target_times ?? [];
  const cols = current ? tableSteps(current.as_of, times) : [];
  const status = (i: number) => horizonStatus(dataset, cov?.motion_state, cov?.vessel_class, horizonMinutes(asOf, times[i]!));

  return (
    <section className="forecast" data-testid="forecast-panel">
      <h2>Forecast</h2>
      <div className="row">
        <label>
          Horizon
          <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
            {HORIZONS.map((h) => (
              <option key={h} value={h}>
                {h} h
              </option>
            ))}
          </select>
        </label>
        <label>
          Radii
          <select value={level ?? ""} onChange={(e) => setLevel(e.target.value ? (Number(e.target.value) as 0.8 | 0.9 | 0.95) : null)}>
            <option value="">none</option>
            <option value="0.8">80%</option>
            <option value="0.9">90%</option>
            <option value="0.95">95%</option>
          </select>
        </label>
      </div>
      <fieldset className="models">
        <legend>Models</legend>
        {models.map((m) => (
          <label key={m.model_id} className="check" title={m.description}>
            <input
              type="checkbox"
              checked={selected.includes(m.model_id)}
              onChange={(e) => setSelected((s) => (e.target.checked ? [...s, m.model_id] : s.filter((x) => x !== m.model_id)))}
            />
            <span className="mono small">{m.model_id}</span>
          </label>
        ))}
      </fieldset>
      <button className="primary" disabled={!selected.length || busy !== null || !history.n_points} onClick={runForecast} data-testid="run-forecast">
        {busy === "forecast" ? "Forecasting…" : current ? "Forecast is current" : "Run forecast"}
      </button>
      {error && <p className="error small">{error}</p>}

      {current && (
        <>
          <p className="muted small">
            {cov?.n_reports} reports in the {req.history_hours} h history · last {cov?.last_report_age_s != null ? `${Math.round(cov.last_report_age_s)} s` : "–"} before cutoff · {cov?.n_gaps} gap
            {cov?.n_gaps === 1 ? "" : "s"} · stratum {cov?.motion_state ?? "no motion state"}:{cov?.vessel_class ?? "unknown"}
          </p>
          {link?.expect && <Reproduction resp={current} expect={link.expect} />}
          <label>
            Target time: +{formatHorizon(horizonMinutes(current.as_of, times[s]!))}
            <span className={`badge ${status(s)}`}>{status(s).replace("_", " ")}</span>
            <input type="range" min={0} max={nSteps - 1} value={s} onChange={(e) => setStep(Number(e.target.value))} data-testid="horizon-slider" />
          </label>
          <ul className="model-rows">
            {current.forecasts.map((f) => {
              const r = f.uncertainty?.radii_m[s];
              return (
                <li key={f.model_id}>
                  <label className="check">
                    <input
                      type="checkbox"
                      disabled={f.status !== "ok"}
                      checked={f.status === "ok" && !hidden.has(f.model_id)}
                      onChange={(e) =>
                        setHidden((h) => {
                          const n = new Set(h);
                          if (e.target.checked) n.delete(f.model_id);
                          else n.add(f.model_id);
                          return n;
                        })
                      }
                    />
                    <i className="swatch" style={{ borderColor: modelColor(f.model_id) }} />
                    <span className="mono small">{f.model_id}</span>
                  </label>
                  {f.status !== "ok" ? (
                    <p className="warning small">
                      {f.status}: {f.error?.message}
                    </p>
                  ) : (
                    <p className="muted small">
                      {f.uncertainty
                        ? r != null
                          ? `${Math.round(f.uncertainty.nominal_level * 100)}% radius ${km(r)} km · n=${f.uncertainty.calibration_n?.[s]} from ${f.uncertainty.calibration_vessels?.[s]} vessels · ${f.uncertainty.strata?.[s]}`
                          : `no radius at this target time (fitted at ${(f.uncertainty.fitted_horizons_h ?? []).join(", ")} h)`
                        : "no radii"}
                      {(f.trajectories?.length ?? 0) > 0 && ` · ${f.trajectories!.length} candidate routes (similarity, not probability)`}
                      {f.hex_sets?.[s] &&
                        ` · ${Math.round(f.hex_sets[s]!.nominal_level * 100)}% hex set: ${f.hex_sets[s]!.cells.length} cells, ${Math.round(f.hex_sets[s]!.area_km2).toLocaleString()} km² (${f.hex_sets[s]!.stratum}, n=${f.hex_sets[s]!.calibration_n}; ${f.hex_sets[s]!.adoption === "adopted" ? "adopted" : f.hex_sets[s]!.adoption === "pending" ? "not yet tested on the hold-out" : "experimental: not adopted"})`}
                      {f.predicted_horizon_hours != null && ` · predicted at ${f.predicted_horizon_hours} h as benchmarked, shown to ${horizon} h`}
                    </p>
                  )}
                  {(f.warnings?.length ?? 0) > 0 && (
                    <details className="small warning">
                      <summary>{f.warnings!.length} warning{f.warnings!.length === 1 ? "" : "s"}</summary>
                      {f.warnings!.map((w) => (
                        <p key={w}>{w}</p>
                      ))}
                    </details>
                  )}
                </li>
              );
            })}
          </ul>

          <button disabled={busy !== null || truth !== null || evError?.startsWith("Truth withheld") === true} onClick={showTruth} data-testid="show-truth">
            {busy === "truth" ? "Loading truth…" : truth ? "Withheld truth shown" : "Reveal withheld truth"}
          </button>
          {evError && <p className="warning small">{evError}</p>}
          {truth && (
            <div className="eval" data-testid="evaluation">
              <table>
                <thead>
                  <tr>
                    <th>error km</th>
                    {cols.map((i) => (
                      <th key={i} className={status(i) === "experimental" ? "experimental" : ""} title={status(i).replace("_", " ")}>
                        {formatHorizon(horizonMinutes(truth.as_of, truth.target_times[i]!))}
                        {status(i) === "experimental" && "*"}
                      </th>
                    ))}
                    <th title="Mean error over the paired target times">ADE</th>
                  </tr>
                </thead>
                <tbody>
                  {truth.models.map((m) => (
                    <tr key={m.model_id}>
                      <td className="mono">
                        <i className="swatch" style={{ borderColor: modelColor(m.model_id) }} />
                        {m.model_id.replace(/_v\d+$/, "")}
                      </td>
                      {cols.map((i) => (
                        <td key={i} title={m.scores[i]?.reason ?? (m.scores[i]?.along_track_m != null ? `along ${km(m.scores[i]!.along_track_m)} · cross ${km(m.scores[i]!.cross_track_m)}` : "")}>
                          {m.scores[i]?.evaluable ? km(m.scores[i]!.error_m) : "n/e"}
                        </td>
                      ))}
                      <td>{km(m.ade_m)}</td>
                    </tr>
                  ))}
                  <tr className="muted">
                    <td>truth</td>
                    {cols.map((i) => (
                      <td key={i}>{truth.truth[i] ? "✓" : (truth.truth_reasons[i] ?? "–").replaceAll("_", " ")}</td>
                    ))}
                    <td>{truth.paired.filter(Boolean).length} paired</td>
                  </tr>
                </tbody>
              </table>
              <p className="muted small">
                n/e = not evaluable (reason on hover). Truth from {truth.truth_region}, never bridged across gaps &gt; {truth.max_bracket_gap_minutes} min.
                {cols.some((i) => status(i) === "experimental") && " * experimental horizon for this stratum (§6.4)."}
                {cols.every((i) => status(i) === "not_audited") && " Horizon admission is not recorded for this dataset."}
              </p>
              {(truth.warnings ?? []).map((w) => (
                <p key={w} className="warning small">
                  {w}
                </p>
              ))}
            </div>
          )}
          <p className="muted small mono repro">
            forecast {current.forecast_id} · data {current.data_version.slice(0, 12)} · {current.forecasts.map((f) => `${f.model_id}@${f.model_version}`).join(", ")}
          </p>
          <div className="legend small">
            <span>
              <i className="dashed" style={{ borderColor: "#6b7280" }} /> predicted path (colour per model)
            </span>
            <span>
              <i style={{ background: "#6b7280", opacity: 0.4, height: 1 }} /> retrieved candidate (similarity)
            </span>
            <span>
              <i className="ring" /> conformal radius at the target time
            </span>
            <span>
              <i className="hex" /> calibrated hex set at the target time (retrieval)
            </span>
            <span>
              <i style={{ background: TRUTH_COLOR }} /> withheld truth (after reveal)
            </span>
          </div>
        </>
      )}
    </section>
  );
}

/** Does the re-run forecast reproduce the run's stored prediction for this sample? */
function Reproduction({ resp, expect }: { resp: ForecastResponse; expect: NonNullable<SampleLink["expect"]> }) {
  const f = resp.forecasts.find((x) => x.model_id === expect.model_id);
  const i = f ? f.target_times.findIndex((t) => horizonMinutes(resp.as_of, t) === expect.horizon_minutes) : -1;
  const p = f && i >= 0 ? f.points[i] : undefined;
  if (!p) return <p className="warning small">Cannot check against the run record: {expect.model_id} has no point at +{formatHorizon(expect.horizon_minutes)}.</p>;
  const dLat = Math.abs(p.latitude_deg - expect.lat);
  const dLon = Math.abs(p.longitude_deg - expect.lon);
  const same = dLat < 1e-6 && dLon < 1e-6;
  return (
    <p className={`small ${same ? "ok" : "warning"}`} data-testid="reproduction">
      {same
        ? `Reproduces the run record: ${expect.model_id} at +${formatHorizon(expect.horizon_minutes)} is the scored prediction.`
        : `Differs from the run record at +${formatHorizon(expect.horizon_minutes)} (Δlat ${dLat.toFixed(5)}°, Δlon ${dLon.toFixed(5)}°): the model, its parameters or the data changed since the run.`}
    </p>
  );
}
