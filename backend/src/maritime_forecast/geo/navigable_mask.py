"""Navigable-water mask for feasibility scoring (build plan §2, §6.5).

Two layers on one lon/lat raster (cell `cell_deg`, default 0.001° ≈ 100 m) over a region:

  land     OSM land polygons (scripts/fetch_land_polygons.py), rasterized at cell centres.
           OSM land is "everything that is not sea": bays behind the coastline, ship
           channels, rivers and lakes are LAND in it. Alone it puts most port traffic on land.
  traffic  cells used by at least `min_vessels` distinct vessels in the protocol's TRAIN
           split, dilated by `dilate_cells`. Fit only on protocol-permitted data (§6.2).

A position is infeasible when it is on land and not in a traffic cell. Positions outside the
raster are unknown and never flagged. A forecast is never constrained by the mask; it is only
flagged (evaluation.feasibility). Natural Earth is never used here.

Segments between consecutive forecast points are sampled as straight lines in lon/lat at
about half a cell. Over one grid step (a few km) that line departs from the geodesic by
metres, far below the mask's resolution; this is the documented exception to "no flat-earth".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import shapely

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR, BBox

GEO_DIR = PROCESSED_DIR / "geo"
MASK_MANIFEST_DIR = MANIFEST_DIR / "masks"
DEFAULT_CELL_DEG = 0.001


def land_polygons_path(region_name: str) -> Path:
    return GEO_DIR / f"osm_land_{region_name}.parquet"


def land_source_id(region_name: str) -> str:
    """'osm_land_<region>@<sha256 prefix of the source zip>' from the fetch manifest."""
    p = MANIFEST_DIR / f"osm_land_{region_name}.json"
    sha = json.loads(p.read_text())["sha256"][:12] if p.exists() else "unknown"
    return f"osm_land_{region_name}@{sha}"


@dataclass(frozen=True)
class Grid:
    lat0: float  # south edge
    lon0: float  # west edge
    cell_deg: float
    ny: int
    nx: int

    @classmethod
    def for_bbox(cls, box: BBox, cell_deg: float = DEFAULT_CELL_DEG) -> Grid:
        ny = int(np.ceil(round((box.lat_max - box.lat_min) / cell_deg, 9)))
        nx = int(np.ceil(round((box.lon_max - box.lon_min) / cell_deg, 9)))
        return cls(box.lat_min, box.lon_min, cell_deg, ny, nx)

    def index(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(row, col, inside) for positions; rows/cols are clipped where not inside."""
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        with np.errstate(invalid="ignore"):
            r = np.floor((lat - self.lat0) / self.cell_deg)
            c = np.floor((lon - self.lon0) / self.cell_deg)
        inside = np.isfinite(r) & np.isfinite(c) & (r >= 0) & (r < self.ny) & (c >= 0)
        inside &= c < self.nx
        r = np.where(inside, r, 0).astype(np.int64)
        c = np.where(inside, c, 0).astype(np.int64)
        return r, c, inside

    def as_dict(self) -> dict:
        return {"lat0": self.lat0, "lon0": self.lon0, "cell_deg": self.cell_deg,
                "ny": self.ny, "nx": self.nx}  # fmt: skip


def rasterize_polygons(polygons, grid: Grid) -> np.ndarray:
    """bool[ny, nx]: cell centre inside any polygon (lon/lat polygons)."""
    out = np.zeros((grid.ny, grid.nx), dtype=bool)
    for poly in polygons:
        x0, y0, x1, y1 = poly.bounds
        c0 = max(int(np.floor((x0 - grid.lon0) / grid.cell_deg - 0.5)), 0)
        c1 = min(int(np.ceil((x1 - grid.lon0) / grid.cell_deg - 0.5)), grid.nx - 1)
        r0 = max(int(np.floor((y0 - grid.lat0) / grid.cell_deg - 0.5)), 0)
        r1 = min(int(np.ceil((y1 - grid.lat0) / grid.cell_deg - 0.5)), grid.ny - 1)
        if c1 < c0 or r1 < r0:
            continue
        xs = grid.lon0 + (np.arange(c0, c1 + 1) + 0.5) * grid.cell_deg
        ys = grid.lat0 + (np.arange(r0, r1 + 1) + 0.5) * grid.cell_deg
        if poly.equals(poly.envelope):  # a whole tile: no point-in-polygon needed
            cx = (xs >= x0) & (xs <= x1)
            cy = (ys >= y0) & (ys <= y1)
            out[r0 : r1 + 1, c0 : c1 + 1] |= cy[:, None] & cx[None, :]
            continue
        shapely.prepare(poly)
        X, Y = np.meshgrid(xs, ys)
        out[r0 : r1 + 1, c0 : c1 + 1] |= shapely.contains_xy(poly, X, Y)
    return out


