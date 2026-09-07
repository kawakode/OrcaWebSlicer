"""The HTTP surface: schema validation, correlation IDs, and nothing CPU-bound.

This process never links `libslic3r` and never parses model geometry. Uploaded
bytes are streamed to disk and only ever read by a disposable worker process.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from web_job_directory import JOB_ID_PATTERN
from web_profile_catalog import KINDS, ProfileCatalogError, load_catalog

from . import API_VERSION, PROTOCOL_VERSION
from .config import ApiConfig, from_environment
from .errors import ApiError, from_catalog_error
from .jobs import ARTIFACT_NAMES, MAX_SETTINGS, JobService, SliceRequest
from .uploads import UploadStore


CORRELATION_HEADER = "X-Correlation-Id"
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PREFIX = f"/api/{API_VERSION}"

logger = logging.getLogger("orca.web.api")


class SliceSubmission(BaseModel):
    """One slice request. Unknown fields are refused rather than ignored."""

    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(min_length=1, max_length=64)
    machine_profile: str = Field(min_length=1, max_length=512)
    process_profile: str = Field(min_length=1, max_length=512)
    filament_profile: str = Field(min_length=1, max_length=512)
    settings: Dict[str, str] = Field(default_factory=dict, max_length=MAX_SETTINGS)
    plate_index: int = Field(default=1, ge=1)

    def to_request(self) -> SliceRequest:
        return SliceRequest(
            upload_id=self.upload_id,
            machine_profile=self.machine_profile,
            process_profile=self.process_profile,
            filament_profile=self.filament_profile,
            settings=dict(self.settings),
            plate_index=self.plate_index,
        )


def correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "")


def _job_id(job_id: str = PathParam(max_length=128)) -> str:
    if not JOB_ID_PATTERN.match(job_id):
        raise ApiError("unknown_job", "No job is known under that id.", 404)
    return job_id


def create_app(config: Optional[ApiConfig] = None) -> FastAPI:
    resolved = config or from_environment()
    resolved.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        catalog = load_catalog(resolved.repo_root, resolved.profile_vendors)
        uploads = UploadStore(resolved.uploads_root, resolved.job_limits.max_input_bytes)
        service = JobService(resolved, catalog, uploads)
        app.state.config = resolved
        app.state.catalog = catalog
        app.state.uploads = uploads
        app.state.jobs = service
        # Reclaim whatever an earlier process left behind before serving.
        service.sweep()
        logger.info("api ready profiles=%d state_root=%s", len(catalog), resolved.state_root)
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

    # Mounted last so every API route is matched before the catch-all, and only
    # when a build exists; `html=True` serves index.html for unknown paths.
    if resolved.frontend_dist is not None:
        app.mount(
            "/", StaticFiles(directory=resolved.frontend_dist, html=True), name="frontend"
        )

    return app
