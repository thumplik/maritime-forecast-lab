"""T1-pos: Timestral zero-shot forecaster on local east/north positions (build plan §5 T1).

Timestral (mistralai, released 2026-10-15) is a general-purpose, patch-based time-series
model pretrained mostly on synthetic series. It knows nothing about ships, coastlines or
lanes. T1-pos hands it the target vessel's own track as a two-variate series and integrates
nothing: its median forecast IS the predicted position.

1. **Frame.** The project's local azimuthal-equidistant frame centred on the last report
   (geo.geodesic.LocalFrame): x east, y north, metres. Positions are recovered with its exact
   inverse, so the antimeridian is the projection's problem, not longitude arithmetic's.
2. **Context.** A regular grid at the request's step, ending at as_of and spanning the
   request's history. A grid time takes a position only if it coincides with a report or lies
   between two of the target's own reports at most `gap_minutes` apart (linear in the frame).
   Elsewhere it is missing (NaN; Timestral masks missing values). Every report used is at or
   before as_of, so the context is causal by construction, and gaps are never bridged.
3. **Forecast.** `forecast(context (1, 2, T), prediction_length=n)`, median of the 21
   quantiles. `clip_nonnegative` is OFF: the package default clamps a series at 0 when its
   whole context is non-negative, which in this frame is every track heading south-west
   (its past lies north-east of the anchor), pinning the forecast to the anchor.
   `symmetric_averaging` stays on (the forecast of -context is mirrored and averaged), which
   suits positions, whose sign is an accident of the frame.
4. **Uncertainty.** None is served. Timestral's quantiles are per-axis marginals and are never
   combined into a 2-D region (AGENTS.md); calibrated radii come from conformal (U1).

The package and weights are optional (`uv sync --extra timestral`, then `make
timestral-weights`), pinned by commit and revision, and loaded lazily from a local cache only:
predicting never downloads. Without them the model is `installed=False` and every request is a
typed `not_installed`. Tests inject a stand-in backend (`ModelContext.params["backend"]`).
"""

from __future__ import annotations

import importlib.util
import threading
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from maritime_forecast.config import DATA_DIR
from maritime_forecast.geo.geodesic import LocalFrame
from maritime_forecast.models.base import (
    AISHistory,
    BaseForecaster,
    ForecastHorizon,
    ModelCapabilities,
    ModelContext,
)

REPO_ID = "mistralai/Timestral-28M"
REVISION = "49b26409ac87b5935539a5d8ffe7f397b720db47"  # Hugging Face weights revision
PACKAGE_COMMIT = "a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff"  # github.com/mistralai/timestral
WEIGHTS_DIR = DATA_DIR / "weights" / "huggingface"
STALE_WARNING_MINUTES = 30
# Distances and bearings from the frame centre are exact; between other points the frame's
# error grows with distance (docs/model-integration-notes.md). Beyond this, say so.
FRAME_WARNING_M = 300_000.0

_BACKENDS: dict[tuple[str, str, str], object] = {}
_LOCK = threading.Lock()


def installed_commit() -> str | None:
    """The git commit the installed `timestral` package was built from (None if absent)."""
    if importlib.util.find_spec("timestral") is None:
        return None
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        raw = distribution("timestral").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    return (json.loads(raw).get("vcs_info") or {}).get("commit_id") if raw else None


def package_available() -> bool:
    """The pinned package is installed (a different commit is a different model)."""
    return installed_commit() == PACKAGE_COMMIT


def weights_present(repo_id: str = REPO_ID, revision: str = REVISION) -> bool:
    if importlib.util.find_spec("huggingface_hub") is None:
        return False
    from huggingface_hub import try_to_load_from_cache

    path = try_to_load_from_cache(repo_id, "model.safetensors", cache_dir=WEIGHTS_DIR,
                                  revision=revision)  # fmt: skip
    return isinstance(path, str)


def load_backend(repo_id: str = REPO_ID, revision: str = REVISION):
    """The pinned checkpoint from the local cache (never the network), once per process."""
    key = (repo_id, revision, str(WEIGHTS_DIR))
    with _LOCK:
        if key not in _BACKENDS:
            from timestral import TimestralForecaster

            _BACKENDS[key] = TimestralForecaster.from_pretrained(
                repo_id, revision=revision, cache_dir=WEIGHTS_DIR, local_files_only=True,
                device="cpu",
            )  # fmt: skip
        return _BACKENDS[key]


