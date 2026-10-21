"""FastAPI application (build plan §8): read-only endpoints over the Parquet store.

Run:  make api      (uvicorn maritime_forecast.api.app:app --reload --port 8000)
Docs: http://localhost:8000/docs
"""

# No `from __future__ import annotations`: FastAPI must resolve the Annotated limits that
# reference `settings` inside create_app at definition time.
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime

from maritime_forecast.api import errors
from maritime_forecast.api.errors import ApiError, dataset_not_found, invalid
from maritime_forecast.api.models import (
    Admission,
    DatasetList,
    DatasetSummary,
    Gap,
    Health,
    IngestTotals,
    ModelInfo,
    ModelList,
    Segment,
    Track,
    TrackPoints,
    VesselPage,
    VesselSummary,
)
from maritime_forecast.config import MANIFEST_DIR, RUNS_DIR, BBox
from maritime_forecast.data.segment import (
    DEFAULT_SEGMENT_CONFIG,
    gap_segments,
    segments_and_gaps,
)
from maritime_forecast.models.base import ForecastHorizon, ModelContext, history_from_store
from maritime_forecast.models.registry import get_model, model_ids
from maritime_forecast.provenance import git_sha
from maritime_forecast.schemas.dataset import DatasetManifest
from maritime_forecast.schemas.forecast import ForecastRequest, ForecastResponse
from maritime_forecast.storage.parquet_store import CANONICAL_ROOT, ParquetStore, connect

DATASET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
VESSEL_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


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
    forecast_cache_size: int = 256
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

    @app.post("/forecasts", response_model=ForecastResponse, responses=R, tags=["models"])
    def forecasts(store: StoreDep, req: ForecastRequest) -> ForecastResponse:
        _partitions(store, req.dataset_id)
        unknown = [m for m in req.model_ids if m not in model_ids()]
        if unknown:
            raise ApiError(
                422, "unknown_model", f"unknown model(s) {unknown}", {"available": model_ids()}
            )
        ctx = _model_context(settings, store, req.dataset_id)
        built = {m: get_model(m, ctx) for m in req.model_ids}
        data_version = _data_version(settings, store, req.dataset_id)
        key_basis = json.dumps(
            {
                "request": req.model_dump(mode="json", exclude={"show_ground_truth"}),
                "configs": {m: built[m].config for m in req.model_ids},
                "versions": {m: built[m].capabilities().model_version for m in req.model_ids},
                "data_version": data_version,
                "code": app.state.ctx.git_sha,
            },
            sort_keys=True,
            default=str,
        )
        forecast_id = hashlib.sha256(key_basis.encode()).hexdigest()[:24]
        warnings = []
        if req.show_ground_truth:
            warnings.append(
                "show_ground_truth: the evaluation endpoint is not available yet; "
                "truth is never part of a forecast response"
            )
        if forecast_id in cache:
            cache.move_to_end(forecast_id)
            return cache[forecast_id].model_copy(update={"warnings": warnings})
        as_of = req.as_of.astimezone(UTC)
        history = history_from_store(store, req.dataset_id, req.vessel_id, as_of, req.history_hours)
        horizon = ForecastHorizon(as_of, req.horizon_hours, req.step_minutes)
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
            },
            forecasts=[built[m].predict(history, horizon) for m in req.model_ids],
        )
        cache[forecast_id] = resp  # request-independent: no per-request warnings inside
        if len(cache) > settings.forecast_cache_size:
            cache.popitem(last=False)
        return resp.model_copy(update={"warnings": warnings})

    return app


def __getattr__(name: str):  # lazy module-level app for `uvicorn maritime_forecast.api.app:app`
    if name == "app":
        return create_app()
    raise AttributeError(name)
