"""Leakage-invariance harness on the SYNTHETIC fixture, including a leaky control model that
the harness must catch."""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from fixtures.synthetic import ANTIMERIDIAN, CSV_PATH, DIRTY, GAPS, MOVING, STOPPING, T0, TURNING

import maritime_forecast.data.ingest as ingest_mod
from maritime_forecast.data.ingest import ingest
from maritime_forecast.data.mapping import load_mapping
from maritime_forecast.evaluation.invariance import (
    PERTURBATIONS,
    check_invariance,
    registry_factories,
)
from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
)
from maritime_forecast.storage.parquet_store import ParquetStore

CASES = [
    (MOVING, T0 + timedelta(minutes=60)),
    (TURNING, T0 + timedelta(minutes=45, seconds=30)),  # between reports
    (STOPPING, T0 + timedelta(minutes=95)),  # moored
    (GAPS, T0 + timedelta(minutes=19)),  # right before the 45-min gap
    (GAPS, T0 + timedelta(minutes=70)),  # inside the 3 h gap: stale history
    (ANTIMERIDIAN, T0 + timedelta(minutes=12)),  # just before the dateline
    (DIRTY, T0 + timedelta(minutes=11)),  # just before the injected spike
]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("inv")
    mp = pytest.MonkeyPatch()
    mp.setattr(ingest_mod, "MANIFEST_DIR", tmp / "m")
    s = ParquetStore(tmp / "canonical")
    ingest([CSV_PATH], load_mapping("noaa_marinecadastre"), "syn", store=s)
    yield s
    mp.undo()


def test_all_registered_models_are_invariant(store, tmp_path):
    rep = check_invariance(registry_factories(), store, "syn", CASES, history_hours=3,
                           workdir=tmp_path)  # fmt: skip
    assert rep.cases == len(CASES)
    assert rep.ok, rep.violations
    # Non-vacuous: every case has future reports in scope, so every perturbation bit,
    # and most comparisons were against a real (status ok) forecast.
    assert rep.vacuous == []
    n = len(CASES) * len(registry_factories()) * 3  # 3 grids
    assert rep.checks == n * (len(PERTURBATIONS) + 1)
    assert rep.ok_comparisons >= 0.8 * n * len(PERTURBATIONS)


@dataclass
class Peeking(BaseForecaster):
    """CONTROL ONLY: reads the store past as_of (the leak the harness must catch)."""

    model_id: str = "peeking_control"
    store: ParquetStore | None = None
    dataset_id: str = ""

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(model_id=self.model_id, model_version="0", description="leaks",
                                 reads_beyond_own_history=True)  # fmt: skip

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        future = self.store.track(self.dataset_id, history.vessel_id, history.as_of,
                                  history.as_of + timedelta(hours=1))  # fmt: skip
        r = future.row(-1, named=True) if future.height else history.last()
        n = len(horizon.target_times)
        return [r["latitude_deg"]] * n, [r["longitude_deg"]] * n, []


def test_harness_catches_a_peeking_model(store, tmp_path):
    rep = check_invariance(
        {"peeking_control": lambda s, ds: Peeking(store=s, dataset_id=ds)},
        store, "syn", [(MOVING, T0 + timedelta(minutes=60))], history_hours=3, grids=[(2, 10)],
        workdir=tmp_path,
    )  # fmt: skip
    assert not rep.ok
    assert {v.perturbation for v in rep.violations} >= {"delete_future", "shift_future"}


def test_harness_self_check_rejects_a_perturbation_that_touches_the_past(store, tmp_path):
    from maritime_forecast.evaluation.invariance import Perturbation

    bad = Perturbation(
        "edits_past",
        lambda src, t, v: f"SELECT * REPLACE (latitude_deg + 1 AS latitude_deg) FROM {src}",
    )
    with pytest.raises(AssertionError, match="before as_of"):
        check_invariance(registry_factories(), store, "syn", CASES[:1], history_hours=3,
                         grids=[(2, 10)], perturbations=[bad], workdir=tmp_path)  # fmt: skip


@dataclass
class OpensRealArchive(BaseForecaster):
    """CONTROL ONLY: ignores the store it is handed and opens the archive by path."""

    model_id: str = "archive_control"
    path: str = ""

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(model_id=self.model_id, model_version="0", description="bypass",
                                 reads_beyond_own_history=True)  # fmt: skip

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        ParquetStore(self.path)  # must be refused while the harness runs
        return [0.0] * len(horizon.target_times), [0.0] * len(horizon.target_times), []


def test_harness_blocks_models_that_bypass_the_scoped_store(store, tmp_path):
    rep = check_invariance(
        {"archive_control": lambda s, ds: OpensRealArchive(path=str(store.root))},
        store, "syn", [(MOVING, T0 + timedelta(minutes=60))], history_hours=3, grids=[(2, 10)],
        workdir=tmp_path,
    )  # fmt: skip
    # Every run fails with a typed exception result -> nothing to compare would be "ok".
    assert rep.ok_comparisons == 0


@dataclass
class CachedByKey(BaseForecaster):
    """CONTROL ONLY: peeks, but memoizes on (vessel, as_of) ignoring the data version."""

    model_id: str = "cached_control"
    store: ParquetStore | None = None
    dataset_id: str = ""

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(model_id=self.model_id, model_version="0", description="cache",
                                 reads_beyond_own_history=True)  # fmt: skip

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        key = (history.vessel_id, history.as_of, len(horizon.target_times))
        if key not in _CACHE:
            future = self.store.track(self.dataset_id, history.vessel_id, history.as_of,
                                      history.as_of + timedelta(hours=1))  # fmt: skip
            r = future.row(-1, named=True) if future.height else history.last()
            _CACHE[key] = ([r["latitude_deg"]] * len(horizon.target_times),
                           [r["longitude_deg"]] * len(horizon.target_times))  # fmt: skip
        la, lo = _CACHE[key]
        return la, lo, []


_CACHE: dict = {}


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN LIMITATION: a process-global cache keyed without the "
    "data version is poisoned by whichever run comes first, so every run agrees. "
    "Only per-run process isolation could catch it; cache keys must include the "
    "data version (new-forecaster checklist).",
)
def test_harness_cannot_catch_a_process_global_cache_keyed_without_data_version(store, tmp_path):
    _CACHE.clear()
    rep = check_invariance(
        {"cached_control": lambda s, ds: CachedByKey(store=s, dataset_id=ds)},
        store, "syn", [(MOVING, T0 + timedelta(minutes=60))], history_hours=3, grids=[(2, 10)],
        workdir=tmp_path,
    )  # fmt: skip
    assert not rep.ok


def test_vacuous_cases_are_reported_not_passed(store, tmp_path):
    late = T0 + timedelta(hours=10)  # MOVING stopped reporting at 02:00: no future in scope
    rep = check_invariance(registry_factories(), store, "syn", [(MOVING, late)], history_hours=12,
                           grids=[(2, 10)], workdir=tmp_path)  # fmt: skip
    vac = {p for _, _, p in rep.vacuous}
    assert {"delete_future", "shift_future", "flood_future"} <= vac
    assert not any(p.startswith("spike") for p in vac)  # spikes always add a future row
