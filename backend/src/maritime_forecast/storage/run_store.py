"""Immutable benchmark run store (build plan §6.1 step 6).

Layout: data/runs/benchmarks/<run_id>/
    run.json        BenchmarkRun record (written last, by finalize)
    files.json      SHA-256 of every other file in the run, run.json included
    *.parquet       per-sample records, cutoffs
    report.md       human-readable summary

A run directory is created exclusively (an existing run_id is an error), files can only be
added before finalize, and finalize makes everything read-only. load() re-verifies every
checksum (run.json included) and refuses added files or directories.

Limitation: the checksums live inside the run, so someone able to rewrite files.json can
defeat them. finalize() returns the files.json digest; record it outside the run (the
implementation log) when a result matters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import polars as pl

from maritime_forecast.config import RUNS_DIR
from maritime_forecast.schemas.benchmark import BenchmarkRun

BENCH_ROOT = RUNS_DIR / "benchmarks"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class RunExistsError(FileExistsError):
    pass


class RunTamperedError(RuntimeError):
    pass


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class RunStore:
    def __init__(self, root: Path = BENCH_ROOT):
        self.root = Path(root)

    def _dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id) or ".." in run_id:
            raise ValueError(f"invalid run id {run_id!r}")
        return self.root / run_id

    def path(self, run_id: str) -> Path:
        """The run's directory (validated id; it may not exist)."""
        return self._dir(run_id)

    def create(self, run_id: str) -> Path:
        d = self._dir(run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir()
        except FileExistsError:
            raise RunExistsError(f"run {run_id} already exists; runs are immutable") from None
        return d

    def _writable(self, run_id: str, name: str) -> Path:
        d = self._dir(run_id)
        if not d.is_dir():
            raise FileNotFoundError(f"run {run_id} was not created")
        if (d / "run.json").exists():
            raise PermissionError(f"run {run_id} is finalized and immutable")
        p = d / name
        if p.exists() or "/" in name or name in ("run.json", "files.json"):
            raise PermissionError(f"refusing to write {name} in run {run_id}")
        return p

    def write_table(self, run_id: str, name: str, df: pl.DataFrame) -> None:
        df.write_parquet(self._writable(run_id, f"{name}.parquet"))

    def write_text(self, run_id: str, name: str, text: str) -> None:
        self._writable(run_id, name).write_text(text)

    def finalize(self, run_id: str, run: BenchmarkRun) -> str:
        """Write run.json, checksum everything, make the run read-only; returns the
        files.json digest (record it outside the run for external verification)."""
        if run.run_id != run_id:
            raise ValueError("run.run_id must match the directory")
        d = self._dir(run_id)
        self._writable(run_id, "_check")  # not finalized yet
        if any(p.is_dir() for p in d.iterdir()):
            raise PermissionError(f"run {run_id} contains a directory")
        (d / "run.json").write_text(run.model_dump_json(indent=2) + "\n")
        files = {p.name: _sha256(p) for p in sorted(d.iterdir()) if p.is_file()}
        (d / "files.json").write_text(json.dumps(files, indent=2, sort_keys=True) + "\n")
        for p in d.iterdir():
            os.chmod(p, _READ_ONLY)
        os.chmod(d, _READ_ONLY | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return _sha256(d / "files.json")

    def load(self, run_id: str) -> BenchmarkRun:
        d = self._dir(run_id)
        recorded = json.loads((d / "files.json").read_text())
        if any(p.is_dir() for p in d.iterdir()):
            raise RunTamperedError(f"run {run_id}: a directory was added after finalize")
        present = {p.name for p in d.iterdir() if p.is_file()} - {"files.json"}
        if present != set(recorded):
            raise RunTamperedError(f"run {run_id}: files added or removed after finalize")
        for name, digest in recorded.items():
            if _sha256(d / name) != digest:
                raise RunTamperedError(f"run {run_id}: {name} changed after finalize")
        return BenchmarkRun.model_validate_json((d / "run.json").read_text())

    def table(self, run_id: str, name: str) -> pl.DataFrame:
        self.load(run_id)  # verify integrity first
        return pl.read_parquet(self._dir(run_id) / f"{name}.parquet")

    def runs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "run.json").exists())