def context_xy(history: AISHistory, frame: LocalFrame, step_minutes: int,
               gap_minutes: float) -> np.ndarray:  # fmt: skip
    """(2, T) east/north metres on the grid as_of - k*step (oldest first, k = T-1..0) within
    the history window; NaN where no report or no gap-free bracket of reports exists."""
    r = history.reports
    t = np.array([x.timestamp() for x in r["observed_at"].to_list()], dtype=float)
    x, y = frame.to_xy(r["latitude_deg"].to_numpy(), r["longitude_deg"].to_numpy())
    as_of = history.as_of.timestamp()
    step = step_minutes * 60.0
    n = int(history.history_hours * 3600 // step) + 1
    grid = as_of - step * np.arange(n - 1, -1, -1)
    out = np.full((2, n), np.nan)
    if not t.size:
        return out
    hi = np.searchsorted(t, grid, side="left")  # first report at or after the grid time
    for j, g in enumerate(grid):
        b = hi[j]
        if b < t.size and t[b] == g:  # a report exactly on the grid
            out[:, j] = x[b], y[b]
        elif 0 < b < t.size and t[b] - t[b - 1] <= gap_minutes * 60:
            w = (g - t[b - 1]) / (t[b] - t[b - 1])
            out[:, j] = x[b - 1] + w * (x[b] - x[b - 1]), y[b - 1] + w * (y[b] - y[b - 1])
    return out


@dataclass
class TimestralPos(BaseForecaster):
    """T1-pos; TimestralVel below overrides only the series and how positions come back."""

    model_id: str = "timestral_pos_v1"
    backend: object | None = None  # injected stand-in (tests); else the pinned checkpoint
    config: dict = field(
        default_factory=lambda: {
            "representation": "T1-pos",
            "repo_id": REPO_ID,
            "revision": REVISION,
            "package_commit": PACKAGE_COMMIT,
            "frame": "local AEQD at the last report (x east, y north, m)",
            "gap_minutes": 30.0,
            "min_observed_steps": 6,
            "symmetric_averaging": True,
            "clip_nonnegative": False,
            "point": "median",
            # The API caps a request at 1,024 target steps = one decode block: never rolled out.
            "rollout": "single pass (<= 1024 steps)",
            "device": "cpu",
            # Travels with every run record: the weights are fitted on data outside this
            # project's splits, so P-unseen is vessel-disjoint only with respect to our splits.
            "pretraining": "external; corpus undisclosed (mostly synthetic per the model "
            "card); public AIS, including NOAA 2023, cannot be ruled out",
        }
    )

    @classmethod
    def from_context(cls, ctx: ModelContext) -> TimestralPos:
        backend = (ctx.params or {}).get("backend")
        return cls(backend=backend)

    def _installed(self) -> bool:
        return self.backend is not None or (package_available() and weights_present())

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            model_id=self.model_id,
            model_version=f"1.0.0+{REVISION[:7]}",
            description="Timestral-28M zero-shot on the vessel's own east/north positions "
            "(T1-pos); median of the per-axis quantiles. Knows no coastlines or lanes.",
            min_history_reports=2,
            # Fitted parameters from an external, undisclosed corpus (the contract's definition).
            reads_beyond_own_history=True,
            uses_store_at_predict=False,
            installed=self._installed(),
            optional=True,
        )

    def _backend(self):
        return self.backend if self.backend is not None else load_backend()

    def _predict(self, history: AISHistory, horizon: ForecastHorizon):
        if not self._installed():
            got = installed_commit()
            if package_available():
                missing = "its weights"
            elif got is None:
                missing = "the timestral package"
            else:
                missing = f"timestral@{PACKAGE_COMMIT[:7]} (found {got[:7]})"
            return self.unsupported(
                horizon, "not_installed",
                f"{missing} not installed: `uv sync --extra timestral` (backend/), then "
                f"`make timestral-weights` ({REPO_ID}@{REVISION[:7]})",
            )  # fmt: skip
        cfg = self.config
        last = history.last()
        frame = LocalFrame(last["latitude_deg"], last["longitude_deg"])
        ctx = context_xy(history, frame, horizon.step_minutes, cfg["gap_minutes"])
        series = self._series(ctx, horizon.step_minutes)
        observed = int(np.isfinite(series[0]).sum())
        if observed < cfg["min_observed_steps"]:
            return self.unsupported(
                horizon, "insufficient_context",
                f"{observed} observed {horizon.step_minutes}-min steps of {self._unit} in the "
                f"history window; needs {cfg['min_observed_steps']}",
            )  # fmt: skip
        refusal = self._refuse(ctx, horizon)
        if refusal is not None:
            return refusal
        n = len(horizon.target_times)
        fc = self._backend().forecast(
            series[None, :, :], prediction_length=n,
            symmetric_averaging=cfg["symmetric_averaging"],
            clip_nonnegative=cfg["clip_nonnegative"],
        )  # fmt: skip
        xy = self._positions(np.asarray(fc.median, dtype=float)[0], ctx, horizon.step_minutes)
        lat, lon = frame.to_latlon(xy[0], xy[1])
        warnings = []
        age = history.as_of - last["observed_at"]
        if age > timedelta(minutes=STALE_WARNING_MINUTES):
            warnings.append(f"last report is {age.total_seconds() / 60:.0f} min older than as_of")
        reach = float(np.nanmax(np.hypot(xy[0], xy[1]))) if n else 0.0
        if reach > FRAME_WARNING_M:
            warnings.append(
                f"forecast reaches {reach / 1000:.0f} km from the frame centre "
                f"(> {FRAME_WARNING_M / 1000:.0f} km: local-frame geometry degrades)"
            )
        return lat.tolist(), lon.tolist(), warnings

    # T1-pos: the series is the position itself.
    _unit = "position"

    def _series(self, ctx: np.ndarray, step_minutes: int) -> np.ndarray:
        return ctx

    def _refuse(self, ctx: np.ndarray, horizon: ForecastHorizon):
        return None

    def _positions(self, median: np.ndarray, ctx: np.ndarray, step_minutes: int) -> np.ndarray:
        return median


