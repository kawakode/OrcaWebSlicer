"""The HTTP surface: schema validation, correlation IDs, and nothing CPU-bound.

This process never links `libslic3r` and never parses model geometry. Uploaded
bytes are streamed to disk and only ever read by a disposable worker process.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from web_job_directory import JOB_ID_PATTERN
from web_profile_catalog import KINDS, ProfileCatalogError, load_catalog
from web_settings_catalog import SettingsCatalogError, evaluate_compatibility, load_settings_catalog

from . import API_VERSION, PROTOCOL_VERSION
from .abuse import MULTIPART_OVERHEAD_BYTES, AbuseGuard, RateLimiter
from .auth import AUTH_MODE_DISABLED, AuthConfigurationError, LOCAL_DEVELOPMENT_OWNER_ID, Principal, create_authenticator
from .config import ApiConfig, from_environment
from .errors import ApiError, error_response, from_catalog_error
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
# The only `/api/v1` routes reachable without a principal.
PUBLIC_PATHS = (f"{PREFIX}/health", f"{PREFIX}/health/live", f"{PREFIX}/health/ready")
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


def get_principal(request: Request) -> Principal:
    """The one dependency every protected route requires.

    Attached to the `protected` router below rather than to individual routes,
    so a route added to it can never accidentally skip authentication. The
    principal itself is authenticated by `AbuseGuard` before the request body
    is read; a request that guard did not authenticate fails closed here.
    """
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise ApiError("authentication_required", "A valid bearer assertion is required.", 401)
    return principal


def require_admission(request: Request):
    """Hold a lifecycle lease until one mutating HTTP request has returned."""
    with request.app.state.jobs.admission():
        yield


def _worker_available(command: str) -> bool:
    """Check the configured executable without launching a worker."""
    candidate = Path(command)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def _state_root_writable(root: Path) -> bool:
    """Probe the state mount that must accept uploads and job directories."""
    probe = root / f".readiness-{uuid.uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as writer:
            writer.write(b"1")
            writer.flush()
            os.fsync(writer.fileno())
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


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
    try:
        authenticate = create_authenticator(
            resolved.auth_mode, resolved.auth_jwks_path, resolved.auth_issuer, resolved.auth_audience
        )
    except AuthConfigurationError as error:
        raise ApiError("invalid_api_configuration", str(error), 500) from error

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        catalog = load_catalog(resolved.repo_root, resolved.profile_vendors)
        settings = _load_engine_metadata(resolved, catalog)
        # Disabled mode has exactly one principal, so a pre-auth upload is
        # unambiguously its own; required mode never guesses an owner.
        legacy_owner_id = LOCAL_DEVELOPMENT_OWNER_ID if resolved.auth_mode == AUTH_MODE_DISABLED else None
        uploads = UploadStore(resolved.uploads_root, resolved.job_limits.max_input_bytes, legacy_owner_id)
        service = JobService(resolved, catalog, uploads, settings_catalog=settings)
        app.state.config = resolved
        app.state.catalog = catalog
        app.state.settings = settings
        app.state.uploads = uploads
        app.state.jobs = service
        # Reclaim whatever an earlier process left behind before serving, then
        # keep enforcing retention even while no request arrives.
        service.sweep()
        service.start_sweeper()
        logger.info(
            "api ready profiles=%d settings=%d state_root=%s",
            len(catalog),
            len(settings) if settings else 0,
            resolved.state_root,
        )
        try:
            yield
        finally:
            service.begin_shutdown()
            logger.info("api draining")
            service.shutdown()
            logger.info("api shutdown complete")

    # Required mode never publishes the generated schema or interactive docs:
    # an unauthenticated deployment detail is not something a production
    # topology should expose next to a route surface that all requires a
    # bearer assertion.
    docs_enabled = resolved.auth_mode == AUTH_MODE_DISABLED
    app = FastAPI(
        title="OrcaWebSlicer API",
        version=API_VERSION,
        summary="Browser-facing slicing API backed by isolated native workers.",
        lifespan=lifespan,
        openapi_url="/openapi.json" if docs_enabled else None,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
    )
    # Added before the correlation middleware, so it runs inside it and its
    # refusals carry the request's correlation ID like any other error.
    app.add_middleware(
        AbuseGuard,
        prefix=PREFIX,
        public_paths=PUBLIC_PATHS,
        upload_path=f"{PREFIX}/uploads",
        max_upload_body_bytes=resolved.job_limits.max_input_bytes + MULTIPART_OVERHEAD_BYTES,
        max_json_body_bytes=resolved.rate_limits.max_json_body_bytes,
        authenticate=authenticate,
        limiter=RateLimiter(resolved.rate_limits),
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
        return error_response(error, identifier)

    if not docs_enabled:
        # The built frontend is mounted as a catch-all below. Reserve these
        # paths explicitly so required mode returns a real 404 instead of the
        # public SPA shell after FastAPI's generated docs are disabled.
        @app.get("/openapi.json", include_in_schema=False)
        @app.get("/docs", include_in_schema=False)
        @app.get("/redoc", include_in_schema=False)
        def hidden_api_documentation() -> Response:
            return Response(status_code=404)

    @app.get(f"{PREFIX}/health", tags=["service"])
    def health() -> Dict[str, Any]:
        return {"status": "ok", "protocol_version": PROTOCOL_VERSION, "api_version": API_VERSION}

    @app.get(f"{PREFIX}/health/live", tags=["service"])
    def liveness() -> Dict[str, Any]:
        """Process-only probe; dependency failures belong to readiness."""
        return {"status": "ok", "protocol_version": PROTOCOL_VERSION, "api_version": API_VERSION}

    @app.get(f"{PREFIX}/health/ready", tags=["service"])
    def readiness(request: Request, response: Response) -> Dict[str, Any]:
        """Deployment gate for admission, worker availability, and state storage."""
        checks = {
            "admission": request.app.state.jobs.is_accepting(),
            "worker": _worker_available(resolved.worker_command[0]),
            "state": _state_root_writable(resolved.state_root),
        }
        ready = all(checks.values())
        if not ready:
            response.status_code = 503
        return {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "protocol_version": PROTOCOL_VERSION,
            "api_version": API_VERSION,
            # Public and truthful, so an operator can never mistake a
            # `disabled` topology for an authenticated deployment.
            "auth_mode": resolved.auth_mode,
        }

    # Every route below requires a principal; this dependency is the one
    # centralized boundary that guarantees it, so a new route added to this
    # router can never accidentally ship unauthenticated. Health stays above,
    # registered directly on `app`, and static frontend assets are mounted
    # after this router, so neither passes through it.
    protected = APIRouter(dependencies=[Depends(get_principal)])

    @protected.get(f"{PREFIX}/profiles", tags=["profiles"])
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

    @protected.get(f"{PREFIX}/settings", tags=["profiles"])
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

    @protected.get(f"{PREFIX}/quota", tags=["service"])
    def read_quota(request: Request, principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
        """The caller's own quota limits and current usage."""
        return request.app.state.jobs.quota(principal.owner_id)

    @protected.post(f"{PREFIX}/uploads", status_code=201, tags=["uploads"])
    def create_upload(
        request: Request,
        file: UploadFile = File(),
        principal: Principal = Depends(get_principal),
        _admission: None = Depends(require_admission),
    ) -> Dict[str, Any]:
        """Store one model. The bytes are never parsed in this process."""
        record = request.app.state.jobs.create_upload(file.filename, file.file, principal.owner_id)
        logger.info(
            "upload stored upload_id=%s format=%s bytes=%d correlation_id=%s",
            record.upload_id,
            record.model_format,
            record.size_bytes,
            correlation_id(request),
        )
        return record.describe()

    @protected.post(f"{PREFIX}/uploads/{{upload_id}}/scene", status_code=202, tags=["scenes"])
    def submit_scene(
        request: Request,
        submission: SceneSubmission,
        upload_id: str = PathParam(min_length=1, max_length=64),
        principal: Principal = Depends(get_principal),
        _admission: None = Depends(require_admission),
    ) -> Dict[str, Any]:
        """Start (or return) the inspect job that turns this upload into a scene."""
        return request.app.state.jobs.submit_scene(
            submission.to_request(upload_id), correlation_id(request), principal.owner_id
        )

    @protected.post(f"{PREFIX}/jobs", status_code=202, tags=["jobs"])
    def submit_job(
        request: Request,
        submission: SliceSubmission,
        principal: Principal = Depends(get_principal),
        _admission: None = Depends(require_admission),
    ) -> Dict[str, Any]:
        return request.app.state.jobs.submit(submission.to_request(), correlation_id(request), principal.owner_id)

    @protected.get(f"{PREFIX}/jobs", tags=["jobs"])
    def list_jobs(request: Request, principal: Principal = Depends(get_principal)) -> Dict[str, Any]:
        return {"jobs": request.app.state.jobs.list(principal.owner_id)}

    @protected.get(f"{PREFIX}/jobs/{{job_id}}", tags=["jobs"])
    def read_job(
        request: Request, job_id: str = Depends(_job_id), principal: Principal = Depends(get_principal)
    ) -> Dict[str, Any]:
        return request.app.state.jobs.get(job_id, principal.owner_id)

    @protected.post(f"{PREFIX}/jobs/{{job_id}}/cancel", tags=["jobs"])
    def cancel_job(
        request: Request, job_id: str = Depends(_job_id), principal: Principal = Depends(get_principal)
    ) -> Dict[str, Any]:
        return request.app.state.jobs.cancel(job_id, principal.owner_id)

    @protected.post(f"{PREFIX}/jobs/{{job_id}}/retry", status_code=202, tags=["jobs"])
    def retry_job(
        request: Request,
        job_id: str = Depends(_job_id),
        principal: Principal = Depends(get_principal),
        _admission: None = Depends(require_admission),
    ) -> Dict[str, Any]:
        return request.app.state.jobs.retry(job_id, correlation_id(request), principal.owner_id)

    @protected.get(f"{PREFIX}/jobs/{{job_id}}/artifacts/{{name}}", tags=["jobs"])
    def download_artifact(
        request: Request,
        job_id: str = Depends(_job_id),
        name: str = PathParam(pattern=f"^({'|'.join(ARTIFACT_NAMES)})$"),
        principal: Principal = Depends(get_principal),
    ) -> Response:
        path, media_type, filename = request.app.state.jobs.artifact(job_id, name, principal.owner_id)
        return FileResponse(path, media_type=media_type, filename=filename)

    @protected.get(f"{PREFIX}/jobs/{{job_id}}/preview", tags=["preview"])
    def read_preview(
        request: Request, job_id: str = Depends(_job_id), principal: Principal = Depends(get_principal)
    ) -> Dict[str, Any]:
        """The layer index: z heights, roles, tools, and each layer's byte range."""
        return request.app.state.jobs.preview(job_id, principal.owner_id)

    @protected.get(f"{PREFIX}/jobs/{{job_id}}/preview/layers/{{layer}}", tags=["preview"])
    def read_preview_layer(
        request: Request,
        job_id: str = Depends(_job_id),
        layer: int = PathParam(ge=0, le=MAX_PREVIEW_LAYER),
        principal: Principal = Depends(get_principal),
    ) -> Response:
        """One layer's toolpaths, read from the blob by seeking to its range.

        The whole preview is never loaded: the response streams the layer's
        bytes straight off disk, which is what keeps a large print servable.
        """
        path, offset, length = request.app.state.jobs.preview_layer(job_id, layer, principal.owner_id)
        return StreamingResponse(
            _read_range(path, offset, length),
            media_type="application/octet-stream",
            headers={"Content-Length": str(length), "Cache-Control": "no-store"},
        )

    @protected.get(f"{PREFIX}/scenes/{{job_id}}", tags=["scenes"])
    def read_scene(
        request: Request, job_id: str = Depends(_job_id), principal: Principal = Depends(get_principal)
    ) -> Dict[str, Any]:
        """The scene index: the bed shape and each object's geometry range."""
        return request.app.state.jobs.scene(job_id, principal.owner_id)

    @protected.get(f"{PREFIX}/scenes/{{job_id}}/objects/{{index}}", tags=["scenes"])
    def read_scene_object(
        request: Request,
        job_id: str = Depends(_job_id),
        index: int = PathParam(ge=0, le=MAX_SCENE_OBJECT_INDEX),
        principal: Principal = Depends(get_principal),
    ) -> Response:
        """One object's triangle soup, read from the blob by seeking to its range.

        The whole scene is never loaded: the response streams the object's
        bytes straight off disk, the same way a preview layer is served.
        """
        path, offset, length = request.app.state.jobs.scene_object(job_id, index, principal.owner_id)
        return StreamingResponse(
            _read_range(path, offset, length),
            media_type="application/octet-stream",
            headers={"Content-Length": str(length), "Cache-Control": "no-store"},
        )

    app.include_router(protected)

    # Mounted last so every API route is matched before the catch-all, and only
    # when a build exists; `html=True` serves index.html for unknown paths.
    # Static assets stay outside `protected`: they are the unauthenticated
    # browser shell, not `/api/v1` data.
    if resolved.frontend_dist is not None:
        app.mount(
            "/", StaticFiles(directory=resolved.frontend_dist, html=True), name="frontend"
        )

    return app