def dilate(mask: np.ndarray, cells: int) -> np.ndarray:
    """Square (Chebyshev) dilation by `cells`."""
    out = mask.copy()
    for _ in range(cells):
        m = out.copy()
        m[1:, :] |= out[:-1, :]
        m[:-1, :] |= out[1:, :]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        m[1:, 1:] |= out[:-1, :-1]
        m[1:, :-1] |= out[:-1, 1:]
        m[:-1, 1:] |= out[1:, :-1]
        m[:-1, :-1] |= out[1:, 1:]
        out = m
    return out


def traffic_cells_sql(source: str, grid: Grid) -> str:
    """SQL over `source(vessel_id, latitude_deg, longitude_deg)`: (r, c, n_vessels)."""
    return f"""
    SELECT r, c, count(DISTINCT vessel_id) AS n_vessels FROM (
        SELECT vessel_id,
               CAST(floor((latitude_deg - {grid.lat0}) / {grid.cell_deg}) AS BIGINT) AS r,
               CAST(floor((longitude_deg - {grid.lon0}) / {grid.cell_deg}) AS BIGINT) AS c
        FROM {source}
    ) WHERE r >= 0 AND r < {grid.ny} AND c >= 0 AND c < {grid.nx}
    GROUP BY r, c
    """


def traffic_raster(cells: pl.DataFrame, grid: Grid, min_vessels: int, dilate_cells: int):
    out = np.zeros((grid.ny, grid.nx), dtype=bool)
    keep = cells.filter(pl.col("n_vessels") >= min_vessels)
    out[keep["r"].to_numpy(), keep["c"].to_numpy()] = True
    return dilate(out, dilate_cells)


