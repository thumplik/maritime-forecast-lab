"""Protocol-aware index of completed track segments for route retrieval (build plan §5 R1,
§6.2). Slice 1 of Milestone 3: the index and its eligibility rules; no forecasting.

A segment is a maximal run of one vessel's reports without a gap over `gap_minutes`
(data.segment, with `causal_ends=True`). The index is built once per dataset from the whole
archive; what a forecast may use is decided per cutoff by `eligible()`:

  * **Known complete before the cutoff.** `known_at = end + 2 * gap` must be <= as_of. At
    `end + gap` no further report has arrived, so the segment has ended. One more gap covers
    any spike report inside that first gap: whether it is dropped depends on its own next
    report, which `causal_ends` bounds to one gap later. So every report that decides a
    segment's extent and content is at or before `known_at`. A test checks that eligible
    segments built from the full archive equal those built from data truncated at as_of.
  * **Protocol vessels.** P-known: every vessel, including the target's own earlier voyages.
    Vessel-disjoint protocols (P-unseen): only vessels assigned to the `train` split, and
    never the target. P-region: not built yet (raises).
  * **Time pool** (`pool`):
      - `"frozen"` (default; headline benchmarks): only segments known complete by the end of
        the manifest's TRAIN period(s), so `known_at <= min(as_of, train end)`. The retrieval
        index period is then the training period, disjoint from calibration and test, as
        §5 U1 and §6.2 require. Under P-unseen this is exactly the manifest's training data
        (train vessels in the train period).
      - `"rolling"` (a labelled operational variant, never the headline): any segment known
        complete before the cutoff, including inside the calibration or test period. It is
        causal, and it is what a deployed system would have. But references can then be other
        test samples' truth, which couples their errors.

Each segment carries a regular track: positions every `step_minutes` from its start,
interpolated only between reports of the same segment (whose gaps are <= `gap_minutes`).
Nothing is ever bridged across a gap.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import polars as pl

from maritime_forecast.config import MANIFEST_DIR, PROCESSED_DIR
from maritime_forecast.data.segment import DEFAULT_SEGMENT_CONFIG, SegmentConfig, segmented_sql
from maritime_forecast.evaluation.truth import truth_sql

INDEX_VERSION = "segment-index-v2"  # v2: deterministic class rule, provenance column
Pool = Literal["frozen", "rolling"]
INDEX_ROOT = PROCESSED_DIR / "segment_index"


@dataclass(frozen=True)
class IndexConfig:
    step_minutes: int = 10
    min_reports: int = 3
    min_duration_minutes: float = 10.0
    segment: SegmentConfig = DEFAULT_SEGMENT_CONFIG

    def as_dict(self) -> dict:
        return {**asdict(self), "version": INDEX_VERSION, "causal_ends": True}


def segment_index_sql(source: str, cfg: IndexConfig, causal_ends: bool = True) -> str:
    """One row per segment from `source` (canonical observations): id, vessel, class, span,
    known_at, endpoints, bbox, mean SOG, and the regular track as lists."""
    gap = cfg.segment.gap_minutes
    step = cfg.step_minutes
    return f"""
    WITH s AS ({segmented_sql(source, cfg.segment, causal_ends=causal_ends)}),
    cls AS (  -- most frequent non-null class, ties to the smallest name (history_vessel_class)
        SELECT vessel_id, segment_no, vessel_class FROM (
            SELECT vessel_id, segment_no, vessel_class, count(*) AS n
            FROM s WHERE vessel_class IS NOT NULL GROUP BY ALL
        ) QUALIFY row_number() OVER (
            PARTITION BY vessel_id, segment_no ORDER BY n DESC, vessel_class) = 1
    ),
    seg AS (
        SELECT vessel_id, segment_no,
            min(observed_at) AS start_t, max(observed_at) AS end_t, count(*) AS n_reports,
            -- NOAA back-fills static fields: provenance says the class is not as-of data
            min(static_fields_provenance) AS static_fields_provenance,
            arg_min(latitude_deg, observed_at) AS start_lat,
            arg_min(longitude_deg, observed_at) AS start_lon,
            arg_max(latitude_deg, observed_at) AS end_lat,
            arg_max(longitude_deg, observed_at) AS end_lon,
            min(latitude_deg) AS lat_min, max(latitude_deg) AS lat_max,
            min(longitude_deg) AS lon_min, max(longitude_deg) AS lon_max,
            -- ordered list: a parallel avg() differs in the last bits from run to run
            list_avg(list(sog_knots ORDER BY observed_at)) AS sog_mean
        FROM s GROUP BY vessel_id, segment_no
        HAVING count(*) >= {int(cfg.min_reports)}
           AND epoch(max(observed_at) - min(observed_at)) >= {float(cfg.min_duration_minutes) * 60}
    ),
    grid AS (
        SELECT vessel_id, segment_no AS cutoff_id,
               unnest(generate_series(start_t, end_t, INTERVAL {int(step)} MINUTE)) AS target_t
        FROM seg
    ),
    obs AS (
        SELECT s.vessel_id || '#' || s.segment_no AS vessel_id, s.observed_at,
               s.latitude_deg, s.longitude_deg, s.sog_knots, s.cog_deg
        FROM s
    ),
    tgt AS (SELECT vessel_id || '#' || cutoff_id AS vessel_id, cutoff_id, target_t FROM grid),
    tr AS ({truth_sql("tgt", "obs", gap, prev_cols=("sog_knots", "cog_deg"))}),
    tracks AS (
        SELECT vessel_id AS seg_key,
            list(target_t ORDER BY target_t) AS track_t,
            list(truth_lat ORDER BY target_t) AS track_lat,
            list(truth_lon ORDER BY target_t) AS track_lon,
            list(p_sog_knots ORDER BY target_t) AS track_sog,
            list(p_cog_deg ORDER BY target_t) AS track_cog,
            bool_and(evaluable) AS track_complete
        FROM tr GROUP BY vessel_id
    )
    SELECT seg.vessel_id || '|' || strftime(seg.start_t AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ')
               AS segment_id,
        seg.* EXCLUDE (segment_no),
        coalesce(cls.vessel_class, 'unknown') AS vessel_class,
        seg.end_t + INTERVAL {int(2 * gap * 60)} SECOND AS known_at,
        tracks.* EXCLUDE (seg_key)
    FROM seg JOIN tracks ON tracks.seg_key = seg.vessel_id || '#' || seg.segment_no
    LEFT JOIN cls ON cls.vessel_id = seg.vessel_id AND cls.segment_no = seg.segment_no
    ORDER BY seg.vessel_id, seg.start_t
    """


def build_segment_index(
    store, dataset_id: str, cfg: IndexConfig | None = None, end: datetime | None = None,
    _causal_ends: bool = True, start: datetime | None = None,
) -> pl.DataFrame:  # fmt: skip
    """Index of every segment in the dataset, or in data within [start, end). A forecaster
    builds from data before its pool bound, so post-cutoff data is never even read; `start`
    bounds the build (segments begin no earlier than it).
    `_causal_ends=False` exists only so a test can show the causality check has teeth."""
    cfg = cfg or IndexConfig()
    files = store.files(dataset_id, start, end)
    conds = ([f"observed_at >= TIMESTAMPTZ '{start.isoformat()}'"] if start else []) + (
        [f"observed_at < TIMESTAMPTZ '{end.isoformat()}'"] if end else []
    )
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    paths = ", ".join(f"'{f}'" for f in files)
    src = f"(SELECT * FROM read_parquet([{paths}], hive_partitioning=false){where})"
    return store.con.execute(segment_index_sql(src, cfg, _causal_ends)).pl()


def pool_until(as_of: datetime, manifest, pool: Pool = "frozen") -> datetime:
    """Latest `known_at` a segment may have to be eligible at `as_of`."""
    if pool == "rolling":
        return as_of
    if pool != "frozen":
        raise ValueError(f"unknown pool {pool!r}")
    train = [p.end for p in manifest.periods or [] if p.split == "train"]
    if not train:
        raise ValueError(f"{manifest.protocol} has no train period; a frozen pool needs one")
    return min(as_of, max(train))


def eligible(
    index: pl.DataFrame, as_of: datetime, target_vessel: str, manifest, pool: Pool = "frozen"
) -> pl.DataFrame:
    """Segments a forecast for `target_vessel` at `as_of` may use under `manifest`."""
    if manifest.region_split is not None:
        raise NotImplementedError("P-region retrieval needs side-aware eligibility; not built")
    out = index.filter(pl.col("known_at") <= pool_until(as_of, manifest, pool))
    va = manifest.vessel_assignment
    if va is not None:
        vids = out["vessel_id"].unique().to_list()
        train = [v for v in vids if va.assign(v) == "train" and v != target_vessel]
        out = out.filter(pl.col("vessel_id").is_in(train))
    return out


# -- persistence ---------------------------------------------------------------------------
def index_path(dataset_id: str) -> Path:
    return INDEX_ROOT / dataset_id / "segments.parquet"


def index_sidecar_path(dataset_id: str) -> Path:
    return MANIFEST_DIR / "segment_index" / f"{dataset_id}.json"


def load_segment_index(dataset_id: str) -> pl.DataFrame:
    p = index_path(dataset_id)
    if not p.exists():
        raise FileNotFoundError(f"no segment index at {p}; run scripts/build_segment_index.py")
    side = json.loads(index_sidecar_path(dataset_id).read_text())
    if side.get("config", {}).get("version") != INDEX_VERSION:
        raise ValueError(f"segment index {p} is not {INDEX_VERSION}; rebuild it")
    return pl.read_parquet(p)
