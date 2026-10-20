"""Versioned ingestion: mapped CSV/Parquet files -> canonical partitions + dataset manifest.

    report = ingest(files, load_mapping("noaa_marinecadastre"), "my_dataset", region=...)

Every input row is accounted for in an IngestReport (kept, dropped by reason, duplicate, or
outside the region). The dataset manifest in data/manifests/<dataset_id>.canonical.json
accumulates one entry per input file and the row count of every partition.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from maritime_forecast.config import MANIFEST_DIR, REPO_ROOT, BBox
from maritime_forecast.data.canonical import (
    CANONICAL_VERSION,
    DROP_REASON_SQL,
    canonical_sql,
    staged_sql,
)
from maritime_forecast.data.mapping import SourceMapping
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.dataset import DatasetManifest, IngestReport, InputFile
from maritime_forecast.storage.parquet_store import ParquetStore

INGEST_VERSION = "ingest-v1"
_SUFFIXES = {".csv": "csv", ".gz": "csv", ".parquet": "parquet"}


def manifest_path(dataset_id: str) -> Path:
    return MANIFEST_DIR / f"{dataset_id}.canonical.json"


def source_sql(paths: Sequence[Path], mapping: SourceMapping) -> str:
    """Table function reading the input files as the mapping's file format.

    CSV is read as text (all_varchar) so parsing is decided by the mapping, not by DuckDB's
    type sniffing. A line with the wrong number of fields is an error; a value that cannot be
    parsed (e.g. a stray quote) becomes NULL and the row is dropped *and counted* in the report.
    Nothing is skipped silently.
    """
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(p)
        kind = _SUFFIXES.get(p.suffix.lower())
        if kind is None or kind != mapping.file_format:
            raise ValueError(f"{p.name}: expected {mapping.file_format} input")
    files = "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"
    if mapping.file_format == "parquet":
        return f"read_parquet({files}, hive_partitioning=false, union_by_name=true)"
    delim = mapping.csv_delimiter.replace("'", "''")
    return f"read_csv({files}, all_varchar=true, header=true, delim='{delim}', union_by_name=true)"


def build_report(
    store: ParquetStore, mapping: SourceMapping, src: str, canonical: str, region: BBox | None
) -> IngestReport:
    con = store.con
    rows_in, *_ = con.execute(f"SELECT count(*) FROM ({staged_sql(mapping, src)})").fetchone()
    dropped = dict(
        con.execute(
            f"SELECT reason, count(*) FROM (SELECT {DROP_REASON_SQL} AS reason "
            f"FROM ({staged_sql(mapping, src)})) WHERE reason IS NOT NULL GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    n_canon, *_ = con.execute(f"SELECT count(*) FROM ({canonical})").fetchone()
    outside = 0
    if region is not None:
        outside = con.execute(
            f"SELECT count(*) FROM ({canonical}) WHERE NOT ({region.sql_predicate()})"
        ).fetchone()[0]
    kept = _region_filtered(canonical, region)
    flags = dict(
        con.execute(
            f"SELECT f, count(*) FROM (SELECT unnest(quality_flags) AS f FROM ({kept})) "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    report = IngestReport(
        rows_in=int(rows_in),
        dropped={k: int(v) for k, v in dropped.items()},
        duplicates_removed=int(rows_in - sum(dropped.values()) - n_canon),
        outside_region=int(outside),
        rows_out=int(n_canon - outside),
        flags={k: int(v) for k, v in flags.items()},
    )
    return report


def _region_filtered(canonical: str, region: BBox | None) -> str:
    if region is None:
        return canonical
    return f"SELECT * FROM ({canonical}) WHERE {region.sql_predicate()}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _display_path(p: Path) -> str:
    p = p.resolve()
    return str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)


def ingest(
    paths: Sequence[str | Path],
    mapping: SourceMapping,
    dataset_id: str,
    *,
    store: ParquetStore | None = None,
    region: BBox | None = None,
    expect_days: set[str] | None = None,
    hash_inputs: bool = True,
    write_manifest: bool = True,
    notes: str = "",
) -> tuple[IngestReport, dict[str, int]]:
    """Ingest files into `dataset_id`. Returns (report, rows written per UTC day).

    Day partitions present in the output replace any existing partition for that day. Pass
    `expect_days` to refuse (before anything is replaced) output that spills into other days.
    """
    store = store or ParquetStore()
    files = [Path(p) for p in paths]
    src = source_sql(files, mapping)
    canonical = canonical_sql(mapping, src)
    report = build_report(store, mapping, src, canonical, region)
    if report.rows_in != (
        sum(report.dropped.values()) + report.duplicates_removed + report.outside_region
        + report.rows_out
    ):  # fmt: skip
        raise AssertionError(f"row accounting does not balance: {report}")
    written = store.write(dataset_id, _region_filtered(canonical, region), expect_days=expect_days)
    if sum(written.values()) != report.rows_out:
        raise AssertionError(f"wrote {sum(written.values())} rows, expected {report.rows_out}")

    if write_manifest:
        path = manifest_path(dataset_id)
        if path.exists():
            m = DatasetManifest.model_validate_json(path.read_text())
            if m.mapping_id != mapping.mapping_id:
                raise ValueError(
                    f"{dataset_id} was built with {m.mapping_id}; refusing to mix in "
                    f"{mapping.mapping_id}. Use a new dataset id."
                )
        else:
            m = DatasetManifest(
                dataset_id=dataset_id,
                source_id=mapping.source_id,
                mapping_id=mapping.mapping_id,
                canonical_version=CANONICAL_VERSION,
                ingest_version=INGEST_VERSION,
                region=region.as_dict() if region else None,
                static_fields_provenance=mapping.static_fields_provenance,
                notes=notes,
            )
        now = datetime.now(UTC).replace(microsecond=0)
        shown = {_display_path(f) for f in files}
        m.inputs = [i for i in m.inputs if i.path not in shown] + [
            InputFile(
                path=_display_path(f),
                bytes=f.stat().st_size,
                sha256=_sha256(f) if hash_inputs else None,
                report=report if len(files) == 1 else IngestReport(),
                ingested_at=now,
            )
            for f in files
        ]
        if len(files) > 1:  # one combined report, attached to the first file of the batch
            m.inputs[-len(files)].report = report
        m.partitions = dict(sorted({**m.partitions, **written}.items()))
        m.canonical_version, m.ingest_version = CANONICAL_VERSION, INGEST_VERSION
        m.git_sha, m.updated_at = git_sha(), now
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(m.model_dump_json(indent=2) + "\n")
        tmp.replace(path)
    return report, written
