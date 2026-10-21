"""R1 historical-route retrieval, `route_retrieval_v1` (build plan §5 R1, Milestone 3).

Plain nearest neighbours over completed historical segments, no learned parameters:

1. **Pool.** The segment index (data.segment_index) is BUILT from the store using only data
   before `until = pool_until(as_of)`. Under the default frozen pool that is the end of the
   protocol's train period, so the retrieval index period is the train period (§5 U1). The
   result is then filtered by `eligible()`: known complete by `until`; P-unseen keeps train
   vessels only and never the target. Post-cutoff data is never read at all. The pool is
   cached per process, keyed by store, dataset, data version (partition files with size and
   mtime), bound, manifest and config, as the new-forecaster checklist requires.
2. **Match.** Every 10-min edge of an eligible track that passes within `radius_m` of the
   anchor (the latest report) is a candidate alignment: the anchor is projected onto the
   edge, which gives a continuous reference time tau. The match distance is

       D = (offset / pos_scale)^2 + (recent shape / shape_scale)^2
           + (course difference / cog_scale)^2   [only when both are moving]
           + (speed difference / sog_scale)^2 + class_penalty [different vessel class]

   The recent shape compares the target's last `context_steps` 10-min displacements (from its
   own history, never interpolated across a gap) with the reference's before tau. The best
   edge per segment is kept, then the top K segments with at most `max_per_vessel` per vessel.
   If nothing lies within the first radius, the next one is tried.
3. **Continue.** Each reference is translated so its point at tau sits on the anchor (in the
   local frame centred on the anchor, geo.geodesic.LocalFrame) and followed forward: target
   time t maps to the reference at tau + (t - anchor time). There is no rotation, because
   lanes are geographic. When a reference's segment ends (a coverage gap or the end of the
   data), the path holds its last position and `reference_ends_at` records when.
4. **Central prediction.** The similarity-weighted medoid of the candidates that cover at
   least `medoid_min_coverage` of the best candidate's span. A medoid is a real path, so it
   never averages two branches of a junction into the bank between them.

`similarity = exp(-D / 2)` ranks references. It is NOT a probability.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from maritime_forecast.config import MANIFEST_DIR
from maritime_forecast.data.segment_index import (
    INDEX_VERSION,
    IndexConfig,
    Pool,
    build_segment_index,
    eligible,
    pool_until,
)
from maritime_forecast.geo.geodesic import KNOT_MPS, LocalFrame
from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
    ModelContext,
)
from maritime_forecast.models.constant_velocity import STALE_WARNING_MINUTES, select_anchor
from maritime_forecast.models.damped_velocity import history_vessel_class
from maritime_forecast.schemas.manifest import SplitManifest

MODEL_ID = "route_retrieval_v1"
CELL_DEG = 0.05  # spatial bucket for the edge lookup
PASS1_KEEP = 4  # approximate best edges per segment re-scored exactly
EDGE_REACH_M = 3000.0  # edges up to 6 km (36 kn over 10 min) are found from their middle
_POOL_CACHE: OrderedDict[tuple, _PoolArrays] = OrderedDict()
_POOL_CACHE_SIZE = 2
_POOL_LOCK = threading.Lock()  # API routes run in a thread pool; build each pool once


@dataclass(frozen=True)
class RetrievalConfig:
    k: int = 10
    radii_m: tuple[float, ...] = (1000.0, 3000.0)
    pos_scale_m: float = 500.0
    shape_scale_m: float = 500.0
    cog_scale_deg: float = 30.0
    sog_scale_kn: float = 3.0
    moving_kn: float = 1.0
    class_penalty: float = 1.0
    context_steps: int = 3
    min_reference_future_min: float = 60.0
    max_per_vessel: int = 3
    medoid_min_coverage: float = 0.5
    pool: Pool = "frozen"
    index_days: float | None = None  # build only from the last N days before the pool bound
    index: IndexConfig = field(default_factory=IndexConfig)

    def __post_init__(self) -> None:
        if self.k < 1 or self.max_per_vessel < 1 or self.context_steps < 0:
            raise ValueError("k, max_per_vessel >= 1 and context_steps >= 0")
        if not self.radii_m or min(self.radii_m) <= 0:
            raise ValueError("radii_m must be non-empty and positive")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["radii_m"] = list(self.radii_m)
        d["index"] = self.index.as_dict()
        return d


@dataclass(frozen=True)
class _PoolArrays:
    """Eligible segments with their regular tracks flattened for vectorized lookup."""

    segment_id: np.ndarray  # object, per segment
    vessel_id: np.ndarray  # object, per segment
    vessel_class: np.ndarray  # object, per segment
    t0: np.ndarray  # float seconds since epoch of track[0], per segment
    off: np.ndarray  # int64 offset of a segment's first track point in the flat arrays
    n: np.ndarray  # int64 track length, per segment
    lat: np.ndarray  # flat track latitudes (NaN where the track has no position)
    lon: np.ndarray
    seg: np.ndarray  # flat -> segment index
    cell_sorted: np.ndarray  # cell key of each flat point, sorted
    cell_order: np.ndarray  # flat indices in cell_sorted order
    step_s: float

    @property
    def size(self) -> int:
        return len(self.segment_id)


def _cell(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    i = np.floor(np.asarray(lat) / CELL_DEG).astype(np.int64) + 4000
    j = np.floor(np.asarray(lon) / CELL_DEG).astype(np.int64) + 8000
    return i * 100_000 + j


def pool_arrays(segments: pl.DataFrame, step_minutes: int) -> _PoolArrays:
    df = segments.sort("segment_id")
    n = df["track_lat"].list.len().to_numpy().astype(np.int64)
    off = np.concatenate([[0], np.cumsum(n)[:-1]]).astype(np.int64)
    lat = df["track_lat"].explode().cast(pl.Float64).fill_null(np.nan).to_numpy()
    lon = df["track_lon"].explode().cast(pl.Float64).fill_null(np.nan).to_numpy()
    seg = np.repeat(np.arange(df.height, dtype=np.int64), n)
    ok = np.isfinite(lat) & np.isfinite(lon)
    idx = np.flatnonzero(ok)
    keys = _cell(lat[idx], lon[idx])
    order = np.argsort(keys, kind="stable")
    t0 = np.array([t.timestamp() for t in df["start_t"].to_list()], dtype=float)
    return _PoolArrays(
        segment_id=np.array(df["segment_id"].to_list(), dtype=object),
        vessel_id=np.array(df["vessel_id"].to_list(), dtype=object),
        vessel_class=np.array(df["vessel_class"].to_list(), dtype=object),
        t0=t0, off=off, n=n, lat=lat, lon=lon, seg=seg,
        cell_sorted=keys[order], cell_order=idx[order], step_s=step_minutes * 60.0,
    )  # fmt: skip


def _manifest_key(manifest: SplitManifest) -> str:
    return hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()


def load_pool(store, dataset_id: str, manifest: SplitManifest, until: datetime,
              cfg: RetrievalConfig) -> _PoolArrays:  # fmt: skip
    """Eligible segments for every cutoff whose pool bound is `until`, built from data before
    `until` only. The target-specific exclusion (P-unseen) is applied per query."""
    with _POOL_LOCK:
        return _load_pool(store, dataset_id, manifest, until, cfg)


def _load_pool(store, dataset_id, manifest, until, cfg) -> _PoolArrays:
    start = until - timedelta(days=cfg.index_days) if cfg.index_days else None
    end = until + timedelta(microseconds=1)  # known_at <= until; data at `until` is causal
    files = store.files(dataset_id, start, end)
    version = tuple((str(f), f.stat().st_size, f.stat().st_mtime_ns) for f in files)
    key = (str(Path(store.root).resolve()), dataset_id, until, start, version,
           _manifest_key(manifest), cfg.pool, json.dumps(cfg.index.as_dict(), sort_keys=True,
                                                         default=str))  # fmt: skip
    if key in _POOL_CACHE:
        _POOL_CACHE.move_to_end(key)
        return _POOL_CACHE[key]
    if not files:
        index = pl.DataFrame(schema={"segment_id": pl.String})
        arrays = None
    else:
        index = build_segment_index(store, dataset_id, cfg.index, end=end, start=start)
        # Target-independent part of eligible(); "" is never a vessel id.
        index = eligible(index, until, "", manifest, "rolling")
        arrays = pool_arrays(index, cfg.index.step_minutes) if index.height else None
    if arrays is None:
        arrays = _empty_pool(cfg.index.step_minutes)
    _POOL_CACHE[key] = arrays
    while len(_POOL_CACHE) > _POOL_CACHE_SIZE:
        _POOL_CACHE.popitem(last=False)
    return arrays


def _empty_pool(step_minutes: int) -> _PoolArrays:
    e = np.array([], dtype=object)
    i = np.array([], dtype=np.int64)
    f = np.array([], dtype=float)
    return _PoolArrays(e, e, e, f, i, i, f, f, i, i, i, step_minutes * 60.0)


def _xy_at(pool: _PoolArrays, frame: LocalFrame, seg: np.ndarray, t: np.ndarray):
    """Reference positions (local frame) of segments `seg` at times `t` (epoch s), linearly
    interpolated between consecutive 10-min track points; NaN outside the track."""
    seg, t = np.broadcast_arrays(np.asarray(seg), np.asarray(t, float))
    f = (t - pool.t0[seg]) / pool.step_s
    n = pool.n[seg]
    valid = (f >= -1e-9) & (f <= n - 1 + 1e-9)
    fc = np.clip(f, 0, np.maximum(n - 1, 0))
    i0 = np.minimum(np.floor(fc).astype(np.int64), np.maximum(n - 1, 0))
    i1 = np.minimum(i0 + 1, n - 1)
    w = fc - i0
    a = pool.off[seg] + i0
    b = pool.off[seg] + i1
    xa, ya = frame.to_xy(pool.lat[a], pool.lon[a])
    xb, yb = frame.to_xy(pool.lat[b], pool.lon[b])
    x = xa + w * (xb - xa)
    y = ya + w * (yb - ya)
    x[~valid] = np.nan
    y[~valid] = np.nan
    return x, y


def _context_xy(history: AISHistory, frame: LocalFrame, t_anchor: float, steps: int,
                step_s: float, gap_s: float) -> np.ndarray:  # fmt: skip
    """Target positions at t_anchor - k*step (k = 1..steps), interpolated only between two of
    its own reports at most `gap_s` apart; stops at the first step that would bridge a gap.
    Returns (m, 2) with m <= steps."""
    r = history.reports
    t = np.array([x.timestamp() for x in r["observed_at"].to_list()], dtype=float)
    lat = r["latitude_deg"].to_numpy().astype(float)
    lon = r["longitude_deg"].to_numpy().astype(float)
    out = []
    for k in range(1, steps + 1):
        tk = t_anchor - k * step_s
        i = int(np.searchsorted(t, tk, side="right")) - 1
        if i < 0 or i + 1 >= len(t) or t[i + 1] - t[i] > gap_s:
            break
        w = (tk - t[i]) / (t[i + 1] - t[i]) if t[i + 1] > t[i] else 0.0
        x, y = frame.to_xy(lat[i : i + 2], lon[i : i + 2])
        out.append((x[0] + w * (x[1] - x[0]), y[0] + w * (y[1] - y[0])))
    return np.array(out, dtype=float).reshape(-1, 2)


def _angle_diff(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((np.asarray(a) - b + 180.0) % 360.0 - 180.0)


@dataclass
class RouteRetrieval(BaseForecaster):
    model_id: str = MODEL_ID
    config: dict = field(default_factory=dict)
    cfg: RetrievalConfig = field(default_factory=RetrievalConfig)
    store: object | None = None
    dataset_id: str | None = None
    manifest: SplitManifest | None = None
    unavailable_reason: str = "no store, dataset or split manifest in the model context"

    @classmethod
    def from_context(cls, ctx: ModelContext) -> RouteRetrieval:
        params = dict(ctx.params or {})
        cfg = RetrievalConfig()
        overrides = params.get("config", {})
        if overrides:
            overrides = {k: tuple(v) if k == "radii_m" else v for k, v in overrides.items()}
            cfg = replace(cfg, **overrides)
        manifest, why = None, ""
        if "manifest" in params:  # explicit (tests, invariance harness)
            m = params["manifest"]
            if isinstance(m, str):
                m = SplitManifest.model_validate_json(m)
            manifest = m if isinstance(m, SplitManifest) else SplitManifest.model_validate(m)
            sha = _manifest_key(manifest)
            if ctx.protocol and manifest.protocol != ctx.protocol:
                why = f"explicit manifest is {manifest.protocol}, the context is {ctx.protocol}"
        elif ctx.protocol and ctx.dataset_id:
            p = MANIFEST_DIR / "splits" / f"{ctx.protocol}__{ctx.dataset_id}.json"
            if not p.exists():
                why = f"no {ctx.protocol} split manifest for {ctx.dataset_id}; run make splits"
            else:
                raw = p.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                if ctx.manifest_sha256 is None or sha != ctx.manifest_sha256:
                    why = "the split manifest on disk differs from the one under evaluation"
                else:
                    manifest = SplitManifest.model_validate_json(raw)
        if ctx.store is None or ctx.dataset_id is None or (manifest is None and not why):
            return cls(cfg=cfg)
        if manifest is not None and manifest.region_split is not None:
            why = "P-region retrieval needs side-aware eligibility; not built"
        if (
            manifest is not None
            and cfg.pool == "frozen"
            and not any(p.split == "train" for p in manifest.periods or [])
        ):
            why = f"{manifest.protocol} has no train period; the frozen pool needs one"
        config = cfg.as_dict()
        if manifest is not None and not why:
            config |= {
                "protocol": manifest.protocol,
                "manifest_sha256": sha,
                "index_version": INDEX_VERSION,
                # The frozen pool is fitted on the train period: scoring on train is in-sample.
                "fit_split": "train" if cfg.pool == "frozen" else None,
                # NOAA back-fills vessel type: the class term is not as-of evidence (§4).
                "class_source": "backfilled",
                # Rolling or truncated pools are labelled variants, never headline (§5 U1).
                "diagnostic_only": cfg.pool != "frozen" or cfg.index_days is not None,
            }
        return cls(config=config, cfg=cfg, store=ctx.store, dataset_id=ctx.dataset_id,
                   manifest=None if why else manifest, unavailable_reason=why)  # fmt: skip

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            model_version="1.0.0",
            min_history_reports=1,
            reads_beyond_own_history=True,  # other vessels' historical segments
            uses_store_at_predict=True,  # builds its segment pool from the store
            installed=not self.unavailable_reason,
            description="Nearest historical segments (protocol-eligible, completed before the "
            "cutoff's pool bound) aligned at the current position and followed forward; "
            "central path is the similarity-weighted medoid. Similarity is not a probability.",
        )

    # -- prediction ----------------------------------------------------------------------
    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        if self.unavailable_reason:
            return self.unsupported(horizon, "not_available", self.unavailable_reason)
        cfg, manifest = self.cfg, self.manifest
        until = pool_until(history.as_of, manifest, cfg.pool)
        pool = load_pool(self.store, self.dataset_id, manifest, until, cfg)
        last = history.last()
        anchor, fell_back = select_anchor(history)
        t_a = last["observed_at"].timestamp()
        frame = LocalFrame(last["latitude_deg"], last["longitude_deg"])
        leads = np.array([t.timestamp() - t_a for t in horizon.target_times])
        q_ctx = _context_xy(history, frame, t_a, cfg.context_steps, pool.step_s,
                            cfg.index.segment.gap_minutes * 60)  # fmt: skip
        sog = anchor["sog_knots"] if anchor else None
        cog = anchor["cog_deg"] if anchor else None
        target_class = history_vessel_class(history)
        exclude = history.vessel_id if manifest.vessel_assignment is not None else None

        found, radius = None, None
        for r in cfg.radii_m:
            found = self._match(pool, frame, r, t_a, leads, q_ctx, sog, cog, target_class,
                                exclude)  # fmt: skip
            if found is not None:
                radius = r
                break
        if found is None:
            return self._result(
                horizon, status="failed", points=[],
                error={"code": "no_references",
                       "message": f"no eligible segment within {max(cfg.radii_m):.0f} m of the "
                                  "last report"},
            )  # fmt: skip
        segs, tau, dist = found
        rx, ry = _xy_at(pool, frame, segs, tau)  # reference position at the alignment time
        fx, fy = _xy_at(pool, frame, segs[:, None], tau[:, None] + leads[None, :])
        px, py = fx - rx[:, None], fy - ry[:, None]
        covered = np.isfinite(px) & np.isfinite(py)
        n_cov = np.where(covered.all(1), covered.shape[1], np.argmin(covered, axis=1))
        sim = np.exp(-dist / 2.0)
        central = self._medoid(px, py, covered, n_cov, sim)
        # Hold the last referenced position once a reference runs out.
        for i in range(len(segs)):
            c = n_cov[i]
            hx, hy = (px[i, c - 1], py[i, c - 1]) if c else (0.0, 0.0)
            px[i, c:], py[i, c:] = hx, hy
        lat, lon = frame.to_latlon(px, py)
        tt = horizon.target_times
        trajectories = [
            {
                "trajectory_id": f"ref{i + 1}",
                "points": [
                    {"timestamp": t, "latitude_deg": float(a), "longitude_deg": float(o)}
                    for t, a, o in zip(tt, lat[i], lon[i], strict=True)
                ],
                "similarity": float(sim[i]),
                "reference_segment_ids": [str(pool.segment_id[segs[i]])],
                "reference_ends_at": None
                if n_cov[i] == len(tt)
                else tt[n_cov[i] - 1]
                if n_cov[i]
                else history.as_of,
            }  # fmt: skip
            for i in range(len(segs))
        ]
        warnings = []
        if radius != cfg.radii_m[0]:
            warnings.append(f"no reference within {cfg.radii_m[0]:.0f} m; matched within "
                            f"{radius:.0f} m")  # fmt: skip
        if n_cov[central] < len(tt):
            held = (tt[max(n_cov[central] - 1, 0)] - history.as_of).total_seconds() / 3600
            warnings.append(f"central reference ends {held:.1f} h after as_of; later points "
                            "hold its last position")  # fmt: skip
        own = sum(pool.vessel_id[s] == history.vessel_id for s in segs)
        if own:
            warnings.append(f"{own} of {len(segs)} references are the target's own earlier "
                            "voyages (allowed under this protocol)")  # fmt: skip
        if anchor is None:
            warnings.append("no usable SOG/COG in the history; matched on position and shape")
        elif fell_back:
            warnings.append("latest report lacks usable SOG/COG; course and speed from an "
                            "earlier report")  # fmt: skip
        age_min = (history.as_of - last["observed_at"]).total_seconds() / 60
        if age_min > STALE_WARNING_MINUTES:
            warnings.append(f"last report is {age_min:.0f} min older than as_of")
        extra = {"trajectories": trajectories, "central_trajectory_id": f"ref{central + 1}"}
        return lat[central].tolist(), lon[central].tolist(), warnings, extra

    def _match(self, pool, frame, radius, t_a, leads, q_ctx, sog, cog, target_class, exclude):
        """Top-K (segment index, tau, D) within `radius` of the anchor, or None."""
        cfg = self.cfg
        if pool.size == 0:
            return None
        lat0, lon0 = frame.lat0, frame.lon0
        # Look up points within radius + EDGE_REACH: an edge can pass near the anchor with
        # both of its endpoints farther away.
        coslat = max(math.cos(math.radians(lat0)), 1e-6)
        dlat = (radius + EDGE_REACH_M) / 110_574.0
        dlon = (radius + EDGE_REACH_M) / (111_320.0 * coslat)

        def span(c, d):
            return np.arange(math.floor((c - d) / CELL_DEG), math.floor((c + d) / CELL_DEG) + 1)

        ci, cj = span(lat0, dlat), span(lon0, dlon)  # no antimeridian wrap (Gulf data only)
        keys = ((ci[:, None] + 4000) * 100_000 + (cj[None, :] + 8000)).ravel()
        lo = np.searchsorted(pool.cell_sorted, keys, side="left")
        hi = np.searchsorted(pool.cell_sorted, keys, side="right")
        if not (hi > lo).any():
            return None
        pts = np.concatenate([pool.cell_order[a:b] for a, b in zip(lo, hi, strict=True)])

        def equirect(i):  # PREFILTER ONLY: < 0.1% error at these ranges; never scored
            x = (((pool.lon[i] - lon0 + 540.0) % 360.0) - 180.0) * 111_320.0 * coslat
            return x, (pool.lat[i] - lat0) * 110_574.0

        px, py = equirect(pts)
        pts = pts[np.hypot(px, py) <= radius + EDGE_REACH_M]
        e = np.unique(np.concatenate([pts, pts - 1]))  # edges (e, e+1) touching a nearby point
        e = e[(e >= 0) & (e + 1 < len(pool.seg))]
        e = e[pool.seg[e] == pool.seg[e + 1]]
        e = e[np.isfinite(pool.lat[e + 1]) & np.isfinite(pool.lat[e])]
        if exclude is not None:
            e = e[pool.vessel_id[pool.seg[e]] != exclude]
        if not len(e):
            return None
        need = min(cfg.min_reference_future_min * 60, leads[-1])
        # Pass 1 (equirectangular, cheap): edges within reach (with a margin), and only each
        # segment's PASS1_KEEP best, so a segment never hinges on one approximate ranking.
        # Pass 2 (local frame, exact): those are projected, filtered and scored exactly.
        reach = radius * 1.02 + 100.0
        e = self._best_edges(pool, e, *equirect(e), *equirect(e + 1), reach, need, sog, cog,
                             target_class, keep_per_segment=PASS1_KEEP)[0]  # fmt: skip
        if not len(e):
            return None
        xa, ya = frame.to_xy(pool.lat[e], pool.lon[e])
        xb, yb = frame.to_xy(pool.lat[e + 1], pool.lon[e + 1])
        e, seg, tau, d = self._best_edges(pool, e, xa, ya, xb, yb, radius, need, sog, cog,
                                          target_class)  # fmt: skip
        if not len(e):
            return None
        if len(q_ctx):
            m = len(q_ctx)
            ks = np.arange(1, m + 1) * pool.step_s
            rx0, ry0 = _xy_at(pool, frame, seg, tau)
            cx, cy = _xy_at(pool, frame, seg[:, None], tau[:, None] - ks[None, :])
            ex = (cx - rx0[:, None]) - q_ctx[None, :, 0]
            ey = (cy - ry0[:, None]) - q_ctx[None, :, 1]
            sq = ex * ex + ey * ey
            have = np.isfinite(sq)
            cnt = have.sum(1)
            rms = np.sqrt(np.where(have, sq, 0).sum(1) / np.maximum(cnt, 1))
            # A reference with no context before tau has started here: penalize as unmatched.
            rms = np.where(cnt > 0, rms, 2 * cfg.shape_scale_m)
            d = d + (rms / cfg.shape_scale_m) ** 2
        ids = pool.segment_id[seg]
        order = np.lexsort((ids.astype(str), d))
        chosen, per_vessel = [], {}
        for i in order:
            v = pool.vessel_id[seg[i]]
            if per_vessel.get(v, 0) >= cfg.max_per_vessel:
                continue
            per_vessel[v] = per_vessel.get(v, 0) + 1
            chosen.append(i)
            if len(chosen) == cfg.k:
                break
        c = np.array(chosen, dtype=np.int64)
        return seg[c], tau[c], d[c]

    def _best_edges(self, pool, e, xa, ya, xb, yb, radius, need, sog, cog, target_class,
                    keep_per_segment=1):  # fmt: skip
        """Edges (e, e+1), with endpoints in an anchor-centred plane, that pass within
        `radius` and leave at least `need` seconds of reference: the best edge per segment
        as (edge, segment, tau, D without the shape term)."""
        cfg = self.cfg
        dx, dy = xb - xa, yb - ya
        L2 = dx * dx + dy * dy
        u = np.where(L2 > 0, np.clip(-(xa * dx + ya * dy) / np.where(L2 > 0, L2, 1), 0, 1), 0.0)
        off = np.hypot(xa + u * dx, ya + u * dy)
        seg = pool.seg[e]
        tau = pool.t0[seg] + (e - pool.off[seg] + u) * pool.step_s
        t_end = pool.t0[seg] + (pool.n[seg] - 1) * pool.step_s
        keep = (off <= radius) & (t_end - tau >= need)
        e, seg, tau, off, dx, dy = e[keep], seg[keep], tau[keep], off[keep], dx[keep], dy[keep]
        r_sog = np.hypot(dx, dy) / pool.step_s / KNOT_MPS
        r_cog = np.degrees(np.arctan2(dx, dy)) % 360.0
        d = (off / cfg.pos_scale_m) ** 2
        if sog is not None:
            d = d + ((r_sog - sog) / cfg.sog_scale_kn) ** 2
            if cog is not None and sog >= cfg.moving_kn:
                moving = r_sog >= cfg.moving_kn
                d = d + np.where(moving, (_angle_diff(r_cog, cog) / cfg.cog_scale_deg) ** 2, 0.0)
        d = d + np.where(pool.vessel_class[seg] != target_class, cfg.class_penalty, 0.0)
        order = np.lexsort((tau, d, seg))  # per segment: lowest D, then earliest tau
        s_sorted = seg[order]
        starts = np.flatnonzero(np.r_[True, s_sorted[1:] != s_sorted[:-1]])
        rank = np.arange(len(order)) - np.repeat(starts, np.diff(np.r_[starts, len(order)]))
        best = order[rank < keep_per_segment]
        return e[best], seg[best], tau[best], d[best]

    def _medoid(self, px, py, covered, n_cov, sim) -> int:
        """Index of the similarity-weighted medoid among candidates covering enough of the
        horizon. Distance between two candidates: mean separation over leads both cover."""
        k = len(sim)
        if k == 1:
            return 0
        sx = np.where(covered, px, 0.0)
        sy = np.where(covered, py, 0.0)
        both = covered[:, None, :] & covered[None, :, :]
        sep = np.hypot(sx[:, None, :] - sx[None, :, :], sy[:, None, :] - sy[None, :, :])
        cnt = both.sum(2)
        dist = np.where(cnt > 0, np.where(both, sep, 0).sum(2) / np.maximum(cnt, 1), 1e12)
        score = (dist * sim[None, :]).sum(1)
        eligible_c = n_cov >= self.cfg.medoid_min_coverage * n_cov.max()
        score = np.where(eligible_c, score, np.inf)
        return int(np.argmin(score))  # ties: the better-ranked candidate
