"""Partitioned Parquet store for canonical observations, queried with DuckDB (build plan §2-3).

Layout:  <root>/<dataset_id>/date=YYYY-MM-DD/part-0.parquet   (UTC calendar day of observed_at)

Reads prune partitions by date before DuckDB opens any file. All user-supplied values are
bound as parameters or validated identifiers, never interpolated into SQL or paths.
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

from maritime_forecast.config import PROCESSED_DIR, BBox

CANONICAL_ROOT = PROCESSED_DIR / "canonical"
_DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PART = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def connect() -> duckdb.DuckDBPyConnection:
    """A DuckDB connection with the session settings every query in this project assumes."""
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _check_id(dataset_id: str) -> str:
    if not _DATASET_ID.match(dataset_id) or ".." in dataset_id:
        raise ValueError(f"invalid dataset id {dataset_id!r}")
    return dataset_id


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return t


class ParquetStore:
    def __init__(self, root: Path = CANONICAL_ROOT, con: duckdb.DuckDBPyConnection | None = None):
        self.root = Path(root)
        self.con = con or connect()

    # -- catalogue -----------------------------------------------------------------------
    def datasets(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and _DATASET_ID.match(p.name)
        )

    def partitions(self, dataset_id: str) -> list[date]:
        d = self.root / _check_id(dataset_id)
        if not d.exists():
            raise KeyError(f"unknown dataset {dataset_id!r}")
        out = []
        for p in d.iterdir():
            m = _PART.match(p.name)
            if m and (p / "part-0.parquet").exists():
                out.append(date.fromisoformat(m.group(1)))
        return sorted(out)

    def files(
        self, dataset_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Path]:
        """Partition files overlapping [start, end)."""
        lo = _utc(start).date() if start else date.min
        hi = _utc(end) if end else None
        days = [
            d for d in self.partitions(dataset_id)
            if d >= lo and (hi is None or datetime.combine(d, datetime.min.time(), hi.tzinfo) < hi)
        ]  # fmt: skip
        return [self.root / dataset_id / f"date={d.isoformat()}" / "part-0.parquet" for d in days]

    # -- reads ---------------------------------------------------------------------------
    def scan(
        self,
        dataset_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        bbox: BBox | None = None,
        vessel_ids: Sequence[str] | None = None,
        columns: Sequence[str] | None = None,
        order: bool = True,
    ) -> duckdb.DuckDBPyRelation:
        """Observations with start <= observed_at < end, optionally in bbox / for vessels."""
        files = self.files(dataset_id, start, end)
        if not files:
            raise LookupError(f"no partitions of {dataset_id!r} overlap the requested window")
        cols = "*" if columns is None else ", ".join(f'"{c}"' for c in _check_cols(columns))
        where, params = ["TRUE"], []
        if start is not None:
            where.append("observed_at >= ?")
            params.append(start)
        if end is not None:
            where.append("observed_at < ?")
            params.append(end)
        if bbox is not None:
            where.append("latitude_deg BETWEEN ? AND ? AND longitude_deg BETWEEN ? AND ?")
            params += [bbox.lat_min, bbox.lat_max, bbox.lon_min, bbox.lon_max]
        if vessel_ids is not None:
            where.append("list_contains(?, vessel_id)")
            params.append(list(vessel_ids))
        sql = (
            f"SELECT {cols} FROM read_parquet(?, hive_partitioning=false) "
            f"WHERE {' AND '.join(where)}" + (" ORDER BY vessel_id, observed_at" if order else "")
        )
        return self.con.sql(sql, params=[[str(f) for f in files], *params])

    def track(
        self, dataset_id: str, vessel_id: str, start: datetime | None = None,
        end: datetime | None = None,
    ) -> pl.DataFrame:  # fmt: skip
        """One vessel's observations in [start, end), time-ordered."""
        return self.scan(dataset_id, start, end, vessel_ids=[vessel_id]).pl()

    def history(
        self, dataset_id: str, vessel_id: str, as_of: datetime, hours: float
    ) -> pl.DataFrame:
        """Causal context: the vessel's observations with as_of - hours < observed_at <= as_of.

        This is the only accessor model context should be built from (build plan §6.1).
        """
        as_of = _utc(as_of)
        df = self.scan(
            dataset_id, as_of - timedelta(hours=hours), as_of + timedelta(microseconds=1),
            vessel_ids=[vessel_id],
        ).pl()  # fmt: skip
        return df.filter(pl.col("observed_at") > as_of - timedelta(hours=hours))

    def vessels(
        self,
        dataset_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        bbox: BBox | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> pl.DataFrame:
        """Per-vessel summary (reports, first/last seen, class), paginated by vessel_id."""
        if not (1 <= limit <= 10_000) or offset < 0:
            raise ValueError("limit must be 1..10000 and offset >= 0")
        rel = self.scan(dataset_id, start, end, bbox, order=False)
        return (
            rel.aggregate(
                "vessel_id, count(*) AS reports, min(observed_at) AS first_seen, "
                "max(observed_at) AS last_seen, mode(vessel_class) AS vessel_class",
                "vessel_id",
            )
            .order("vessel_id")
            .limit(limit, offset)
            .pl()
        )

    # -- writes --------------------------------------------------------------------------
    def write(
        self, dataset_id: str, relation_sql: str, expect_days: set[str] | None = None
    ) -> dict[str, int]:
        """Write canonical rows (an SQL query) into daily partitions; return rows per day.

        Each day written replaces that day's partition; other days are untouched. If
        `expect_days` is given and the output contains any other day, nothing is replaced.
        """
        target = self.root / _check_id(dataset_id)
        staging = self.root / f".staging-{dataset_id}-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True)
        try:
            self.con.execute(
                f"COPY (SELECT *, strftime(observed_at, '%Y-%m-%d') AS date FROM ({relation_sql})) "
                f"TO '{staging}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (date), "
                "FILENAME_PATTERN 'part-{i}', OVERWRITE_OR_IGNORE)"
            )
            parts = sorted(staging.iterdir())
            if expect_days is not None:
                extra = {p.name.removeprefix("date=") for p in parts} - set(expect_days)
                if extra:
                    raise ValueError(f"output has rows on unexpected days {sorted(extra)}")
            written: dict[str, int] = {}
            target.mkdir(parents=True, exist_ok=True)
            for part in parts:
                m = _PART.match(part.name)
                files = sorted(part.glob("*.parquet"))
                if not m or len(files) != 1:
                    raise RuntimeError(f"unexpected staging layout in {part}")
                files[0].rename(part / "part-0.parquet")
                dest = target / part.name
                old = target / f".old-{part.name}-{uuid.uuid4().hex[:6]}"
                if dest.exists():
                    dest.rename(old)
                part.rename(dest)
                shutil.rmtree(old, ignore_errors=True)
                n = self.con.execute(
                    "SELECT count(*) FROM read_parquet(?)", [str(dest / "part-0.parquet")]
                ).fetchone()[0]
                written[m.group(1)] = int(n)
            return written
        finally:
            shutil.rmtree(staging, ignore_errors=True)


_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _check_cols(columns: Sequence[str]) -> list[str]:
    bad = [c for c in columns if not _COLUMN.match(c)]
    if bad:
        raise ValueError(f"invalid column names {bad}")
    return list(columns)
