"""Leakage-invariance harness on the SYNTHETIC fixture, including a leaky control model that
the harness must catch."""

from dataclasses import dataclass
from datetime import timedelta

import pytest
from fixtures.fake_timestral import LinearBackend
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
from maritime_forecast.schemas.manifest import Period, SplitManifest
from maritime_forecast.storage.parquet_store import ParquetStore

# Retrieval runs here too (it must never change), but this fixture has few earlier segments
# near each case, so its non-vacuous check is in test_route_retrieval.py.
SYN_MANIFEST = SplitManifest(
    protocol="P-known", dataset_id="syn", description="SYNTHETIC",
    periods=[Period(split="train", start=T0, end=T0 + timedelta(days=1))],
)  # fmt: skip
PARAMS = {
    "damped_velocity_v1": {"taus_hours": {"default": 2.0, "cargo": 0.5}},
    "route_retrieval_v1": {"manifest": SYN_MANIFEST, "config": {"pool": "rolling"}},
    "route_retrieval_v2": {"manifest": SYN_MANIFEST, "config": {"pool": "rolling"}},
    # A stand-in for the optional checkpoint: the adapter's context building is what can leak.
    "timestral_pos_v1": {"backend": LinearBackend()},
    "timestral_vel_v1": {"backend": LinearBackend()},
}
FEW_REFERENCES = {"route_retrieval_v1", "route_retrieval_v2"}
# T1 needs 6 observed grid steps (1 h at 10 min). Five of the seven cases have under 1 h of
# fixture history, so those grids are correctly `insufficient_context`; the 1-min grid
# forecasts every case. Measured for T1-pos: 54 of 126 comparisons are against an ok forecast.
# T1-vel also needs a position at as_of (stale_anchor otherwise), so it forecasts fewer.
SHORT_CONTEXT_OK = {"timestral_pos_v1": 0.4, "timestral_vel_v1": 0.3}

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
    rep = check_invariance(registry_factories(PARAMS), store, "syn", CASES, history_hours=3,
                           workdir=tmp_path)  # fmt: skip
    assert rep.cases == len(CASES)
    assert rep.ok, rep.violations
    # Non-vacuous: every case has future reports in scope, so every perturbation bit,
    # and most comparisons were against a real (status ok) forecast.
    assert rep.vacuous == []
    n = len(CASES) * len(registry_factories(PARAMS)) * 3  # 3 grids
    assert rep.checks == n * (len(PERTURBATIONS) + 1)
    for model_id in set(registry_factories(PARAMS)) - FEW_REFERENCES:  # must really forecast
        need = SHORT_CONTEXT_OK.get(model_id, 0.8)
        assert rep.ok_fraction(model_id) >= need, (model_id, rep.ok_fraction(model_id))


@dataclass
class Peeking(BaseForecaster):
    """CONTROL ONLY: reads the store past as_of (the leak the harness must catch)."""

    model_id: str = "peeking_control"
    store: ParquetStore | None = None
    dataset_id: str = ""

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(model_id=self.model_id, model_version="0", description="leaks",
                                 reads_beyond_own_history=True, uses_store_at_predict=True)  # fmt: skip

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
        check_invariance(registry_factories(PARAMS), store, "syn", CASES[:1], history_hours=3,
                         grids=[(2, 10)], perturbations=[bad], workdir=tmp_path)  # fmt: skip


@dataclass
class OpensRealArchive(BaseForecaster):
    """CONTROL ONLY: ignores the store it is handed and opens the archive by path."""

    model_id: str = "archive_control"
    path: str = ""

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(model_id=self.model_id, model_version="0", description="bypass",
                                 reads_beyond_own_history=True, uses_store_at_predict=True)  # fmt: skip

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
                                 reads_beyond_own_history=True, uses_store_at_predict=True)  # fmt: skip

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
    rep = check_invariance(registry_factories(PARAMS), store, "syn", [(MOVING, late)], history_hours=12,
                           grids=[(2, 10)], workdir=tmp_path)  # fmt: skip
    vac = {p for _, _, p in rep.vacuous}
    assert {"delete_future", "shift_future", "flood_future"} <= vac
    assert not any(p.startswith("spike") for p in vac)  # spikes always add a future row


def test_harness_refuses_models_that_cannot_forecast(store, tmp_path):
    with pytest.raises(ValueError, match="not installed"):
        check_invariance(
            registry_factories(),
            store,
            "syn",
            CASES[:1],
            history_hours=3,
            grids=[(2, 10)],
            workdir=tmp_path,
        )  # damped has no params here


@pytest.mark.timestral
def test_real_timestral_checkpoint_is_invariant(store, tmp_path):
    """Needs `uv sync --extra timestral` and `make timestral-weights` (run with -m timestral)."""
    import maritime_forecast.models.timestral_adapter as ta

    if not (ta.package_available() and ta.weights_present()):
        pytest.skip("timestral extra or weights not installed")
    ids = ("timestral_pos_v1", "timestral_vel_v1")
    rep = check_invariance({m: registry_factories()[m] for m in ids},
                           store, "syn", CASES, history_hours=3, workdir=tmp_path)  # fmt: skip
    assert rep.ok, rep.violations
    assert rep.vacuous == []
    for m in ids:
        assert rep.ok_fraction(m) >= SHORT_CONTEXT_OK[m], (m, rep.ok_fraction(m))