@dataclass(frozen=True)
class NavigableMask:
    grid: Grid
    infeasible: np.ndarray  # bool[ny, nx]: land and not traffic
    source_id: str  # provenance: land source + traffic fit

    @classmethod
    def build(cls, grid: Grid, land: np.ndarray, traffic: np.ndarray | None, source_id: str):
        bad = land if traffic is None else land & ~traffic
        return cls(grid, bad, source_id)

    def is_infeasible(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Same shape as lat; unknown (outside the raster, NaN) is False."""
        lat = np.asarray(lat, float)
        r, c, inside = self.grid.index(lat.ravel(), np.asarray(lon, float).ravel())
        return (self.infeasible[r, c] & inside).reshape(lat.shape)

    def segment_infeasible(self, lat0, lon0, lat1, lon1, chunk: int = 1_000_000) -> np.ndarray:
        """Whether any sample along each segment is infeasible (endpoints included)."""
        a = [np.asarray(x, float).ravel() for x in (lat0, lon0, lat1, lon1)]
        shape = np.asarray(lat0).shape
        out = np.zeros(a[0].shape, dtype=bool)
        step = self.grid.cell_deg / 2
        for s in range(0, a[0].size, chunk):
            la0, lo0, la1, lo1 = (x[s : s + chunk] for x in a)
            ok = np.isfinite(la0) & np.isfinite(lo0) & np.isfinite(la1) & np.isfinite(lo1)
            ok &= np.abs(lo1 - lo0) < 180  # an antimeridian crossing is not a short line
            span = np.where(ok, np.maximum(np.abs(la1 - la0), np.abs(lo1 - lo0)), 0.0)
            n = np.ceil(span / step).astype(np.int64) + 1  # samples per segment
            seg = np.repeat(np.arange(la0.size), n)
            first = np.repeat(np.cumsum(n) - n, n)
            frac = (np.arange(seg.size) - first) / np.maximum(np.repeat(n, n) - 1, 1)
            lat = la0[seg] + frac * (la1[seg] - la0[seg])
            lon = lo0[seg] + frac * (lo1[seg] - lo0[seg])
            hit = self.is_infeasible(lat, lon) & np.repeat(ok, n)
            out[s : s + chunk] = np.bincount(seg[hit], minlength=la0.size) > 0
        return out.reshape(shape)

    # -- persistence -------------------------------------------------------------------------
    def save(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path, infeasible=np.packbits(self.infeasible, axis=None),
            grid=json.dumps(self.grid.as_dict()), source_id=self.source_id,
        )  # fmt: skip
        return sha256_file(path)

    @classmethod
    def load(cls, path: Path) -> NavigableMask:
        z = np.load(path, allow_pickle=False)
        grid = Grid(**json.loads(str(z["grid"])))
        bits = np.unpackbits(z["infeasible"], count=grid.ny * grid.nx).astype(bool)
        return cls(grid, bits.reshape(grid.ny, grid.nx), str(z["source_id"]))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def land_raster_path(region_name: str, cell_deg: float) -> Path:
    return GEO_DIR / f"osm_land_{region_name}_{cell_deg:g}.npz"


def mask_path(protocol: str, dataset_id: str) -> Path:
    return GEO_DIR / f"navigable_{protocol}__{dataset_id}.npz"


def mask_sidecar_path(protocol: str, dataset_id: str) -> Path:
    return MASK_MANIFEST_DIR / f"navigable_{protocol}__{dataset_id}.json"


# -- protocol-aware fit (§6.2: density masks only from protocol-permitted data) ------------
@dataclass(frozen=True)
class TrafficConfig:
    # Chosen on each protocol's CALIBRATION split (never test) with
    # scripts/tune_navigable_mask.py; see docs/evaluation-protocol.md.
    min_vessels: int = 2
    dilate_cells: int = 1
    cell_deg: float = DEFAULT_CELL_DEG
    split: str = "train"


def traffic_source_sql(
    store, dataset_id: str, manifest, split: str, extra_cols: tuple[str, ...] = ()
) -> tuple[str, dict]:
    """SQL source of the reports a protocol permits for `split`, and what it covers.

    Time: the split's periods only. Vessels: under a vessel-disjoint manifest, only vessels
    assigned to `split`. Quality-flagged reports are excluded."""
    if manifest.region_split is not None:
        raise NotImplementedError("P-region traffic masks need per-side fits; not built yet")
    periods = [p for p in manifest.periods or [] if p.split == split]
    if not periods:
        raise ValueError(f"manifest has no {split!r} period")
    lo, hi = min(p.start for p in periods), max(p.end for p in periods)
    files = store.files(dataset_id, lo, hi)
    when = " OR ".join(
        f"(observed_at >= TIMESTAMPTZ '{p.start.isoformat()}' "
        f"AND observed_at < TIMESTAMPTZ '{p.end.isoformat()}')"
        for p in periods
    )
    vessel_filter = ""
    n_vessels = None
    if manifest.vessel_assignment is not None:
        vids = store.con.execute(
            "SELECT DISTINCT vessel_id FROM read_parquet(["
            + ", ".join(f"'{f}'" for f in files) + "], hive_partitioning=false)"
        ).pl()["vessel_id"].to_list()  # fmt: skip
        keep = sorted(v for v in vids if manifest.vessel_assignment.assign(v) == split)
        n_vessels = len(keep)
        store.con.execute("CREATE OR REPLACE TEMP TABLE _mask_vessels (vessel_id VARCHAR)")
        store.con.executemany("INSERT INTO _mask_vessels VALUES (?)", [(v,) for v in keep])
        vessel_filter = " AND vessel_id IN (SELECT vessel_id FROM _mask_vessels)"
    cols = ", ".join(("vessel_id", "latitude_deg", "longitude_deg", *extra_cols))
    src = (
        f"(SELECT {cols} FROM read_parquet(["
        + ", ".join(f"'{f}'" for f in files)
        + f"], hive_partitioning=false) WHERE len(quality_flags) = 0 AND ({when}){vessel_filter})"
    )
    cover = {
        "split": split,
        "periods": [[p.start.isoformat(), p.end.isoformat()] for p in periods],
        "data_until": hi.isoformat(),
        "vessel_disjoint": manifest.vessel_assignment is not None,
        "n_vessels_permitted": n_vessels,
    }
    return src, cover


def load_land_raster(box: BBox, cell_deg: float = DEFAULT_CELL_DEG) -> tuple[Grid, np.ndarray]:
    """The cached OSM land raster for a region, built from the polygons on first use."""
    grid = Grid.for_bbox(box, cell_deg)
    p = land_raster_path(box.name, cell_deg)
    if p.exists():
        z = np.load(p, allow_pickle=False)
        land = np.unpackbits(z["land"], count=grid.ny * grid.nx).astype(bool)
        return grid, land.reshape(grid.ny, grid.nx)
    src = land_polygons_path(box.name)
    if not src.exists():
        raise FileNotFoundError(f"no land polygons at {src}; run scripts/fetch_land_polygons.py")
    polys = shapely.from_wkb(pl.read_parquet(src, columns=["wkb"])["wkb"].to_numpy())
    land = rasterize_polygons(polys, grid)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, land=np.packbits(land, axis=None))
    return grid, land


def fit_navigable_mask(
    store, dataset_id: str, manifest, manifest_sha256: str, box: BBox,
    cfg: TrafficConfig | None = None,
) -> tuple[NavigableMask, dict]:  # fmt: skip
    cfg = cfg or TrafficConfig()
    grid, land = load_land_raster(box, cfg.cell_deg)
    src, cover = traffic_source_sql(store, dataset_id, manifest, cfg.split)
    cells = store.con.execute(traffic_cells_sql(src, grid)).pl()
    traffic = traffic_raster(cells, grid, cfg.min_vessels, cfg.dilate_cells)
    land_id = land_source_id(box.name)
    source_id = (
        f"{land_id}+traffic[{manifest.protocol}:{cfg.split},>={cfg.min_vessels}v,"
        f"dilate{cfg.dilate_cells}]"
    )
    mask = NavigableMask.build(grid, land, traffic, source_id)
    sidecar = {
        "protocol": manifest.protocol,
        "dataset_id": dataset_id,
        "manifest_sha256": manifest_sha256,
        "region": box.name,
        "grid": grid.as_dict(),
        "land_source": land_id,
        "traffic": {
            **cfg.__dict__,
            **cover,
            "cells_with_reports": cells.height,
            "cells_kept": int(traffic.sum()),
        },  # fmt: skip
        "land_cells": int(land.sum()),
        "infeasible_cells": int(mask.infeasible.sum()),
        "source_id": source_id,
    }
    return mask, sidecar


def infeasible_report_rates(
    store, dataset_id: str, manifest, mask: NavigableMask, region: BBox, split: str,
    n_sample: int, seed: int,
) -> dict[str, float]:  # fmt: skip
    """Share of real (permitted, unflagged) reports of `split` inside `region` that the mask
    calls infeasible, by motion: stationary (< 0.5 kn) and underway (>= 2 kn)."""
    src, _ = traffic_source_sql(store, dataset_id, manifest, split, ("sog_knots",))
    df = store.con.execute(
        f"SELECT * FROM {src} WHERE {region.sql_predicate()} AND sog_knots IS NOT NULL "
        f"USING SAMPLE reservoir({int(n_sample)} ROWS) REPEATABLE ({int(seed)})"
    ).pl()
    bad = mask.is_infeasible(df["latitude_deg"].to_numpy(), df["longitude_deg"].to_numpy())
    sog = df["sog_knots"].to_numpy()
    return {
        "n_reports": df.height,
        "stationary": float(bad[sog < 0.5].mean()),
        "underway": float(bad[sog >= 2].mean()),
    }


class MaskMismatchError(ValueError):
    pass


def load_navigable_mask(
    protocol: str, dataset_id: str, manifest_sha256: str | None
) -> tuple[NavigableMask, dict] | None:
    """The fitted mask for a protocol, or None when it was never built. Refuses a mask fitted
    for another manifest, or one whose file changed since its sidecar was written."""
    p, sp = mask_path(protocol, dataset_id), mask_sidecar_path(protocol, dataset_id)
    if not p.exists() or not sp.exists():
        return None
    side = json.loads(sp.read_text())
    if manifest_sha256 is not None and side["manifest_sha256"] != manifest_sha256:
        raise MaskMismatchError(f"navigable mask for {protocol} was fitted on another manifest")
    if sha256_file(p) != side["mask_sha256"]:
        raise MaskMismatchError(f"{p} does not match its sidecar checksum; rebuild it")
    return NavigableMask.load(p), side
