"use client";

// Workbench: pick a dataset, a time window and a vessel; see its real track with coverage gaps
// drawn distinctly. Setting a cutoff clips the track to what was known then and opens the
// forecast comparison (Milestone 5, Screen B).
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import ForecastPanel from "@/components/forecast/ForecastPanel";
import Nav from "@/components/Nav";
import TrackMap, { COLORS } from "@/components/map/TrackMap";
import { clipTrack, type ForecastGeo, type TruthGeo } from "@/lib/forecast";
import { api, ApiError, type DatasetSummary, type Track, type VesselPage } from "@/lib/api/client";
import { formatDuration, trackToGeo } from "@/lib/track";
import { parseSampleLink } from "@/lib/benchmark";

const PAGE = 200;

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function asOfOf(track: Track, idx: number | null): string | undefined {
  return idx === null ? undefined : track.points.observed_at[idx];
}

function message(e: unknown): string {
  if (e instanceof ApiError) return `${e.message} (${e.code})`;
  return (e as Error).message;
}

export default function Page() {
  return (
    <Suspense fallback={<main className="layout" />}>
      <Explorer />
    </Suspense>
  );
}

function Explorer() {
  const params = useSearchParams();
  // A link from the failure gallery: replay one benchmark sample (vessel, cutoff, models).
  const [link] = useState(() => parseSampleLink(new URLSearchParams(params.toString())));
  const [pending, setPending] = useState(link);
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [datasetId, setDatasetId] = useState("");
  const [day, setDay] = useState("");
  const [span, setSpan] = useState(1);
  const [onlyInView, setOnlyInView] = useState(false);
  const [view, setView] = useState<[number, number, number, number] | null>(null);
  const [page, setPage] = useState<VesselPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  const [vesselId, setVesselId] = useState<string | null>(null);
  const [track, setTrack] = useState<Track | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [cutoffIdx, setCutoffIdx] = useState<number | null>(null);
  const [overlay, setOverlay] = useState<{ forecast: ForecastGeo | null; truth: TruthGeo | null; fitKey: string | null }>({
    forecast: null,
    truth: null,
    fitKey: null,
  });
  const onOverlay = useCallback(
    (forecast: ForecastGeo | null, truth: TruthGeo | null, fitKey: string | null) => setOverlay({ forecast, truth, fitKey }),
    [],
  );

  const dataset = datasets.find((d) => d.dataset_id === datasetId);
  const window = useMemo(
    () => (day ? { from: `${day}T00:00:00Z`, to: `${addDays(day, span)}T00:00:00Z` } : null),
    [day, span],
  );

  useEffect(() => {
    const ac = new AbortController();
    api
      .datasets(ac.signal)
      .then((r) => {
        setDatasets(r.datasets);
        const first = r.datasets[0];
        if (link && r.datasets.some((d) => d.dataset_id === link.dataset)) {
          // The day before the cutoff through the day after: the 24 h history is on the map.
          setDatasetId(link.dataset);
          setDay(addDays(link.as_of.slice(0, 10), -1));
          setSpan(3);
          setVesselId(link.vessel);
          setFilter(link.vessel);
        } else if (first) {
          setDatasetId(first.dataset_id);
          setDay(first.first_day);
        }
      })
      .catch((e) => e.name !== "AbortError" && setError(message(e)));
    return () => ac.abort();
  }, []);

  const bbox = onlyInView && view ? view.map((x) => x.toFixed(4)).join(",") : undefined;
  useEffect(() => setOffset(0), [bbox]);

  useEffect(() => {
    if (!datasetId || !window) return;
    const ac = new AbortController();
    setError(null);
    api
      .vessels({ dataset_id: datasetId, ...window, bbox, limit: PAGE, offset }, ac.signal)
      .then(setPage)
      .catch((e) => e.name !== "AbortError" && setError(message(e)));
    return () => ac.abort();
  }, [datasetId, window, bbox, offset]);

  useEffect(() => {
    if (!datasetId || !window || !vesselId) return;
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    api
      .track(vesselId, { dataset_id: datasetId, ...window }, ac.signal)
      .then(setTrack)
      .catch((e) => {
        if (e.name === "AbortError") return;
        setTrack(null);
        setError(message(e));
      })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, [datasetId, window, vesselId]);

  useEffect(() => {
    if (pending && track?.vessel_id === pending.vessel) {
      const t = Date.parse(pending.as_of);
      const i = track.points.observed_at.findIndex((x) => Date.parse(x) === t);
      setPending(null);
      setCutoffIdx(i >= 0 ? i : null);
      if (i < 0) setError(`The linked cutoff ${pending.as_of} is not a report in this track.`);
      return;
    }
    setCutoffIdx(null);
  }, [track]); // eslint-disable-line react-hooks/exhaustive-deps
  const linkActive = !!link && !!track && track.vessel_id === link.vessel && !!asOfOf(track, cutoffIdx) && Date.parse(asOfOf(track, cutoffIdx)!) === Date.parse(link.as_of);
  // A cutoff is always a real report time (§6.1); everything after it is removed from the map.
  const asOf = track && cutoffIdx !== null ? track.points.observed_at[cutoffIdx] : undefined;
  const history = useMemo(() => (track && asOf ? clipTrack(track, asOf) : null), [track, asOf]);
  useEffect(() => {
    if (!asOf) setOverlay({ forecast: null, truth: null, fitKey: null });
  }, [asOf]);
  const geo = useMemo(() => (history ? trackToGeo(history) : track ? trackToGeo(track) : null), [track, history]);
  const onView = useCallback((b: [number, number, number, number]) => setView(b), []);
  const shown = (page?.vessels ?? []).filter((v) => v.vessel_id.includes(filter.trim()));
  const longestGap = track?.gaps.reduce((m, g) => Math.max(m, g.duration_s), 0) ?? 0;

  return (
    <main className="layout">
      <aside className="panel">
        <Nav />
        <h1>Maritime Forecast Lab</h1>
        <p className="muted">Historical AIS forecast workbench</p>

        <label>
          Dataset
          <select
            value={datasetId}
            onChange={(e) => {
              const d = datasets.find((x) => x.dataset_id === e.target.value);
              setDatasetId(e.target.value);
              if (d) setDay(d.first_day);
              setOffset(0);
              setVesselId(null);
              setTrack(null);
            }}
          >
            {datasets.map((d) => (
              <option key={d.dataset_id} value={d.dataset_id}>
                {d.dataset_id}
              </option>
            ))}
          </select>
        </label>
        {dataset && (
          <p className="muted small">
            {dataset.source_id ?? "unknown source"} · {dataset.days} days · {dataset.rows?.toLocaleString() ?? "?"} reports
            {dataset.static_fields_provenance === "backfilled" && " · static fields back-filled"}
          </p>
        )}

        <div className="row">
          <label>
            Day (UTC)
            <input
              type="date"
              value={day}
              min={dataset?.first_day}
              max={dataset?.last_day}
              onChange={(e) => {
                setDay(e.target.value);
                setOffset(0);
              }}
            />
          </label>
          <label>
            Span
            <select value={span} onChange={(e) => setSpan(Number(e.target.value))}>
              {[1, 3, 7, 30].map((n) => (
                <option key={n} value={n}>
                  {n} day{n > 1 ? "s" : ""}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label className="check">
          <input type="checkbox" checked={onlyInView} onChange={(e) => (setOnlyInView(e.target.checked), setOffset(0))} />
          Only vessels seen in the current map view
        </label>

        <input
          className="search"
          placeholder="Filter this page by MMSI"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <div className="muted small">
          {page ? `${page.total.toLocaleString()} vessels · showing ${offset + 1}–${Math.min(offset + PAGE, page.total)}` : "…"}
          <span className="pager">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              ‹
            </button>
            <button disabled={!page || offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>
              ›
            </button>
          </span>
        </div>
        {/^\d{9}$/.test(filter.trim()) && !shown.some((v) => v.vessel_id === filter.trim()) && (
          <button className="link small" onClick={() => setVesselId(filter.trim())}>
            Open MMSI {filter.trim()} (not on this page)
          </button>
        )}
        <ul className="vessels" data-testid="vessel-list">
          {shown.map((v) => (
            <li key={v.vessel_id}>
              <button className={v.vessel_id === vesselId ? "selected" : ""} onClick={() => setVesselId(v.vessel_id)}>
                <span className="mono">{v.vessel_id}</span>
                <span className="muted">{v.vessel_class ?? "unknown"}</span>
                <span className="muted right">{v.reports.toLocaleString()}</span>
              </button>
            </li>
          ))}
        </ul>

        {error && <p className="error">{error}</p>}
        {loading && <p className="muted">Loading track…</p>}
        {track && (
          <section className="track" data-testid="track-summary">
            <h2 className="mono">{track.vessel_id}</h2>
            <p>
              {track.vessel_class ?? "unknown"} · {track.n_points.toLocaleString()} reports · {track.segments.length} segment
              {track.segments.length === 1 ? "" : "s"} · {track.gaps.length} gap{track.gaps.length === 1 ? "" : "s"}
              {track.gaps.length > 0 && ` (longest ${formatDuration(longestGap)})`}
            </p>
            {track.n_points > 0 && (
              <p className="small" data-testid="last-report">
                Last report in window: <span className="mono">{track.points.observed_at[track.n_points - 1]}</span> ·{" "}
                {track.points.sog_knots[track.n_points - 1] ?? "–"} kn · COG {track.points.cog_deg[track.n_points - 1] ?? "–"}°
              </p>
            )}
            {track.warnings.map((w) => (
              <p key={w} className="warning small">
                {w}
              </p>
            ))}
            <label className="check">
              <input
                type="checkbox"
                data-testid="cutoff-toggle"
                checked={cutoffIdx !== null}
                disabled={track.n_points < 2}
                onChange={(e) => setCutoffIdx(e.target.checked ? Math.floor(track.n_points / 2) : null)}
              />
              Forecast from a cutoff (hides reports after it)
            </label>
            {cutoffIdx !== null && asOf && (
              <label>
                Cutoff (as_of): <span className="mono small">{asOf}</span>
                <input
                  type="range"
                  min={0}
                  max={track.n_points - 1}
                  value={cutoffIdx}
                  onChange={(e) => setCutoffIdx(Number(e.target.value))}
                  data-testid="cutoff-slider"
                />
                <span className="muted small">
                  report {cutoffIdx + 1} of {track.n_points} · {track.points.sog_knots[cutoffIdx] ?? "–"} kn · COG {track.points.cog_deg[cutoffIdx] ?? "–"}°
                </span>
              </label>
            )}
          </section>
        )}
        {linkActive && link && (
          <div className="banner small" data-testid="sample-banner">
            <span>
              Replaying benchmark sample <span className="mono">#{link.cutoff}</span> of <span className="mono">{link.run}</span>
            </span>
            <Link href={`/benchmarks?run=${encodeURIComponent(link.run)}`}>← back to the run</Link>
          </div>
        )}
        {history && asOf && (
          <ForecastPanel key={`${history.vessel_id}/${linkActive}`} dataset={dataset} history={history} asOf={asOf} onOverlay={onOverlay} link={linkActive ? link : null} />
        )}

        <div className="legend small">
          <span>
            <i style={{ background: COLORS.segment }} /> observed track
          </span>
          <span>
            <i className="dashed" style={{ borderColor: COLORS.gap }} /> coverage gap &gt; {track?.gap_minutes ?? 30} min (not observed)
          </span>
          <span>
            <i className="dot" style={{ background: COLORS.flagged }} /> report with quality flags
          </span>
        </div>
      </aside>
      <TrackMap
        geo={geo}
        trackKey={track ? `${track.dataset_id}/${track.vessel_id}/${track.from}` : null}
        forecast={overlay.forecast}
        truth={overlay.truth}
        fitKey={overlay.fitKey}
        onViewChange={onView}
      />
    </main>
  );
}
