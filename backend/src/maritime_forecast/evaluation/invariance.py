"""Leakage-invariance harness (build plan §10 "Temporal leakage", Milestone 2 done-when).

For each cutoff (vessel, as_of), every report after as_of, for every vessel in the scoped data,
is deleted or altered. A model whose forecast changes has seen the future.

What the harness enforces:
  * Models are built by a factory(store, dataset_id) that receives the scoped (perturbed)
    store. While models run, opening the real archive (or the store under test) is blocked,
    so a model cannot sidestep the perturbation by reading the data another way.
  * Every run uses a fresh model instance (perturbed runs first), so instance-level caches
    cannot carry answers between data versions. A PROCESS-GLOBAL cache keyed without the data
    version would still hide a leak (whichever run fills it first wins); that needs process
    isolation, so instead cache keys must include the data version (new-forecaster checklist;
    xfail test documents it).
  * Self-checks: a perturbation may not touch rows at or before as_of, and every perturbation
    must actually change the target's future. Cases where it can't (no future reports in
    scope) are reported as `vacuous`, never counted as passes.
  * Boundary spikes at +1 µs, +1 ms and +1 s after as_of; several horizon/step grids.
  * Models that read beyond their own history get the whole dataset as scope (lookback and
    look-ahead), so an index built from the scoped store is realistic.

What it cannot see: protocol leakage (P-unseen exclusion, index period, parameters fitted on
test). Those need dedicated tests (build plan §10 "Protocol leakage").
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from maritime_forecast.models.base import ForecastHorizon, TrajectoryForecaster, history_from_store
from maritime_forecast.storage import parquet_store
from maritime_forecast.storage.parquet_store import CANONICAL_ROOT, ParquetStore

Factory = Callable[[ParquetStore, str], TrajectoryForecaster]
DEFAULT_GRIDS: list[tuple[float, int]] = [(1, 1), (6, 10), (24, 60)]  # (horizon h, step min)


def _lit(t: datetime) -> str:
    return f"TIMESTAMPTZ '{t.isoformat()}'"


@dataclass(frozen=True)
class Perturbation:
    name: str
    sql: Callable[[str, str, str], str]  # (source relation, as_of literal, vessel_id) -> SQL


def _spike(offset: str) -> Callable[[str, str, str], str]:
    # A wild report just after the CUTOFF (not after the last report: when as_of falls
    # between reports that would land in the visible past).
    return lambda src, t, v: (
        f"SELECT * FROM {src} UNION ALL "
        f"SELECT * REPLACE ({t} + INTERVAL {offset} AS observed_at, "
        "latitude_deg + 1.0 AS latitude_deg, 60.0 AS sog_knots, 123.0 AS cog_deg) "
        f"FROM (SELECT * FROM {src} WHERE vessel_id = '{v}' AND observed_at <= {t} "
        "ORDER BY observed_at DESC LIMIT 1)"
    )


PERTURBATIONS: list[Perturbation] = [
    Perturbation(
        "delete_future", lambda src, t, v: f"SELECT * FROM {src} WHERE observed_at <= {t}"
    ),
    Perturbation(
        "shift_future",
        lambda src, t, v: (
            "SELECT * REPLACE ("
            f"CASE WHEN observed_at > {t} THEN latitude_deg * 0.9 + 3.0 "
            "ELSE latitude_deg END AS latitude_deg, "
            f"CASE WHEN observed_at > {t} THEN ((longitude_deg + 187.0) % 360) - 180 "
            "ELSE longitude_deg END AS longitude_deg, "
            f"CASE WHEN observed_at > {t} THEN 37.0 ELSE sog_knots END AS sog_knots, "
            f"CASE WHEN observed_at > {t} THEN 271.0 ELSE cog_deg END AS cog_deg"
            f") FROM {src}"
        ),
    ),
    Perturbation("spike_1us_after_cutoff", _spike("1 MICROSECOND")),
    Perturbation("spike_1ms_after_cutoff", _spike("1 MILLISECOND")),
    Perturbation("spike_1s_after_cutoff", _spike("1 SECOND")),
    Perturbation(
        "flood_future",
        lambda src, t, v: (
            f"SELECT * FROM {src} UNION ALL "
            "SELECT * REPLACE (observed_at + INTERVAL 7 SECOND AS observed_at, "
            "latitude_deg - 0.2 AS latitude_deg, 0.0 AS sog_knots) "
            f"FROM {src} WHERE observed_at > {t}"
        ),
    ),
]


@dataclass
class Violation:
    model_id: str
    vessel_id: str
    as_of: datetime
    grid: tuple[float, int]
    perturbation: str
    detail: str


@dataclass
class InvarianceReport:
    cases: int = 0
    checks: int = 0
    ok_comparisons: int = 0  # perturbed comparisons whose reference forecast had status ok
    violations: list[Violation] = field(default_factory=list)
    vacuous: list[tuple[str, datetime, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _comparable(forecast) -> dict:
    return forecast.model_dump(mode="json", exclude={"runtime_ms"})


def _rows(root: Path, where: str) -> str:
    return (
        "SELECT * EXCLUDE (quality_flags), CAST(quality_flags AS VARCHAR) AS qf "
        f"FROM read_parquet('{root}/*/*.parquet', hive_partitioning=false) WHERE {where}"
    )


def _sym_diff(con, a: str, b: str) -> int:
    (n1,), (n2,) = con.execute(
        f"SELECT count(*) FROM (({a}) EXCEPT ALL ({b})) UNION ALL "
        f"SELECT count(*) FROM (({b}) EXCEPT ALL ({a}))"
    ).fetchall()
    return n1 + n2


@contextmanager
def _blocked(*roots: Path) -> Iterator[None]:
    added = {Path(r).resolve() for r in roots} - parquet_store.BLOCKED_ROOTS
    parquet_store.BLOCKED_ROOTS.update(added)
    try:
        yield
    finally:
        parquet_store.BLOCKED_ROOTS.difference_update(added)


def _scope_files(store, dataset_id, as_of, history_hours, grids, wide) -> list[Path]:
    if wide:  # index-building models see the whole dataset, as in a benchmark
        return store.files(dataset_id)
    lo = as_of - timedelta(hours=history_hours)
    hi = as_of + timedelta(hours=max(h for h, _ in grids), days=1)
    return store.files(dataset_id, lo, hi)


def check_invariance(
    factories: dict[str, Factory],
    store: ParquetStore,
    dataset_id: str,
    cases: list[tuple[str, datetime]],
    *,
    history_hours: float = 24,
    grids: list[tuple[float, int]] = DEFAULT_GRIDS,
    perturbations: list[Perturbation] = PERTURBATIONS,
    workdir: Path | None = None,
) -> InvarianceReport:
    """Run every model on every case and grid: perturbed runs first, then the reference."""
    report = InvarianceReport()
    wide = any(
        f(store, dataset_id).capabilities().reads_beyond_own_history for f in factories.values()
    )
    tmp = Path(tempfile.mkdtemp(prefix="invariance_", dir=workdir))
    try:
        for i, (vessel_id, as_of) in enumerate(cases):
            report.cases += 1
            files = _scope_files(store, dataset_id, as_of, history_hours, grids, wide)
            if not files:
                raise LookupError(f"no partitions around {as_of} in {dataset_id}")
            src = (
                "read_parquet(["
                + ", ".join(f"'{f}'" for f in files)
                + "], hive_partitioning=false)"
            )
            scoped = ParquetStore(tmp / f"case{i}", con=store.con)
            scoped.write("reference", f"SELECT * FROM {src}")
            ref_root = scoped.root / "reference"
            past = f"observed_at <= {_lit(as_of)}"
            target_future = f"vessel_id = '{vessel_id}' AND observed_at > {_lit(as_of)}"
            active = []
            for p in perturbations:
                scoped.write(p.name, p.sql(src, _lit(as_of), vessel_id))
                p_root = scoped.root / p.name
                if _sym_diff(store.con, _rows(ref_root, past), _rows(p_root, past)):
                    raise AssertionError(f"perturbation {p.name!r} altered rows at or before as_of")
                if not _sym_diff(
                    store.con, _rows(ref_root, target_future), _rows(p_root, target_future)
                ):
                    report.vacuous.append((vessel_id, as_of, p.name))
                    continue
                active.append(p)

            with _blocked(CANONICAL_ROOT, store.root):
                for horizon_h, step in grids:
                    horizon = ForecastHorizon(as_of, horizon_h, step)
                    for model_id, factory in factories.items():
                        perturbed = {}
                        for p in active:  # perturbed first, fresh instance each
                            hp = history_from_store(scoped, p.name, vessel_id, as_of, history_hours)
                            out = factory(scoped, p.name).predict(hp, horizon)
                            perturbed[p.name] = _comparable(out)
                        h = history_from_store(scoped, "reference", vessel_id, as_of, history_hours)
                        model = factory(scoped, "reference")
                        ref = _comparable(model.predict(h, horizon))
                        repeats = (
                            _comparable(model.predict(h, horizon)),
                            _comparable(factory(scoped, "reference").predict(h, horizon)),
                        )
                        report.checks += 1
                        if any(r != ref for r in repeats):
                            report.violations.append(
                                Violation(
                                    model_id,
                                    vessel_id,
                                    as_of,
                                    (horizon_h, step),
                                    "determinism",
                                    "repeat call or fresh instance differs",
                                )
                            )
                        for name, got in perturbed.items():
                            report.checks += 1
                            report.ok_comparisons += ref["status"] == "ok"
                            if got != ref:
                                diff = sorted(k for k in ref if ref[k] != got.get(k))
                                report.violations.append(
                                    Violation(
                                        model_id,
                                        vessel_id,
                                        as_of,
                                        (horizon_h, step),
                                        name,
                                        f"fields differ: {diff}",
                                    )
                                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def registry_factories() -> dict[str, Factory]:
    """Factories for every registered model, each built from the store it is handed."""
    from maritime_forecast.models.registry import get_model, model_ids

    return {m: (lambda store, ds, m=m: get_model(m, store, ds)) for m in model_ids()}