def velocity_series(ctx: np.ndarray, step_minutes: int) -> np.ndarray:
    """(2, T-1) east/north velocity, m/s, between consecutive context grid points; NaN where
    either point is missing (so a gap never yields a velocity)."""
    return np.diff(ctx, axis=1) / (step_minutes * 60.0)


@dataclass
class TimestralVel(TimestralPos):
    """T1-vel (§5 T1): forecast the east/north velocity, then integrate it from the position at
    as_of in the local frame (distances and bearings from the frame centre are exact; the path
    is a sum of straight steps in the plane, mapped back with the exact inverse)."""

    model_id: str = "timestral_vel_v1"

    def __post_init__(self) -> None:
        self.config = {
            **self.config,
            "representation": "T1-vel",
            "series": "east/north velocity (m/s) between consecutive grid points",
            "integration": "cumulative sum from the position at as_of, in the local frame",
        }

    def capabilities(self) -> ModelCapabilities:
        return super().capabilities().model_copy(update={
            "description": "Timestral-28M zero-shot on the vessel's own east/north velocity "
            "(T1-vel), integrated from the position at as_of; median of the per-axis "
            "quantiles. Knows no coastlines or lanes.",
        })  # fmt: skip

    _unit = "velocity"

    def _series(self, ctx: np.ndarray, step_minutes: int) -> np.ndarray:
        return velocity_series(ctx, step_minutes)

    def _refuse(self, ctx: np.ndarray, horizon: ForecastHorizon):
        if not np.isfinite(ctx[:, -1]).all():
            return self.unsupported(
                horizon, "stale_anchor",
                "no report at or bracketing as_of: there is no position to integrate the "
                "forecast velocity from",
            )  # fmt: skip
        return None

    def _positions(self, median: np.ndarray, ctx: np.ndarray, step_minutes: int) -> np.ndarray:
        return ctx[:, -1:] + np.cumsum(median * step_minutes * 60.0, axis=1)
