"""Benchmark runs launched by the API (build plan §8): one local background process each.

A run is the runner's own immutable record (storage.run_store); this module only starts the
process and reports where a run stands. Status comes from the run store, never from memory
alone: a finalized run.json is the truth. A run directory without run.json is `running` while
this API's process for it is alive, otherwise `incomplete` (started elsewhere, e.g. the CLI,
and still going, or interrupted).

Processes are started with `spawn`, which re-imports the launching program's main module:
under uvicorn that is safe; an ad-hoc script driving the app must use `if __name__ ==
"__main__":`.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
from datetime import UTC, datetime
from multiprocessing.process import BaseProcess
from pathlib import Path

from maritime_forecast.api.errors import ApiError
from maritime_forecast.api.models import BenchmarkListItem, BenchmarkStatus
from maritime_forecast.evaluation.runner import BenchmarkConfig, run_benchmark
from maritime_forecast.storage.parquet_store import ParquetStore
from maritime_forecast.storage.run_store import RunStore, RunTamperedError


def _run_job(cfg: BenchmarkConfig, store_root: Path, runs_root: Path, run_id: str) -> None:
    """Child process entry point. run_benchmark records its own failures in the run."""
    store = ParquetStore(store_root)
    store.con.execute("SET enable_progress_bar=false")  # keep the API's log readable
    run_benchmark(cfg, store, RunStore(runs_root), run_id=run_id)


class BenchmarkJobs:
    def __init__(self, runs: RunStore, store_root: Path, max_running: int = 1):
        self.runs = runs
        self.store_root = store_root
        self.max_running = max_running
        self._procs: dict[str, BaseProcess] = {}
        # spawn, not fork: the API holds DuckDB connections and threads.
        self._mp = mp.get_context("spawn")
        self._lock = threading.Lock()  # sync endpoints run in a thread pool

    def running(self) -> list[str]:
        return sorted(r for r, p in self._procs.items() if p.is_alive())

    def start(self, cfg: BenchmarkConfig, run_id: str) -> None:
        with self._lock:
            busy = self.running()
            if len(busy) >= self.max_running:
                raise ApiError(
                    409, "benchmark_busy",
                    "a benchmark is already running; retry when it finishes", {"running": busy},
                )  # fmt: skip
            # daemon: an API shutdown stops the run, which then reports `incomplete`.
            p = self._mp.Process(
                target=_run_job, args=(cfg, self.store_root, self.runs.root, run_id), daemon=True
            )
            p.start()
            self._procs[run_id] = p

    def status(self, run_id: str) -> BenchmarkStatus:
        try:
            d = self.runs.path(run_id)
        except ValueError:
            raise ApiError(422, "invalid_parameter", "invalid run id") from None
        p = self._procs.get(run_id)
        if (d / "run.json").exists():
            try:
                run = self.runs.load(run_id)  # verifies every checksum
            except RunTamperedError as e:
                raise ApiError(409, "run_tampered", str(e), {"run_id": run_id}) from None
            return BenchmarkStatus(run_id=run_id, status=run.status, run=run)
        if p is not None and p.is_alive():
            return BenchmarkStatus(run_id=run_id, status="running")
        if d.is_dir():
            return BenchmarkStatus(
                run_id=run_id, status="incomplete",
                message="no run record: still running in another process, or interrupted",
            )  # fmt: skip
        if p is not None:  # exited before the runner created the run
            return BenchmarkStatus(
                run_id=run_id, status="failed",
                message=f"the run process exited (code {p.exitcode}) before creating the run",
            )  # fmt: skip
        raise ApiError(404, "run_not_found", f"no benchmark run {run_id!r}")

    def list(self) -> list[BenchmarkListItem]:
        """Every run directory, newest first: finalized headers plus running/incomplete ones."""
        out = []
        root = self.runs.root
        for d in root.iterdir() if root.exists() else []:
            if not d.is_dir():
                continue
            f = d / "run.json"
            if not f.exists():
                p = self._procs.get(d.name)
                alive = p is not None and p.is_alive()
                out.append(
                    BenchmarkListItem(run_id=d.name, status="running" if alive else "incomplete")
                )
                continue
            r = json.loads(f.read_text())
            sampler = r.get("sampler") or {}
            out.append(
                BenchmarkListItem(
                    run_id=r["run_id"],
                    status=r["status"],
                    created_at=r.get("created_at"),
                    dataset_id=r.get("dataset_id"),
                    protocol=(r.get("protocol") or {}).get("protocol"),
                    split=sampler.get("split"),
                    operational=r.get("operational"),
                    model_ids=[m["model_id"] for m in r.get("models", [])],
                    horizons_minutes=r.get("horizons_minutes", []),
                    n_cutoffs=sampler.get("n_cutoffs"),
                    git_sha=r.get("git_sha"),
                )
            )
        far = datetime.min.replace(tzinfo=UTC)
        return sorted(out, key=lambda x: (x.created_at or far, x.run_id), reverse=True)
