"use client";

// Screen C (build plan §9): benchmark dashboard. Reads one immutable run record; every number
// on this page comes from it. The run and stratum live in the URL (?run=&stratum=) so a view
// can be reproduced from its run id.
import { Suspense, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import FailureGallery from "@/components/benchmark/FailureGallery";
import LeadTimeChart from "@/components/benchmark/LeadTimeChart";
import Nav from "@/components/Nav";
import { api, ApiError, type BenchmarkListItem, type BenchmarkRun } from "@/lib/api/client";
import { errorSeries, formatH, kmCI, notEvaluable, pct, skillVerdict, splitNote, strataOf, stratumTable } from "@/lib/benchmark";
import { modelColor } from "@/lib/forecast";

function message(e: unknown): string {
  if (e instanceof ApiError) return `${e.message} (${e.code})`;
  return (e as Error).message;
}

export default function Page() {
  return (
    <Suspense fallback={<main className="layout" />}>
      <Dashboard />
    </Suspense>
  );
}

function Dashboard() {
  const params = useSearchParams();
  const router = useRouter();
  const path = usePathname();
  const runId = params.get("run");
  const stratumParam = params.get("stratum");
  const [runs, setRuns] = useState<BenchmarkListItem[]>([]);
  const [run, setRun] = useState<BenchmarkRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [onlyHeadline, setOnlyHeadline] = useState(false);

  const go = (q: Record<string, string | null>) => {
    const p = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(q)) (v === null ? p.delete(k) : p.set(k, v));
    router.replace(`${path}?${p.toString()}`, { scroll: false });
  };

  useEffect(() => {
    const ac = new AbortController();
    api
      .benchmarks(ac.signal)
      .then((r) => setRuns(r.runs))
      .catch((e) => e.name !== "AbortError" && setError(message(e)));
    return () => ac.abort();
  }, []);

  useEffect(() => {
    if (!runId) return setRun(null);
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    api
      .benchmark(runId, ac.signal)
      .then((s) => {
        setRun(s.run ?? null);
        if (!s.run) setError(`Run ${runId} is ${s.status}${s.message ? `: ${s.message}` : ""}`);
      })
      .catch((e) => {
        if (e.name === "AbortError") return;
        setRun(null);
        setError(message(e));
      })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, [runId]);

  const strata = useMemo(() => (run ? strataOf(run) : []), [run]);
  const stratum = stratumParam && strata.some((s) => s.stratum === stratumParam) ? stratumParam : (strata[0]?.stratum ?? null);
  const cells = useMemo(() => (run && stratum ? stratumTable(run, stratum) : []), [run, stratum]);
  const modelIds = run?.models.map((m) => m.model_id) ?? [];
  const series = useMemo(() => errorSeries(cells, modelIds), [cells, modelIds.join()]); // eslint-disable-line react-hooks/exhaustive-deps
  const ne = useMemo(() => (run ? notEvaluable(run) : null), [run]);
  const dropped = run?.dropped_cells?.filter((d) => d.stratum === stratum) ?? [];
  const sampler = (run?.sampler ?? {}) as Record<string, unknown>;
  const note = splitNote(sampler.split as string | undefined);

  return (
    <main className="layout">
      <aside className="panel">
        <Nav />
        <h1>Benchmarks</h1>
        <p className="muted small">
          Immutable runs in <span className="mono">data/runs/benchmarks</span>, newest first. The kept runs, and which ones superseded others, are listed in{" "}
          <span className="mono">docs/benchmarks.md</span>.
        </p>
        <ul className="runs" data-testid="run-list">
          {runs.map((r) => (
            <li key={r.run_id}>
              <button className={r.run_id === runId ? "selected" : ""} onClick={() => go({ run: r.run_id, stratum: null })}>
                <span className="mono small">{r.run_id}</span>
                <span className="small muted">
                  <span className={`badge split-${r.split}`}>{r.split ?? r.status}</span> {r.n_cutoffs?.toLocaleString() ?? "–"} cutoffs · {r.model_ids?.length ?? 0} models
                </span>
              </button>
            </li>
          ))}
        </ul>
        {error && <p className="error small">{error}</p>}
      </aside>

      <section className="dashboard" data-testid="dashboard">
        {!runId && <p className="muted">Pick a run on the left.</p>}
        {loading && <p className="muted">Loading and verifying the run record…</p>}
        {run && (
          <>
            <header>
              <h2 className="mono">{run.run_id}</h2>
              <p>
                <strong>{run.protocol.protocol}</strong> · {run.dataset_id} ({run.data_provenance}) · split <strong>{String(sampler.split)}</strong> ·{" "}
                {Number(sampler.n_cutoffs).toLocaleString()} cutoffs · {run.status}
              </p>
              {note && <p className="warning">{note}</p>}
              {!run.operational && <p className="warning">Not operational: diagnostic numbers only.</p>}
            </header>

            <div className="controls">
              <label>
                Stratum
                <select value={stratum ?? ""} onChange={(e) => go({ stratum: e.target.value })} data-testid="stratum">
                  {strata
                    .filter((s) => !onlyHeadline || s.headline)
                    .map((s) => (
                      <option key={s.stratum} value={s.stratum}>
                        {s.stratum} · {s.n_paired.toLocaleString()} paired{s.headline ? "" : " · not headline"}
                      </option>
                    ))}
                </select>
              </label>
              <label className="check">
                <input type="checkbox" checked={onlyHeadline} onChange={(e) => setOnlyHeadline(e.target.checked)} />
                Headline strata only
              </label>
            </div>

            <h3>Median error, km [95% CI], paired</h3>
            <div className="table-wrap">
              <table className="bench" data-testid="bench-table">
                <thead>
                  <tr>
                    <th>model</th>
                    {cells.map((c) => (
                      <th key={c.horizon_minutes} className={c.headline ? "" : "experimental"}>
                        {formatH(c.horizon_minutes)}
                        {!c.headline && "*"}
                        <div className="muted small">
                          {c.n_paired.toLocaleString()} / {c.n_vessels.toLocaleString()} v
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {modelIds.map((m) => (
                    <tr key={m}>
                      <td className="mono">
                        <i className="key" style={{ background: modelColor(m) }} />
                        {m}
                      </td>
                      {cells.map((c) => {
                        const r = c.models[m];
                        if (!r) return <td key={c.horizon_minutes}>–</td>;
                        const v = kmCI(r.median_error_m);
                        const sk = r.skill_vs_best_baseline;
                        const verdict = skillVerdict(sk);
                        const isRef = c.best_baseline_id === m;
                        return (
                          <td
                            key={c.horizon_minutes}
                            className={c.lowest === m ? "lowest" : ""}
                            title={`p90 ${(r.p90_error_m / 1000).toFixed(1)} km · failure rate ${(r.failure_rate * 100).toFixed(1)}% (${r.n_failed} failed)`}
                          >
                            <div>
                              {v.est} <span className="muted">[{v.ci}]</span>
                            </div>
                            <div className="small">
                              {isRef ? (
                                <span className="muted">best baseline</span>
                              ) : sk ? (
                                <span className={`skill ${verdict}`} title={`skill vs ${c.best_baseline_id}: ${pct(sk.estimate)} [${pct(sk.ci_low)}, ${pct(sk.ci_high)}]`}>
                                  {pct(sk.estimate)} [{pct(sk.ci_low)}, {pct(sk.ci_high)}]
                                </span>
                              ) : (
                                <span className="muted">no skill reference</span>
                              )}
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small">
              Columns: horizon, paired samples / vessels. Bold: lowest median in the cell. Second line: skill = 1 − median / median(best of persistence, constant velocity and damped velocity), with a paired
              vessel-bootstrap 95% CI. Green means the CI is above 0, red means it is below, grey means it straddles 0. * not headline: experimental (§6.4), too few samples, or a failure rate above tolerance. Hover a cell for p90 and failures.
            </p>
            {dropped.length > 0 && (
              <p className="warning small">
                Not scored in this stratum (too few evaluable samples): {dropped.map((d) => `${formatH(d.horizon_minutes)} (${d.n_evaluable} evaluable of ${d.n_cutoffs})`).join(", ")}
              </p>
            )}

            <h3>Failure gallery</h3>
            {stratum && cells.length > 0 && <FailureGallery key={`${run.run_id}/${stratum}`} run={run} stratum={stratum} cells={cells} />}

            <h3>Error versus lead time</h3>
            <LeadTimeChart series={series} horizons={cells.map((c) => c.horizon_minutes)} />

            <h3>Along-track / cross-track, median |error| km</h3>
            <p className="muted small">Along-track error means wrong speed; cross-track error means wrong route. Measured relative to the true path, on the samples where the decomposition exists (n shown).</p>
            <div className="table-wrap">
              <table className="bench compact">
                <thead>
                  <tr>
                    <th>model</th>
                    {cells.map((c) => (
                      <th key={c.horizon_minutes}>{formatH(c.horizon_minutes)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {modelIds.map((m) => (
                    <tr key={m}>
                      <td className="mono">
                        <i className="key" style={{ background: modelColor(m) }} />
                        {m}
                      </td>
                      {cells.map((c) => {
                        const r = c.models[m];
                        const f = (x: number | null | undefined) => (x == null ? "–" : (x / 1000).toFixed(x < 10_000 ? 2 : 1));
                        return (
                          <td key={c.horizon_minutes} title={r ? `n=${r.n_decomposed.toLocaleString()}` : ""}>
                            {r ? `${f(r.median_abs_along_track_m)} / ${f(r.median_abs_cross_track_m)}` : "–"}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                  <tr className="muted">
                    <td>n decomposed</td>
                    {cells.map((c) => (
                      <td key={c.horizon_minutes}>{Object.values(c.models)[0]?.n_decomposed.toLocaleString() ?? "–"}</td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>

            {ne && (
              <>
                <h3>Not evaluable (all cutoffs, all strata)</h3>
                <div className="table-wrap">
                  <table className="bench compact">
                    <thead>
                      <tr>
                        <th>horizon</th>
                        {ne.reasons.map((r) => (
                          <th key={r}>{r.replaceAll("_", " ")}</th>
                        ))}
                        <th>total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ne.rows.map((row) => (
                        <tr key={row.horizon_minutes}>
                          <td>{formatH(row.horizon_minutes)}</td>
                          {ne.reasons.map((r) => (
                            <td key={r}>{(row.counts[r] ?? 0).toLocaleString()}</td>
                          ))}
                          <td>{row.total.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            <h3>Reproducibility</h3>
            <dl className="meta small">
              <dt>git</dt>
              <dd className="mono">{run.git_sha}</dd>
              <dt>created</dt>
              <dd>{run.created_at}</dd>
              <dt>manifest</dt>
              <dd className="mono">
                {run.protocol.manifest_version} · {run.protocol.manifest_sha256}
              </dd>
              <dt>sampler</dt>
              <dd className="mono">
                seed {String(sampler.seed)} · n_boot {String(sampler.n_boot)} · history {String(sampler.history_hours)} h · step {String(sampler.step_minutes)} min · region {String(sampler.cutoff_region)} · truth bracket{" "}
                {String(sampler.max_bracket_gap_minutes)} min
              </dd>
              {Array.isArray(sampler.informed_by) && (
                <>
                  <dt>informed by</dt>
                  <dd className="mono">{(sampler.informed_by as string[]).join(" · ")}</dd>
                </>
              )}
              <dt>models</dt>
              <dd>
                {run.models.map((m) => (
                  <details key={m.model_id}>
                    <summary className="mono">
                      {m.model_id}@{m.model_version}
                    </summary>
                    <pre>{JSON.stringify(m.config, null, 1)}</pre>
                  </details>
                ))}
              </dd>
              <dt>reproduce</dt>
              <dd className="mono">
                make benchmark PROTOCOL={run.protocol.protocol} DS={run.dataset_id}
                {sampler.split && sampler.split !== "test" ? ` SPLIT=${String(sampler.split)}` : ""}
              </dd>
            </dl>
            {(run.warnings ?? []).map((w) => (
              <p key={w} className="warning small">
                {w}
              </p>
            ))}
          </>
        )}
      </section>
    </main>
  );
}
