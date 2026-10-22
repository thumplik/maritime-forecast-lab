"""FastAPI application (build plan §8): data, forecasts, forecast evaluation and benchmarks.

Run:  make api      (uvicorn maritime_forecast.api.app:app --reload --port 8000)
Docs: http://localhost:8000/docs
"""

# No `from __future__ import annotations`: FastAPI must resolve the Annotated limits that
# reference `settings` inside create_app at definition time.
import hashlib
import json
import re
import secrets
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime

from maritime_forecast.api import errors
from maritime_forecast.api.errors import ApiError, dataset_not_found, invalid
from maritime_forecast.api.jobs import BenchmarkJobs
from maritime_forecast.api.models import (
    Admission,
    BenchmarkList,
    BenchmarkRequest,
    BenchmarkSample,
    BenchmarkSamples,
    BenchmarkStatus,
    BenchmarkSummary,
    DatasetList,
    DatasetSummary,
    Gap,
    Health,
    IngestTotals,
    ModelInfo,
    ModelList,
    Segment,
    SummaryCell,
    SummaryModelCell,
    Track,
    TrackPoints,
    VesselPage,
    VesselSummary,
)
from maritime_forecast.config import DATA_DIR, MANIFEST_DIR, RUNS_DIR, BBox, default_region
from maritime_forecast.data.segment import (
    DEFAULT_SEGMENT_CONFIG,
    gap_segments,
    segments_and_gaps,
)
from maritime_forecast.evaluation import conformal
from maritime_forecast.evaluation.forecast_eval import evaluate_forecast
from maritime_forecast.evaluation.holdout import config_sha, is_holdout, touches_test
from maritime_forecast.evaluation.runner import BenchmarkConfig, benchmark_context, preflight
from maritime_forecast.evaluation.splits import HOLDOUT_MANIFEST_VERSION
from maritime_forecast.geo.navigable_mask import MaskMismatchError, load_navigable_mask
from maritime_forecast.models.base import ForecastHorizon, ModelContext, history_from_store
from maritime_forecast.models.registry import get_model, model_ids
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.schemas.forecast import (
    MAX_TARGET_STEPS,
    ConformalRegion,
    ForecastEvaluation,
    ForecastRequest,
    ForecastResponse,
    ModelForecast,
)
from maritime_forecast.schemas.manifest import SplitManifest
from maritime_forecast.storage.parquet_store import CANONICAL_ROOT, ParquetStore, connect
from maritime_forecast.storage.run_store import RunStore

DATASET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
VESSEL_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# records.parquet columns the failure gallery needs (runs before 2026-10-21 03:46 lack them).
GALLERY_COLUMNS = frozenset({"pred_lat", "pred_lon", "truth_lat", "truth_lon",
                             "feas_land_point", "feas_land_crossing"})  # fmt: skip


@dataclass
class ApiSettings:
    store_root: Path = CANONICAL_ROOT
    manifest_dir: Path = MANIFEST_DIR
    runs_dir: Path = RUNS_DIR
    max_window_days: int = 31
    max_track_points: int = 200_000
    max_vessel_page: int = 1000
    gap_minutes: float = DEFAULT_SEGMENT_CONFIG.gap_minutes
    # Fitted parameters for the API come from this protocol's TRAIN split (deployment fit).
    deployment_protocol: str = "P-known"
    # Split-conformal radii per (protocol, dataset), fitted on the calibration split.
    conformal_dir: Path = DATA_DIR / "model_params" / "conformal"
    forecast_cache_size: int = 256
    # Truth for POST /forecasts/evaluation: the benchmark's bracketing rule (§6.1 step 4).
    max_bracket_gap_minutes: float = 30.0
    max_running_benchmarks: int = 1
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )


class Context:
    """Per-app state. DuckDB connections are not shared across threads: each request gets
    its own cursor on the base connection."""

    def __init__(self, settings: ApiSettings):
        self.settings = settings
        self.base = connect()
        self.base.execute("SET enable_progress_bar=false")
        self.runs = RunStore(settings.runs_dir / "benchmarks")
        self.jobs = BenchmarkJobs(self.runs, settings.store_root, settings.max_running_benchmarks)

    @cached_property
    def git_sha(self) -> str:
        return git_sha()

    def store(self) -> Iterator[ParquetStore]:
        cur = self.base.cursor()
        try:
            yield ParquetStore(self.settings.store_root, con=cur)
        finally:
            cur.close()


def _ctx(request: Request) -> Context:
    return request.app.state.ctx


def _store(ctx: Annotated[Context, Depends(_ctx)]) -> Iterator[ParquetStore]:
    yield from ctx.store()


StoreDep = Annotated[ParquetStore, Depends(_store)]
CtxDep = Annotated[Context, Depends(_ctx)]
DatasetQ = Annotated[str, Query(pattern=DATASET_ID_PATTERN, description="Dataset id")]
FromQ = Annotated[
    AwareDatetime | None, Query(alias="from", description="Inclusive, UTC offset required")
]  # noqa: E501
ToQ = Annotated[
    AwareDatetime | None, Query(alias="to", description="Exclusive, UTC offset required")
]


