"""The HTTP surface: schema validation, correlation IDs, and nothing CPU-bound.

This process never links `libslic3r` and never parses model geometry. Uploaded
bytes are streamed to disk and only ever read by a disposable worker process.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from web_job_directory import JOB_ID_PATTERN
from web_profile_catalog import KINDS, ProfileCatalogError, load_catalog
from web_settings_catalog import SettingsCatalogError, evaluate_compatibility, load_settings_catalog

from . import API_VERSION, PROTOCOL_VERSION
from .config import ApiConfig, from_environment
from .errors import ApiError, from_catalog_error
from .jobs import (
    ARTIFACT_NAMES,
    MAX_FILAMENT_PROFILES,
    MAX_OBJECT_TRANSFORMS,
    MAX_SETTINGS,
    TRANSFORM_LENGTH,
    JobService,
    SceneRequest,
    SliceRequest,
)
from .uploads import UploadStore


CORRELATION_HEADER = "X-Correlation-Id"
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PREFIX = f"/api/{API_VERSION}"
# A print taller than this has no browsable preview anyway, and the bound keeps
# an absurd path parameter from reaching the job service at all.
MAX_PREVIEW_LAYER = 1_000_000
# A scene's own object count is not capped by the API (a model may have many
# parts); this only keeps an absurd path parameter from reaching the service.
MAX_SCENE_OBJECT_INDEX = 1_000_000
PREVIEW_CHUNK_BYTES = 256 * 1024

logger = logging.getLogger("orca.web.api")


def _read_range(path, offset: int, length: int):
    """Yield one byte range of a file without holding the whole file."""
    with open(path, "rb") as stream:
        stream.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = stream.read(min(PREVIEW_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


class ObjectPlacement(BaseModel):
    """One explicit per-object transform, indexing into an inspected scene.

    Structural shape is checked here (count, exactly 16 entries); finiteness
    and the non-negative bound on `source_object` are checked again in
    `JobService`, which is reached by a retry as well as a fresh submission.
    """

    model_config = ConfigDict(extra="forbid")

    source_object: int = Field(ge=0)
    transform: List[float] = Field(min_length=TRANSFORM_LENGTH, max_length=TRANSFORM_LENGTH)
    # 1-based index into the request's filament_profiles; 0 (the default)
    # means "leave this object's own assignment alone", matching the worker.
    # The upper bound depends on how many filaments the request names, so it
    # is re-checked in `JobService` rather than here.
    filament: int = Field(default=0, ge=0)


class SliceSubmission(BaseModel):
    """One slice request. Unknown fields are refused rather than ignored."""

    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(min_length=1, max_length=64)
    machine_profile: str = Field(min_length=1, max_length=512)
    process_profile: str = Field(min_length=1, max_length=512)
    # Exactly one of these two must be named: `filament_profile` is the
    # single-filament spelling kept for compatibility, `filament_profiles`
    # names one profile per filament slot. Naming both is refused the same
    # way the worker itself refuses it (`invalid_profiles`), rather than one
    # silently winning.
    filament_profile: Optional[str] = Field(default=None, min_length=1, max_length=512)
    filament_profiles: Optional[List[str]] = Field(default=None, min_length=1, max_length=MAX_FILAMENT_PROFILES)
    settings: Dict[str, str] = Field(default_factory=dict, max_length=MAX_SETTINGS)
    plate_index: int = Field(default=1, ge=1)
    # Explicit per-object placement; empty keeps the worker's existing default
    # placement. See docs/web/scene-format.md for what `source_object` indexes.
    objects: List[ObjectPlacement] = Field(default_factory=list, max_length=MAX_OBJECT_TRANSFORMS)

    def to_request(self) -> SliceRequest:
        if self.filament_profile is not None and self.filament_profiles is not None:
            raise ApiError(
                "invalid_profiles", "Name either filament_profile or filament_profiles, not both."
            )
        if self.filament_profile is not None:
            filament_profiles = (self.filament_profile,)
        elif self.filament_profiles is not None:
            filament_profiles = tuple(self.filament_profiles)
        else:
            raise ApiError(
                "invalid_profiles", "A slice must name filament_profile or filament_profiles."
            )
        return SliceRequest(
            upload_id=self.upload_id,
            machine_profile=self.machine_profile,
            process_profile=self.process_profile,
            filament_profiles=filament_profiles,
            settings=dict(self.settings),
            plate_index=self.plate_index,
            objects=[_object_placement(entry) for entry in self.objects],
        )


def _object_placement(entry: ObjectPlacement) -> Dict[str, Any]:
    """Placement as a plain dict, omitting `filament` when it is the default.

    Keeps an unassigned object's manifest entry identical to what it was
    before per-object filament assignment existed.
    """
    placement: Dict[str, Any] = {"source_object": entry.source_object, "transform": list(entry.transform)}
    if entry.filament:
        placement["filament"] = entry.filament
    return placement


class SceneSubmission(BaseModel):
    """One inspect request. Unknown fields are refused rather than ignored.

    It names the same profile chain a slice of this upload would, because that
    chain is what the API flattens into the machine profile the inspect
    manifest carries; see `SceneRequest` for why process and filament are
    required even though only the machine reaches the worker.
    """

    model_config = ConfigDict(extra="forbid")

    machine_profile: str = Field(min_length=1, max_length=512)
    process_profile: str = Field(min_length=1, max_length=512)
    filament_profile: str = Field(min_length=1, max_length=512)
    plate_index: int = Field(default=1, ge=1)

    def to_request(self, upload_id: str) -> SceneRequest:
        return SceneRequest(
            upload_id=upload_id,
            machine_profile=self.machine_profile,
            process_profile=self.process_profile,
            filament_profile=self.filament_profile,
            plate_index=self.plate_index,
        )


def correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "")


def _job_id(job_id: str = PathParam(max_length=128)) -> str:
    if not JOB_ID_PATTERN.match(job_id):
        raise ApiError("unknown_job", "No job is known under that id.", 404)
    return job_id


def _load_engine_metadata(config: ApiConfig, catalog):
    """Ask the worker for the engine's setting metadata and compatibility rules.

    Both are engine facts rather than job work, so they are read once at startup
    from the trusted worker executable. A worker that cannot answer leaves the
    API serving jobs with the worker as the sole authority on a setting's
    validity, which is what it was before this catalog existed.
    """
    try:
        settings = load_settings_catalog(config.worker_command)
        resolved = catalog.resolve_conditions(
            lambda request: evaluate_compatibility(config.worker_command, str(request))
        )
    except (SettingsCatalogError, ProfileCatalogError) as error:
        logger.warning("engine metadata unavailable code=%s", getattr(error, "code", "unknown"))
        return None
    logger.info("engine metadata loaded settings=%d conditional_profiles=%d", len(settings), resolved)
    return settings


def create_app(config: Optional[ApiConfig] = None) -> FastAPI:
    resolved = config or from_environment()
    resolved.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        catalog = load_catalog(resolved.repo_root, resolved.profile_vendors)
        settings = _load_engine_metadata(resolved, catalog)
        uploads = UploadStore(resolved.uploads_root, resolved.job_limits.max_input_bytes)
        service = JobService(resolved, catalog, uploads, settings_catalog=settings)
        app.state.config = resolved
        app.state.catalog = catalog
        app.state.settings = settings
        app.state.uploads = uploads
        app.state.jobs = service
        # Reclaim whatever an earlier process left behind before serving.
        service.sweep()
        logger.info(
            "api ready profiles=%d settings=%d state_root=%s",
            len(catalog),
            len(settings) if settings else 0,
            resolved.state_root,
        )
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(
        title="OrcaWebSlicer API",
        version=API_VERSION,
        summary="Browser-facing slicing API backed by isolated native workers.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def attach_correlation_id(request: Request, call_next):
        supplied = request.headers.get(CORRELATION_HEADER, "")
        request.state.correlation_id = (
            supplied if CORRELATION_PATTERN.match(supplied) else uuid.uuid4().hex
        )
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = request.state.correlation_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        identifier = correlation_id(request)
        logger.info("request failed code=%s correlation_id=%s", error.code, identifier)
        return JSONResponse(
            status_code=error.status,
            content={
                "error": {"code": error.code, "message": str(error)},
                "correlation_id": identifier,
            },
            headers={CORRELATION_HEADER: identifier} if identifier else None,
        )

    @app.get(f"{PREFIX}/health", tags=["service"])
    def health() -> Dict[str, Any]:
        return {"status": "ok", "protocol_version": PROTOCOL_VERSION, "api_version": API_VERSION}

    @app.get(f"{PREFIX}/profiles", tags=["profiles"])
    def profiles(
        request: Request,
        printer: Optional[str] = Query(default=None, max_length=512),
    ) -> Dict[str, Any]:
        """List the bundled profiles, narrowed to one printer when asked."""
        catalog = request.app.state.catalog
        try:
            selected = catalog.get(printer, "machine") if printer else None
            return {
                kind: [entry.describe() for entry in catalog.list(kind, selected)] for kind in KINDS
            }
        except ProfileCatalogError as error:
            raise from_catalog_error(error) from error

    @app.get(f"{PREFIX}/settings", tags=["profiles"])
    def settings(request: Request) -> Dict[str, Any]:
        """Serve the engine's own definition of every curated setting."""
        catalog = request.app.state.settings
        if catalog is None:
            raise ApiError(
                "settings_catalog_unavailable",
                "The slicing engine could not describe its settings.",
                503,
            )
        return catalog.describe()

    @app.post(f"{PREFIX}/uploads", status_code=201, tags=["uploads"])
    def create_upload(request: Request, file: UploadFile = File()) -> Dict[str, Any]:
        """Store one model. The bytes are never parsed in this process."""
        record = request.app.state.uploads.create(file.filename, file.file)
        logger.info(
            "upload stored upload_id=%s format=%s bytes=%d correlation_id=%s",
            record.upload_id,
            record.model_format,
            record.size_bytes,
            correlation_id(request),
        )
        return record.describe()

    @app.post(f"{PREFIX}/uploads/{{upload_id}}/scene", status_code=202, tags=["scenes"])
    def submit_scene(
        request: Request,
        submission: SceneSubmission,
        upload_id: str = PathParam(min_length=1, max_length=64),
    ) -> Dict[str, Any]:
        """Start (or return) the inspect job that turns this upload into a scene."""
        return request.app.state.jobs.submit_scene(submission.to_request(upload_id), correlation_id(request))

    @app.post(f"{PREFIX}/jobs", status_code=202, tags=["jobs"])
    def submit_job(request: Request, submission: SliceSubmission) -> Dict[str, Any]:
        return request.app.state.jobs.submit(submission.to_request(), correlation_id(request))

    @app.get(f"{PREFIX}/jobs", tags=["jobs"])
    def list_jobs(request: Request) -> Dict[str, Any]:
        return {"jobs": request.app.state.jobs.list()}

    @app.get(f"{PREFIX}/jobs/{{job_id}}", tags=["jobs"])
    def read_job(request: Request, job_id: str = Depends(_job_id)) -> Dict[str, Any]:
        return request.app.state.jobs.get(job_id)

    @app.post(f"{PREFIX}/jobs/{{job_id}}/cancel", tags=["jobs"])
    def cancel_job(request: Request, job_id: str = Depends(_job_id)) -> Dict[str, Any]:
        return request.app.state.jobs.cancel(job_id)

    @app.post(f"{PREFIX}/jobs/{{job_id}}/retry", status_code=202, tags=["jobs"])
    def retry_job(request: Request, job_id: str = Depends(_job_id)) -> Dict[str, Any]:
        return request.app.state.jobs.retry(job_id, correlation_id(request))

    @app.get(f"{PREFIX}/jobs/{{job_id}}/artifacts/{{name}}", tags=["jobs"])
    def download_artifact(
        request: Request,
        job_id: str = Depends(_job_id),
        name: str = PathParam(pattern=f"^({'|'.join(ARTIFACT_NAMES)})$"),
    ) -> Response:
        path, media_type, filename = request.app.state.jobs.artifact(job_id, name)
        return FileResponse(path, media_type=media_type, filename=filename)

    @app.get(f"{PREFIX}/jobs/{{job_id}}/preview", tags=["preview"])
    def read_preview(request: Request, job_id: str = Depends(_job_id)) -> Dict[str, Any]:
        """The layer index: z heights, roles, tools, and each layer's byte range."""
        return request.app.state.jobs.preview(job_id)

    @app.get(f"{PREFIX}/jobs/{{job_id}}/preview/layers/{{layer}}", tags=["preview"])
    def read_preview_layer(
        request: Request,
        job_id: str = Depends(_job_id),
        layer: int = PathParam(ge=0, le=MAX_PREVIEW_LAYER),
    ) -> Response:
        """One layer's toolpaths, read from the blob by seeking to its range.

        The whole preview is never loaded: the response streams the layer's
        bytes straight off disk, which is what keeps a large print servable.
        """
        path, offset, length = request.app.state.jobs.preview_layer(job_id, layer)
        return StreamingResponse(
            _read_range(path, offset, length),
            media_type="application/octet-stream",
            headers={"Content-Length": str(length), "Cache-Control": "no-store"},
        )

    @app.get(f"{PREFIX}/scenes/{{job_id}}", tags=["scenes"])
    def read_scene(request: Request, job_id: str = Depends(_job_id)) -> Dict[str, Any]:
        """The scene index: the bed shape and each object's geometry range."""
        return request.app.state.jobs.scene(job_id)

    @app.get(f"{PREFIX}/scenes/{{job_id}}/objects/{{index}}", tags=["scenes"])
    def read_scene_object(
        request: Request,
        job_id: str = Depends(_job_id),
        index: int = PathParam(ge=0, le=MAX_SCENE_OBJECT_INDEX),
    ) -> Response:
        """One object's triangle soup, read from the blob by seeking to its range.

        The whole scene is never loaded: the response streams the object's
        bytes straight off disk, the same way a preview layer is served.
        """
        path, offset, length = request.app.state.jobs.scene_object(job_id, index)
        return StreamingResponse(
            _read_range(path, offset, length),
            media_type="application/octet-stream",
            headers={"Content-Length": str(length), "Cache-Control": "no-store"},
        )

    # Mounted last so every API route is matched before the catch-all, and only
    # when a build exists; `html=True` serves index.html for unknown paths.
    if resolved.frontend_dist is not None:
        app.mount(
            "/", StaticFiles(directory=resolved.frontend_dist, html=True), name="frontend"
        )

    return app
