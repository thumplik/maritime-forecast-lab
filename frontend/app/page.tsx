"use client";

// Milestone 1 explorer: pick a dataset, a time window and a vessel; see its real track with
// coverage gaps drawn distinctly. Forecasting and truth overlays arrive in Milestones 2 and 5.
import { useCallback, useEffect, useMemo, useState } from "react";
import TrackMap, { COLORS } from "@/components/map/TrackMap";
import { api, ApiError, type DatasetSummary, type Track, type VesselPage } from "@/lib/api/client";
import { formatDuration, trackToGeo } from "@/lib/track";

const PAGE = 200;

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function message(e: unknown): string {
  if (e instanceof ApiError) return `${e.message} (${e.code})`;
  return (e as Error).message;
}

export default function Explorer() {
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
        if (first) {
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

  const geo = useMemo(() => (track ? trackToGeo(track) : null), [track]);
  const onView = useCallback((b: [number, number, number, number]) => setView(b), []);
  const shown = (page?.vessels ?? []).filter((v) => v.vessel_id.includes(filter.trim()));
  const longestGap = track?.gaps.reduce((m, g) => Math.max(m, g.duration_s), 0) ?? 0;

  return (
    <main className="layout">
      <aside className="panel">
        <h1>Maritime Forecast Lab</h1>
        <p className="muted">Historical AIS explorer · Milestone 1</p>

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
            {track.warnings.map((w) => (
              <p key={w} className="warning small">
                {w}
              </p>
            ))}
          </section>
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
      <TrackMap geo={geo} onViewChange={onView} />
    </main>
  );
}