# -- helpers -----------------------------------------------------------------------------
def _partitions(store: ParquetStore, dataset_id: str) -> list[date]:
    try:
        parts = store.partitions(dataset_id)
    except KeyError:
        raise dataset_not_found(dataset_id) from None
    if not parts:
        raise dataset_not_found(dataset_id)
    return parts


def _window(
    store: ParquetStore, dataset_id: str, start: datetime | None, end: datetime | None,
    settings: ApiSettings,
) -> tuple[datetime, datetime]:  # fmt: skip
    """Resolve [from, to) in UTC, defaulting to the dataset's span, and enforce the limit."""
    parts = _partitions(store, dataset_id)
    lo = datetime.combine(parts[0], datetime.min.time(), UTC)
    hi = datetime.combine(parts[-1], datetime.min.time(), UTC) + timedelta(days=1)
    start = start.astimezone(UTC) if start else lo
    end = end.astimezone(UTC) if end else hi
    if end <= start:
        raise invalid("'to' must be after 'from'")
    if end - start > timedelta(days=settings.max_window_days):
        raise ApiError(
            422, "window_too_large",
            f"time window exceeds {settings.max_window_days} days; pass a narrower from/to",
            {"max_window_days": settings.max_window_days},
        )  # fmt: skip
    return start, end


_BBOX = re.compile(r"^\s*-?[0-9.]+(\s*,\s*-?[0-9.]+){3}\s*$")


def parse_bbox(text: str | None) -> BBox | None:
    """'lon_min,lat_min,lon_max,lat_max' (GeoJSON order) -> BBox."""
    if text is None:
        return None
    if not _BBOX.match(text):
        raise invalid("bbox must be 'lon_min,lat_min,lon_max,lat_max'")
    lon_min, lat_min, lon_max, lat_max = (float(x) for x in text.split(","))
    if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
        raise invalid("bbox out of range or min >= max")
    return BBox("request", lat_min, lat_max, lon_min, lon_max)


def _manifest(settings: ApiSettings, dataset_id: str) -> DatasetManifest | None:
    p = settings.manifest_dir / f"{dataset_id}.canonical.json"
    if not p.exists():
        return None
    try:
        return DatasetManifest.model_validate_json(p.read_text())
    except ValueError:
        return None  # unreadable manifest: report the dataset without it


def _admission(settings: ApiSettings, dataset_id: str) -> Admission | None:
    """Per-stratum admission from the newest coverage audit run on this dataset."""
    for summary in sorted(settings.runs_dir.glob("coverage_audit_*/summary.json"), reverse=True):
        try:
            s = json.loads(summary.read_text())
        except (OSError, ValueError):
            continue
        rows = s.get("admission") or []
        if s.get("dataset_id") != dataset_id or not rows or "stratum" not in rows[0]:
            continue
        strata: dict[str, dict[str, str]] = {}
        for r in rows:
            strata.setdefault(r["stratum"], {})[str(r["horizon_h"])] = r["status"]
        return Admission(
            run_id=summary.parent.name, rule=s.get("admission_config", {}), strata=strata
        )
    return None


def _model_context(
    settings: ApiSettings, store: ParquetStore, dataset_id: str | None
) -> ModelContext:
    if dataset_id is None:
        return ModelContext()
    proto = settings.deployment_protocol
    mpath = settings.manifest_dir / "splits" / f"{proto}__{dataset_id}.json"
    sha = hashlib.sha256(mpath.read_bytes()).hexdigest() if mpath.exists() else None
    return ModelContext(store=store, dataset_id=dataset_id, protocol=proto, manifest_sha256=sha)


