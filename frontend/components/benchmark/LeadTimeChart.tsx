"use client";

// Median error versus lead time with 95% vessel-bootstrap CI bands, one line per model.
// Both axes are log scale: horizons span 1–24 h and errors span metres to hundreds of km.
// Hovering a horizon shows every model's value there (crosshair + tooltip).
import { useState } from "react";
import { formatH, logTicks, type SeriesPoint } from "@/lib/benchmark";
import { modelColor } from "@/lib/forecast";

interface Props {
  series: Record<string, SeriesPoint[]>;
  horizons: number[];
}

const W = 640;
const H = 280;
const M = { l: 52, r: 16, t: 12, b: 32 };

export default function LeadTimeChart({ series, horizons }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const models = Object.keys(series).filter((m) => series[m]!.length);
  const all = models.flatMap((m) => series[m]!);
  if (!all.length || !horizons.length) return <p className="muted small">No scored cells in this stratum.</p>;

  const km = (m: number) => Math.max(m / 1000, 0.001);
  const yLo = Math.min(...all.map((p) => km(p.lo)));
  const yHi = Math.max(...all.map((p) => km(p.hi)));
  const ly0 = Math.log10(yLo) - 0.05;
  const ly1 = Math.log10(yHi) + 0.05;
  const lx0 = Math.log10(horizons[0]!);
  const lx1 = Math.log10(horizons.at(-1)!);
  const x = (h: number) => M.l + (lx1 === lx0 ? 0.5 : (Math.log10(h) - lx0) / (lx1 - lx0)) * (W - M.l - M.r);
  const y = (m: number) => M.t + (1 - (Math.log10(km(m)) - ly0) / (ly1 - ly0)) * (H - M.t - M.b);
  const yTicks = logTicks(10 ** ly0, 10 ** ly1);
  const fmt = (v: number) => (v >= 1 ? String(v) : v.toFixed(v >= 0.1 ? 1 : 2));

  const hh = hover !== null ? horizons[hover]! : null;
  const rows = hh === null ? [] : models.map((m) => ({ m, p: series[m]!.find((p) => p.horizon_minutes === hh) })).filter((r) => r.p);

  return (
    <div className="chart">
      <div className="chart-legend small">
        {models.map((m) => (
          <span key={m}>
            <i style={{ background: modelColor(m) }} />
            {m}
          </span>
        ))}
      </div>
      <div className="chart-box">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Median error versus lead time, log–log, with 95% CI bands" onMouseLeave={() => setHover(null)}>
          {yTicks.map((v) => (
            <g key={v}>
              <line x1={M.l} x2={W - M.r} y1={y(v * 1000)} y2={y(v * 1000)} className="grid" />
              <text x={M.l - 6} y={y(v * 1000)} className="tick" textAnchor="end" dominantBaseline="middle">
                {fmt(v)}
              </text>
            </g>
          ))}
          <text x={12} y={M.t + (H - M.t - M.b) / 2} className="tick" textAnchor="middle" transform={`rotate(-90 12 ${M.t + (H - M.t - M.b) / 2})`}>
            median error, km (log)
          </text>
          {horizons.map((h) => (
            <text key={h} x={x(h)} y={H - M.b + 16} className="tick" textAnchor="middle">
              {formatH(h)}
            </text>
          ))}
          {models.map((m) => {
            const s = series[m]!;
            const band = [...s.map((p) => `${x(p.horizon_minutes)},${y(p.hi)}`), ...[...s].reverse().map((p) => `${x(p.horizon_minutes)},${y(p.lo)}`)].join(" ");
            return <polygon key={`b${m}`} points={band} fill={modelColor(m)} opacity={0.12} />;
          })}
          {models.map((m) => (
            <polyline
              key={`l${m}`}
              points={series[m]!.map((p) => `${x(p.horizon_minutes)},${y(p.est)}`).join(" ")}
              fill="none"
              stroke={modelColor(m)}
              strokeWidth={2}
              strokeLinejoin="round"
            />
          ))}
          {hh !== null && <line x1={x(hh)} x2={x(hh)} y1={M.t} y2={H - M.b} className="crosshair" />}
          {models.map((m) =>
            series[m]!.map((p) => (
              <circle key={`${m}${p.horizon_minutes}`} cx={x(p.horizon_minutes)} cy={y(p.est)} r={hh === p.horizon_minutes ? 5 : 3.5} fill={modelColor(m)} stroke="#fff" strokeWidth={2} />
            )),
          )}
          {horizons.map((h, i) => {
            const a = i === 0 ? M.l : (x(horizons[i - 1]!) + x(h)) / 2;
            const b = i === horizons.length - 1 ? W - M.r : (x(h) + x(horizons[i + 1]!)) / 2;
            return <rect key={`h${h}`} x={a} y={M.t} width={b - a} height={H - M.t - M.b} fill="transparent" onMouseEnter={() => setHover(i)} />;
          })}
        </svg>
        {hh !== null && (
          <div
            className="tooltip small"
            style={{ left: `${(x(hh) / W) * 100}%`, transform: x(hh) > W * 0.55 ? "translateX(calc(-100% - 12px))" : "translateX(12px)" }}
          >
            <strong>+{formatH(hh)}</strong>
            {rows
              .sort((a, b) => a.p!.est - b.p!.est)
              .map(({ m, p }) => (
                <div key={m}>
                  <i style={{ background: modelColor(m) }} />
                  <span className="mono">{m}</span>
                  <span>
                    {(p!.est / 1000).toFixed(2)} km <span className="muted">[{(p!.lo / 1000).toFixed(2)}–{(p!.hi / 1000).toFixed(2)}]</span>
                  </span>
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}