def _data_version(settings: ApiSettings, store: ParquetStore, dataset_id: str) -> str:
    """Identity of the data a forecast read, part of every cache key: the dataset manifest's
    mapping, inputs and update time, plus every partition file's name, size and mtime (so a
    rewrite that keeps row counts still changes it)."""
    m = _manifest(settings, dataset_id)
    files = store.files(dataset_id)
    basis = json.dumps(
        {
            "mapping": m.mapping_id if m else None,
            "inputs": [i.sha256 for i in m.inputs] if m else None,
            "updated_at": str(m.updated_at) if m else None,
            "files": [(f.parent.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files],
        },
        sort_keys=True,
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# -- app ---------------------------------------------------------------------------------
def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings()
    app = FastAPI(
        title="Maritime Forecast Lab API",
        version=version("maritime-forecast"),
        description="Read-only access to canonical AIS datasets. Errors are always "
        '`{"error": {"code", "message", "details"}}`. Times are UTC; query times must carry '
        "an offset.",
    )
    app.state.ctx = Context(settings)
    errors.install(app)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )  # fmt: skip
    R = errors.ERROR_RESPONSES

    @app.get("/health", response_model=Health, tags=["meta"])
    def health(ctx: CtxDep, store: StoreDep) -> Health:
        return Health(
            version=app.version,
            git_sha=ctx.git_sha,
            data_root_exists=settings.store_root.exists(),
            datasets=len(store.datasets()),
            models=model_ids(),
        )

    @app.get("/datasets", response_model=DatasetList, tags=["data"])
    def datasets(store: StoreDep) -> DatasetList:
        out = []
        for ds in store.datasets():
            parts = store.partitions(ds)
            if not parts:
                continue
            m = _manifest(settings, ds)
            ingest = None
            if m is not None:
                r = m.report
                ingest = IngestTotals(
                    rows_in=r.rows_in, rows_out=r.rows_out, dropped=r.dropped,
                    duplicates_removed=r.duplicates_removed, flags=r.flags,
                )  # fmt: skip
            out.append(
                DatasetSummary(
                    dataset_id=ds,
                    has_manifest=m is not None,
                    source_id=m.source_id if m else None,
                    mapping_id=m.mapping_id if m else None,
                    canonical_version=m.canonical_version if m else None,
                    static_fields_provenance=m.static_fields_provenance if m else None,
                    region=m.region if m else None,
                    first_day=parts[0],
                    last_day=parts[-1],
                    days=len(parts),
                    rows=m.rows if m else None,
                    ingest=ingest,
                    admission=_admission(settings, ds),
                )
            )
        return DatasetList(datasets=out)

    @app.get("/vessels", response_model=VesselPage, responses=R, tags=["data"])
    def vessels(
        store: StoreDep,
        dataset_id: DatasetQ,
        start: FromQ = None,
        end: ToQ = None,
        bbox: Annotated[
            str | None, Query(max_length=100, description="lon_min,lat_min,lon_max,lat_max")
        ] = None,  # noqa: E501
        limit: Annotated[int, Query(ge=1, le=settings.max_vessel_page)] = 100,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> VesselPage:
        box = parse_bbox(bbox)
        lo, hi = _window(store, dataset_id, start, end, settings)
        page, total = store.vessels(dataset_id, lo, hi, box, limit=limit, offset=offset)
        return VesselPage(
            dataset_id=dataset_id, from_=lo, to=hi, total=total, limit=limit, offset=offset,
            bbox=[box.lon_min, box.lat_min, box.lon_max, box.lat_max] if box else None,
            vessels=[VesselSummary(**r) for r in page.to_dicts()],
        )  # fmt: skip

    @app.get("/vessels/{vessel_id}/track", response_model=Track, responses=R, tags=["data"])
    def track(
        store: StoreDep,
        vessel_id: Annotated[str, PathParam(pattern=VESSEL_ID_PATTERN)],
        dataset_id: DatasetQ,
        start: FromQ = None,
        end: ToQ = None,
    ) -> Track:
        lo, hi = _window(store, dataset_id, start, end, settings)
        df = store.track(dataset_id, vessel_id, lo, hi)
        if df.height == 0:
            raise ApiError(
                404, "vessel_not_found", f"no reports for vessel {vessel_id!r} in this window",
                {"dataset_id": dataset_id, "from": lo.isoformat(), "to": hi.isoformat()},
            )  # fmt: skip
        if df.height > settings.max_track_points:
            raise ApiError(
                413, "too_many_points",
                f"{df.height} reports exceed the limit of {settings.max_track_points}; "
                "narrow from/to", {"points": df.height, "limit": settings.max_track_points},
            )  # fmt: skip
        times = df["observed_at"].to_list()
        seg = gap_segments(times, settings.gap_minutes)
        seg_rows, gap_rows = segments_and_gaps(times, seg)
        segments = [Segment(**r) for r in seg_rows]
        gaps = [Gap(**r) for r in gap_rows]
        provenance = df["static_fields_provenance"].drop_nulls().mode().to_list()
        provenance = provenance[0] if provenance else None
        classes = df["vessel_class"].drop_nulls().mode().to_list()
        warnings = []
        if provenance == "backfilled":
            warnings.append("Static fields (type, dimensions) are back-filled by the source, "
                            "not known as of each report.")  # fmt: skip
        flagged = int((df["quality_flags"].list.len() > 0).sum())
        if flagged:
            warnings.append(f"{flagged} report(s) carry quality flags.")
        return Track(
            dataset_id=dataset_id, vessel_id=vessel_id, from_=lo, to=hi,
            gap_minutes=settings.gap_minutes, n_points=df.height,
            vessel_class=sorted(classes)[0] if classes else None,
            static_fields_provenance=provenance, warnings=warnings,
            segments=segments, gaps=gaps,
            points=TrackPoints(
                observed_at=times,
                latitude_deg=df["latitude_deg"].to_list(),
                longitude_deg=df["longitude_deg"].to_list(),
                sog_knots=df["sog_knots"].to_list(),
                cog_deg=df["cog_deg"].to_list(),
                heading_deg=df["heading_deg"].to_list(),
                nav_status=df["nav_status"].to_list(),
                segment=seg,
                quality_flags=[list(f) for f in df["quality_flags"].to_list()],
            ),
        )  # fmt: skip

    @app.get("/models", response_model=ModelList, tags=["models"])
    def models(
        store: StoreDep,
        dataset_id: Annotated[str | None, Query(pattern=DATASET_ID_PATTERN)] = None,
    ) -> ModelList:
        if dataset_id is not None:
            _partitions(store, dataset_id)
        ctx = _model_context(settings, store, dataset_id)
        out = []
        for m in model_ids():
            model = get_model(m, ctx)
            c = model.capabilities()
            out.append(
                ModelInfo(
                    model_id=c.model_id,
                    model_version=c.model_version,
                    description=c.description,
                    installed=c.installed,
                    calibration_only=c.calibration_only,
                    reads_beyond_own_history=c.reads_beyond_own_history,
                    max_horizon_hours=c.max_horizon_hours,
                    step_minutes=c.step_minutes,
                    min_history_reports=c.min_history_reports,
                    config=model.config,
                )
            )
        return ModelList(
            dataset_id=dataset_id,
            parameters_from=settings.deployment_protocol if dataset_id else None,
            models=out,
        )

    cache: OrderedDict[str, ForecastResponse] = OrderedDict()

    def _radii_file(dataset_id: str) -> tuple[dict | None, str | None]:
        """The radii file's params and sha256, read once per request."""
        p = settings.conformal_dir / f"{settings.deployment_protocol}__{dataset_id}.json"
        if not p.exists():
            return None, None
        raw = p.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()

    def _with_radii(req, mctx, history, horizon, forecasts, params):
        """Attach split-conformal radii (Milestone 4) to every ok forecast, or say why not."""
        if req.conformal_level is None:
            return forecasts
        mpath = (
            settings.manifest_dir
            / "splits"
            / f"{settings.deployment_protocol}__{req.dataset_id}.json"
        )

        def refuse(why):
            return [f.model_copy(update={"warnings": [*f.warnings, f"no radii: {why}"]})
                    if f.status == "ok" else f for f in forecasts]  # fmt: skip

        if params is None or not mpath.exists():
            return refuse(f"none fitted for {settings.deployment_protocol} on {req.dataset_id}")
        why = conformal.calibration_like(history.reports, history.as_of, req.history_hours,
                                         default_region("cutoff_region"))  # fmt: skip
        if why:
            return refuse(f"the forecast does not resemble the calibration samples ({why})")
        # The radii describe forecasts requested exactly as the calibration run requested them.
        creq = params.get("request")
        if creq is None:
            return refuse("the radii file does not record its calibration request; refit it "
                          "(scripts/conformal.py --params-only)")  # fmt: skip
        if (req.step_minutes, req.history_hours) != (creq["step_minutes"],
                                                     creq["history_hours"]):  # fmt: skip
            return refuse(
                f"the request (history {req.history_hours:g} h, step {req.step_minutes} min) "
                f"differs from the calibration run's (history {creq['history_hours']:g} h, "
                f"step {creq['step_minutes']} min)"
            )
        manifest = SplitManifest.model_validate_json(mpath.read_bytes())
        motion = conformal.motion_state(history.reports)
        cls = conformal.last_vessel_class(history.reports)
        configs = {m: get_model(m, mctx).config for m in req.model_ids}
        out = []
        for f in forecasts:
            if f.status != "ok":
                out.append(f)
                continue
            predicted_h = f.predicted_horizon_hours or req.horizon_hours
            if predicted_h != creq["horizon_h"]:
                why = (f"{f.model_id} predicted a {predicted_h:g} h path; the radii were "
                       f"calibrated on {creq['horizon_h']:g} h paths")  # fmt: skip
                out.append(f.model_copy(update={"warnings": [*f.warnings, f"no radii: {why}"]}))
                continue
            region, why = conformal.region_for(
                params, manifest, mctx.manifest_sha256, f.model_id, f.model_version, motion, cls,
                history.as_of, horizon.target_times, req.conformal_level,
                config_sha(configs[f.model_id]),
            )  # fmt: skip
            if region is None:
                out.append(f.model_copy(update={"warnings": [*f.warnings, f"no radii: {why}"]}))
            else:
                out.append(f.model_copy(update={"uncertainty": ConformalRegion(**region)}))
        return out

    def _forecast(store: ParquetStore, req: ForecastRequest) -> ForecastResponse:
        """The causal forecast for a request, cached by request, models, data and code."""
        _partitions(store, req.dataset_id)
        unknown = [m for m in req.model_ids if m not in model_ids()]
        if unknown:
            raise ApiError(
                422, "unknown_model", f"unknown model(s) {unknown}", {"available": model_ids()}
            )
        ctx = _model_context(settings, store, req.dataset_id)
        built = {m: get_model(m, ctx) for m in req.model_ids}
        data_version = _data_version(settings, store, req.dataset_id)
        params, radii_sha = _radii_file(req.dataset_id)
        key_basis = json.dumps(
            {
                "request": req.model_dump(mode="json", exclude={"show_ground_truth"}),
                "configs": {m: built[m].config for m in req.model_ids},
                "versions": {m: built[m].capabilities().model_version for m in req.model_ids},
                "data_version": data_version,
                "code": app.state.ctx.git_sha,
                "radii": radii_sha,
                "manifest": ctx.manifest_sha256,
            },
            sort_keys=True,
            default=str,
        )
        forecast_id = hashlib.sha256(key_basis.encode()).hexdigest()[:24]
        if forecast_id in cache:
            cache.move_to_end(forecast_id)
            return cache[forecast_id]
        as_of = req.as_of.astimezone(UTC)
        history = history_from_store(store, req.dataset_id, req.vessel_id, as_of, req.history_hours)
        horizon = ForecastHorizon(as_of, req.horizon_hours, req.step_minutes)
        n_steps = len(horizon.target_times)
        predict_h = {m: _predict_horizon(req, params, m, built[m].capabilities().max_horizon_hours)
                     for m in req.model_ids}  # fmt: skip
        times = history.reports["observed_at"].to_list()
        segs = gap_segments(times, settings.gap_minutes) if times else []
        manifest = _manifest(settings, req.dataset_id)
        resp = ForecastResponse(
            forecast_id=forecast_id,
            dataset_id=req.dataset_id,
            vessel_id=req.vessel_id,
            as_of=as_of,
            source=manifest.source_id if manifest else req.dataset_id,
            data_version=data_version,
            input_coverage={
                "n_reports": len(times),
                "first_report": times[0] if times else None,
                "last_report": times[-1] if times else None,
                "last_report_age_s": (as_of - times[-1]).total_seconds() if times else None,
                "n_gaps": max(segs) if segs else 0,
                "motion_state": conformal.motion_state(history.reports),
                "vessel_class": conformal.last_vessel_class(history.reports) if times else None,
            },
            forecasts=_with_radii(
                req,
                ctx,
                history,
                horizon,
                [
                    _prefix(
                        built[m].predict(
                            history, ForecastHorizon(as_of, predict_h[m], req.step_minutes)
                        ),
                        n_steps,
                        predict_h[m],
                    )
                    for m in req.model_ids
                ],
                params,
            ),
        )
        cache[forecast_id] = resp  # request-independent: no per-request warnings inside
        if len(cache) > settings.forecast_cache_size:
            cache.popitem(last=False)
        return resp

    @app.post("/forecasts", response_model=ForecastResponse, responses=R, tags=["models"])
    def forecasts(store: StoreDep, req: ForecastRequest) -> ForecastResponse:
        resp = _forecast(store, req)
        warnings = []
        if req.show_ground_truth:
            warnings.append(
                "show_ground_truth: truth is never part of a forecast response; "
                "POST the same request to /forecasts/evaluation"
            )
        return resp.model_copy(update={"warnings": warnings})

    @app.post(
        "/forecasts/evaluation", response_model=ForecastEvaluation, responses=R, tags=["models"]
    )
    def forecast_evaluation(store: StoreDep, req: ForecastRequest) -> ForecastEvaluation:
        """Withheld truth and per-target-time errors for the forecast this request produces.

        The forecast is the one `POST /forecasts` serves (same `forecast_id`); truth is read
        only after it is made, from the truth region, never bridged across gaps."""
        as_of = req.as_of.astimezone(UTC)
        mpath = (settings.manifest_dir / "splits"
                 / f"{settings.deployment_protocol}__{req.dataset_id}.json")  # fmt: skip
        if mpath.exists() and touches_test(
            SplitManifest.model_validate_json(mpath.read_bytes()), as_of,
            as_of + timedelta(hours=req.horizon_hours, minutes=settings.max_bracket_gap_minutes),
        ):  # fmt: skip
            raise ApiError(
                403, "holdout_test_period",
                f"{req.dataset_id} holds out this period as an untouched test set; its truth is "
                "scored only by a confirmed benchmark (docs/evaluation-protocol.md)",
            )  # fmt: skip
        resp = _forecast(store, req)
        hist = history_from_store(store, req.dataset_id, req.vessel_id, as_of, req.history_hours)
        anchor = None
        if hist.reports.height:
            last = hist.reports.row(-1, named=True)
            anchor = {
                "lat": last["latitude_deg"],
                "lon": last["longitude_deg"],
                "sog_knots": last["sog_knots"],
                "observed_at": last["observed_at"],
            }
        warnings = []
        mask = None
        try:
            mctx = _model_context(settings, store, req.dataset_id)
            found = load_navigable_mask(
                settings.deployment_protocol, req.dataset_id, mctx.manifest_sha256
            )
        except MaskMismatchError as e:
            found = None
            warnings.append(f"navigable mask not used: {e}")
        if found is not None:
            mask, side = found
            if as_of < datetime.fromisoformat(side["traffic"]["data_until"]):
                # The mask's traffic cells could include this vessel's own track after as_of
                # (its truth): land flags would favour paths that follow the truth.
                mask = None
                warnings.append(
                    f"land flags not scored: as_of is inside the {settings.deployment_protocol} "
                    "train split the navigable mask was built from"
                )
        if anchor is None:
            warnings.append("no report in the history window: feasibility not scored")
        ev = evaluate_forecast(
            store, resp, req.step_minutes, settings.max_bracket_gap_minutes, anchor, mask
        )
        return ev.model_copy(update={"warnings": ev.warnings + warnings})

    # -- benchmarks ------------------------------------------------------------------------
    @app.post(
        "/benchmarks", response_model=BenchmarkStatus, status_code=202, responses=R,
        tags=["benchmarks"],
    )  # fmt: skip
    def start_benchmark(
        store: StoreDep, ctx: CtxDep, req: BenchmarkRequest, response: Response
    ) -> BenchmarkStatus:
        """Start an unattended paired benchmark in a background process; poll its status."""
        _partitions(store, req.dataset_id)
        mpath = settings.manifest_dir / "splits" / f"{req.protocol}__{req.dataset_id}.json"
        if not mpath.exists():
            raise ApiError(
                404, "manifest_not_found",
                f"no {req.protocol} split manifest for {req.dataset_id!r}; run make splits",
            )  # fmt: skip
        cfg = BenchmarkConfig(
            dataset_id=req.dataset_id,
            manifest_path=mpath,
            model_ids=tuple(req.model_ids),
            horizons_h=tuple(req.horizons_hours),
            step_minutes=req.step_minutes,
            history_hours=req.history_hours,
            seed=req.seed,
            n_boot=req.n_boot,
            max_cutoffs_per_set=req.max_cutoffs_per_set,
        )
        try:  # the runner's own checks, before anything is started
            manifest, mctx = benchmark_context(cfg, store)
            if is_holdout(manifest):
                raise ValueError(
                    f"{req.dataset_id} has a held-out test period; it is scored only by a "
                    "confirmed CLI benchmark (run_benchmark.py --confirm-test), never by the API"
                )
            preflight(cfg, mctx)
        except (ValueError, NotImplementedError) as e:
            raise ApiError(422, "benchmark_refused", str(e)) from None
        started = datetime.now(UTC)
        run_id = f"{req.protocol}_{req.dataset_id}_{started:%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"
        ctx.jobs.start(cfg, run_id)
        response.headers["Location"] = f"/benchmarks/{run_id}"
        return BenchmarkStatus(run_id=run_id, status="running")

    RunIdP = Annotated[str, PathParam(pattern=RUN_ID_PATTERN)]

    @app.get("/benchmarks", response_model=BenchmarkList, tags=["benchmarks"])
    def benchmarks(ctx: CtxDep) -> BenchmarkList:
        """Every benchmark run, newest first (headers only; open one to verify and read it)."""
        return BenchmarkList(runs=ctx.jobs.list())

    @app.get(
        "/benchmarks/{run_id}", response_model=BenchmarkStatus, responses=R, tags=["benchmarks"]
    )
    def benchmark(ctx: CtxDep, run_id: RunIdP) -> BenchmarkStatus:
        """Status, and once finalized the full immutable run record (checksums verified)."""
        return ctx.jobs.status(run_id)

    @app.get(
        "/benchmarks/{run_id}/summary", response_model=BenchmarkSummary, responses=R,
        tags=["benchmarks"],
    )  # fmt: skip
    def benchmark_summary(
        ctx: CtxDep,
        run_id: RunIdP,
        stratum: Annotated[list[str] | None, Query(max_length=50)] = None,
        headline_only: bool = False,
    ) -> BenchmarkSummary:
        """Model x horizon x stratum table: medians with CIs and skill, side by side per
        paired cell."""
        st = ctx.jobs.status(run_id)
        if st.run is None:
            raise ApiError(
                409, "run_not_finished", f"run {run_id!r} is {st.status}",
                {"status": st.status},
            )  # fmt: skip
        return summarize(st.run, set(stratum) if stratum else None, headline_only)

    @app.get(
        "/benchmarks/{run_id}/samples", response_model=BenchmarkSamples, responses=R,
        tags=["benchmarks"],
    )  # fmt: skip
    def benchmark_samples(
        ctx: CtxDep,
        run_id: RunIdP,
        stratum: Annotated[str, Query(max_length=64)],
        horizon_minutes: float,
        model_id: Annotated[str, Query(max_length=64)],
        sort: Literal["error", "loss"] = "loss",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> BenchmarkSamples:
        """Failure gallery: one cell's paired samples for a model, worst first, by its error or
        by its loss to the cell's best baseline (error minus the baseline's, same sample).

        Per-sample records of a held-out test run are refused: every look at them belongs in
        the test-access ledger, which only the CLI writes. Their aggregates stay readable."""
        st = ctx.jobs.status(run_id)  # verifies every checksum
        run = st.run
        if run is None:
            raise ApiError(409, "run_not_finished", f"run {run_id!r} is {st.status}",
                           {"status": st.status})  # fmt: skip
        d = ctx.jobs.runs.path(run_id)
        why = _holdout_records(settings, run, d / "cutoffs.parquet")
        if why:
            raise ApiError(
                403, "holdout_test_period",
                f"{run_id} {why}; its per-sample records are read only through a recorded "
                "analysis (docs/evaluation-protocol.md), not by the API",
            )  # fmt: skip
        cell = [r for r in run.results
                if r.stratum == stratum and r.horizon_minutes == horizon_minutes]  # fmt: skip
        if not cell:
            raise ApiError(
                404, "cell_not_found", f"no scored cell {stratum} @ {horizon_minutes} min"
            )
        if model_id not in {r.model_id for r in cell}:
            raise ApiError(404, "model_not_in_run", f"{model_id} is not in {run_id}")
        ref = cell[0].best_baseline_id
        if sort == "loss" and (ref is None or ref == model_id):
            raise ApiError(
                422, "no_reference",
                f"{model_id} has no other skill reference in this cell; sort by error instead",
            )  # fmt: skip
        motion, _, cls = stratum.partition(":")
        if cls == "ALL":
            where, params = "m.motion_set = ?", [motion]
        elif cls == "TRANSITING":
            where, params = "m.transiting", []
        else:
            where, params = (
                "m.motion_set = ? AND coalesce(nullif(m.vessel_class, ''), 'unknown') = ?"
            ), [
                motion, cls,
            ]  # fmt: skip
        order = "m.error_m - b.error_m" if sort == "loss" else "m.error_m"
        sql = f"""
            WITH m AS (SELECT * FROM read_parquet(?) WHERE model_id = ?
                       AND abs(horizon_h - ?) < 1e-9 AND evaluable
                       AND (paired OR status <> 'ok')),
                 b AS (SELECT cutoff_id, error_m, pred_lat, pred_lon FROM read_parquet(?)
                       WHERE model_id = ? AND abs(horizon_h - ?) < 1e-9),
                 sel AS (SELECT m.*, b.error_m AS reference_error_m,
                                b.pred_lat AS reference_pred_lat, b.pred_lon AS reference_pred_lon,
                                {order} AS score
                         FROM m LEFT JOIN b USING (cutoff_id) WHERE {where})
            SELECT sel.*, c.latitude_deg AS anchor_lat, c.longitude_deg AS anchor_lon,
                   (SELECT count(*) FILTER (WHERE paired) FROM sel) AS n_paired,
                   (SELECT count(*) FILTER (WHERE status <> 'ok') FROM sel) AS n_failed
            FROM sel JOIN read_parquet(?) c USING (cutoff_id)
            ORDER BY status <> 'ok' DESC, score DESC NULLS LAST, cutoff_id LIMIT ?
        """  # fmt: skip
        records = str(d / "records.parquet")
        h = horizon_minutes / 60
        con = connect()
        try:
            cols = {c[0] for c in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [records]).fetchall()}  # fmt: skip
            missing = sorted(GALLERY_COLUMNS - cols)
            if missing:
                raise ApiError(
                    409, "records_without_positions",
                    f"{run_id} predates per-sample positions in its records (missing "
                    f"{', '.join(missing)}); open a later run of the same models",
                )  # fmt: skip
            rows = con.execute(
                sql, [records, model_id, h, records, ref or model_id, h, *params,
                      str(d / "cutoffs.parquet"), limit],
            ).pl()  # fmt: skip
        finally:
            con.close()
        n_paired = int(rows["n_paired"][0]) if rows.height else 0
        n_failed = int(rows["n_failed"][0]) if rows.height else 0
        return BenchmarkSamples(
            run_id=run_id, dataset_id=run.dataset_id, stratum=stratum,
            horizon_minutes=horizon_minutes, model_id=model_id, reference_id=ref, sort=sort,
            n_paired=n_paired, n_failed=n_failed,
            samples=[
                BenchmarkSample(
                    **{k: r[k] for k in BenchmarkSample.model_fields
                       if k not in ("as_of", "land_point", "land_crossing")},
                    as_of=r["observed_at"], land_point=r["feas_land_point"],
                    land_crossing=r["feas_land_crossing"],
                )
                for r in rows.to_dicts()
            ],
        )  # fmt: skip

    return app


def _holdout_records(settings: ApiSettings, run, cutoffs_path: Path) -> str | None:
    """Why a run's per-sample records are held-out test records (None if they are not).

    Decided on the times the records cover, not only on names: a run on a held-out test split,
    or any run whose cutoffs plus horizon and truth bracket reach into the test period of its
    protocol's current hold-out manifest."""
    if run.protocol.manifest_version == HOLDOUT_MANIFEST_VERSION and (
        run.sampler.get("split") == "test"
    ):
        return "scored the held-out test split"
    mpath = (settings.manifest_dir / "splits"
             / f"{run.protocol.protocol}__{run.dataset_id}.json")  # fmt: skip
    if not mpath.exists():
        return None
    manifest = SplitManifest.model_validate_json(mpath.read_bytes())
    if not is_holdout(manifest):
        return None
    con = connect()
    try:
        lo, hi = con.execute(
            "SELECT min(observed_at), max(observed_at) FROM read_parquet(?)", [str(cutoffs_path)]
        ).fetchone()
    finally:
        con.close()
    if lo is None:
        return None
    bracket = run.sampler.get("max_bracket_gap_minutes", settings.max_bracket_gap_minutes)
    end = hi + timedelta(minutes=max(run.horizons_minutes) + bracket)
    if touches_test(manifest, lo, end):
        return "has records whose labels reach into the held-out test period"
    return None


def _predict_horizon(req: ForecastRequest, params: dict | None, model_id: str,
                     max_horizon_h: float) -> float:  # fmt: skip
    """The horizon a model is asked for. An explicit `predict_horizon_hours` wins (replaying a
    benchmark run). Otherwise a model with conformal radii predicts as their calibration run
    did, once at its longest horizon, when the request matches that run's step and history
    (retrieval chooses its path over the whole horizon asked for). Else the requested one."""
    if req.predict_horizon_hours is not None:
        return req.predict_horizon_hours
    creq = (params or {}).get("request")
    if (
        creq is None
        or model_id not in params.get("models", {})
        or (req.step_minutes, req.history_hours) != (creq["step_minutes"], creq["history_hours"])
        or req.horizon_hours >= creq["horizon_h"]
        or creq["horizon_h"] > max_horizon_h
        or creq["horizon_h"] * 60 / req.step_minutes > MAX_TARGET_STEPS
    ):
        return req.horizon_hours
    return float(creq["horizon_h"])


def _prefix(f: ModelForecast, n: int, predicted_h: float) -> ModelForecast:
    """The first `n` target times of a forecast predicted at `predicted_h` (and of each
    candidate path), labelled with the horizon it was predicted at."""
    if len(f.target_times) <= n:
        return f
    end = f.target_times[n - 1]
    trajectories = [
        t.model_copy(
            update={
                "points": t.points[:n],
                # null means the reference covers every (remaining) target time
                "reference_ends_at": t.reference_ends_at
                if t.reference_ends_at is not None and t.reference_ends_at <= end
                else None,
            }
        )  # fmt: skip
        for t in f.trajectories
    ]
    note = (f"predicted at {predicted_h:g} h (as the calibration run did) and cut to the "
            "request; warnings may refer to times after the last one shown")  # fmt: skip
    return f.model_copy(update={
        "target_times": f.target_times[:n], "points": f.points[:n],
        "trajectories": trajectories, "predicted_horizon_hours": predicted_h,
        "warnings": [*f.warnings, note] if f.warnings else f.warnings,
    })  # fmt: skip


def summarize(run, strata: set[str] | None = None, headline_only: bool = False):
    """Pivot a run's per-model CellResults into one row per (stratum, horizon) cell."""
    cells: dict[tuple[str, float], list] = {}
    for r in run.results:
        if strata is not None and r.stratum not in strata:
            continue
        cells.setdefault((r.stratum, r.horizon_minutes), []).append(r)
    rows = []
    for (stratum, h), rs in sorted(cells.items()):
        first = rs[0]
        shared = ("headline", "admission", "n_evaluable", "n_paired", "n_vessels",
                  "sample_set_sha256", "best_baseline_id")  # fmt: skip
        if any(getattr(r, k) != getattr(first, k) for r in rs for k in shared):
            raise ApiError(
                500, "run_inconsistent", f"cell {stratum} @ {h} min differs between models",
                {"run_id": run.run_id},
            )  # fmt: skip
        if headline_only and not first.headline:
            continue
        rows.append(
            SummaryCell(
                stratum=stratum,
                horizon_minutes=h,
                admission=first.admission,
                headline=first.headline,
                n_evaluable=first.n_evaluable,
                n_paired=first.n_paired,
                n_vessels=first.n_vessels,
                sample_set_sha256=first.sample_set_sha256,
                best_baseline_id=first.best_baseline_id,
                models={
                    r.model_id: SummaryModelCell(
                        median_error_m=r.median_error_m,
                        p90_error_m=r.p90_error_m,
                        n_failed=r.n_failed,
                        failure_rate=r.failure_rate,
                        skill_vs_best_baseline=r.skill_vs_best_baseline,
                        feasibility=r.feasibility,
                    )
                    for r in rs
                },
            )
        )
    return BenchmarkSummary(
        run_id=run.run_id,
        status=run.status,
        created_at=run.created_at,
        git_sha=run.git_sha,
        dataset_id=run.dataset_id,
        data_provenance=run.data_provenance,
        protocol=run.protocol,
        models=run.models,
        horizons_minutes=run.horizons_minutes,
        n_cutoffs=run.sampler.get("n_cutoffs"),
        seed=run.sampler.get("seed"),
        max_cutoffs_per_set=run.sampler.get("max_cutoffs_per_set"),
        feasibility=run.feasibility,
        cells=rows,
        not_evaluable=run.not_evaluable,
        dropped_cells=[d for d in run.dropped_cells if strata is None or d.stratum in strata],
        warnings=run.warnings,
        error=run.error,
    )


def __getattr__(name: str):  # lazy module-level app for `uvicorn maritime_forecast.api.app:app`
    if name == "app":
        return create_app()
    raise AttributeError(name)
