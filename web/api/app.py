"""The HTTP surface: schema validation, correlation IDs, and nothing CPU-bound.

This process never links `libslic3r` and never parses model geometry. Uploaded
bytes are streamed to disk and only ever read by a disposable worker process.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterator, List, Literal, Optional

from fastapi import Depends, FastAPI, File, Path as PathParam, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from web_job_directory import JOB_ID_PATTERN
from web_profile_catalog import KINDS, ProfileCatalogError, load_catalog
from web_settings_catalog import SettingsCatalogError, evaluate_compatibility, load_settings_catalog

from . import API_VERSION, PROTOCOL_VERSION
from .config import ApiConfig, from_environment
from .edits import EditSpec
from .errors import ApiError, from_catalog_error, from_settings_error
from .jobs import (
    ARTIFACT_NAMES,
    MAX_OBJECT_INSTANCES,
    MAX_LAYER_RANGES,
    MAX_PLATE_OBJECTS,
    MAX_SETTINGS,
    PREVIEW_CHUNK_BYTES,
    JobService,
    LayerRange,
    PlacedObject,
    SliceRequest,
)
from .uploads import UploadStore


CORRELATION_HEADER = "X-Correlation-Id"
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PREFIX = f"/api/{API_VERSION}"

logger = logging.getLogger("orca.web.api")


class InstanceSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Row-major 4x4 affine matrix in millimetres.
    transform: List[float] = Field(min_length=16, max_length=16)


class LayerRangeSubmission(BaseModel):
    """Settings for part of an object's height, measured from its bottom."""

    model_config = ConfigDict(extra="forbid")

    min_z: float = Field(ge=0, allow_inf_nan=False)
    max_z: float = Field(gt=0, allow_inf_nan=False)
    settings: Dict[str, str] = Field(min_length=1, max_length=MAX_SETTINGS)


class ObjectSubmission(BaseModel):
    """One object of an upload, placed by the plater, with its own overrides."""

    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(min_length=1, max_length=64)
    object: int = Field(default=0, ge=0, lt=MAX_PLATE_OBJECTS)
    instances: List[InstanceSubmission] = Field(min_length=1, max_length=MAX_OBJECT_INSTANCES)
    settings: Dict[str, str] = Field(default_factory=dict, max_length=MAX_SETTINGS)
    layer_ranges: List[LayerRangeSubmission] = Field(default_factory=list, max_length=MAX_LAYER_RANGES)


class PlateSubmission(BaseModel):
    """One plate with its profiles. Unknown fields are refused rather than ignored.

    `upload_id` names one model as the worker arranges it; `objects` names a
    plate exactly as the plater placed it. Exactly one of the two is given.
    """

    model_config = ConfigDict(extra="forbid")

    upload_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    objects: Optional[List[ObjectSubmission]] = Field(default=None, min_length=1, max_length=MAX_PLATE_OBJECTS)
    machine_profile: str = Field(min_length=1, max_length=512)
    process_profile: str = Field(min_length=1, max_length=512)
    filament_profile: str = Field(min_length=1, max_length=512)
    settings: Dict[str, str] = Field(default_factory=dict, max_length=MAX_SETTINGS)
    plate_index: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def names_one_plate(self) -> "PlateSubmission":
        if (self.upload_id is None) == (self.objects is None):
            raise ValueError("give either upload_id or objects, not both")
        return self

    def to_request(self, operation: str = "slice", **options: Any) -> SliceRequest:
        return SliceRequest(
            operation=operation,
            upload_id=self.upload_id,
            machine_profile=self.machine_profile,
            process_profile=self.process_profile,
            filament_profile=self.filament_profile,
            settings=dict(self.settings),
            plate_index=self.plate_index,
            objects=tuple(
                PlacedObject(
                    upload_id=placed.upload_id,
                    object_index=placed.object,
                    instances=tuple(tuple(instance.transform) for instance in placed.instances),
                    settings=dict(placed.settings),
                    layer_ranges=tuple(
                        LayerRange(layer_range.min_z, layer_range.max_z, dict(layer_range.settings))
                        for layer_range in placed.layer_ranges
                    ),
                )
                for placed in self.objects or ()
            ),
            **options,
        )


class SliceSubmission(PlateSubmission):
    """One slice request: a plate, plus whether the G-code embeds thumbnails."""

    thumbnails: bool = False

    def to_request(self, operation: str = "slice", **options: Any) -> SliceRequest:
        return super().to_request(operation, thumbnails=self.thumbnails, **options)


class ProjectSubmission(PlateSubmission):
    """One plate to save as a desktop-compatible 3MF project instead of slicing."""


class CutSubmission(BaseModel):
    """A planar cut: a point on the plane and the normal pointing at the upper part, in plate millimetres."""

    model_config = ConfigDict(extra="forbid")

    point: List[float] = Field(min_length=3, max_length=3)
    normal: List[float] = Field(min_length=3, max_length=3)
    keep: Literal["both", "upper", "lower"] = "both"
    flip_upper: bool = False
    place_on_cut: bool = False


class EditSubmission(BaseModel):
    """One object tool applied to one object of an upload, where the plater placed it."""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["cut", "repair"]
    object: int = Field(default=0, ge=0, lt=MAX_PLATE_OBJECTS)
    # Row-major 4x4 affine matrix in millimetres: the object's placement.
    transform: List[float] = Field(min_length=16, max_length=16)
    cut: Optional[CutSubmission] = None

    @model_validator(mode="after")
    def cut_has_a_plane(self) -> "EditSubmission":
        if (self.tool == "cut") != (self.cut is not None):
            raise ValueError("a cut, and only a cut, names its plane")
        return self

    def to_spec(self) -> EditSpec:
        spec = EditSpec(tool=self.tool, transform=tuple(self.transform), object_index=self.object)
        if self.cut is None:
            return spec
        return dataclasses.replace(
            spec,
            point=tuple(self.cut.point),
            normal=tuple(self.cut.normal),
            keep=self.cut.keep,
            flip_upper=self.cut.flip_upper,
            place_on_cut=self.cut.place_on_cut,
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
        # The engine owns setting metadata and the compatibility expression
        # language, so both are read from the worker once, before serving.
        try:
            settings = load_settings_catalog(resolved.worker_command)
            catalog = load_catalog(
                resolved.repo_root,
                resolved.profile_vendors,
                lambda request: evaluate_compatibility(resolved.worker_command, str(request)),
            )
        except SettingsCatalogError as error:
            raise from_settings_error(error) from error
        uploads = UploadStore(resolved.uploads_root, resolved.job_limits.max_input_bytes)
        service = JobService(resolved, catalog, uploads, settings)
        app.state.config = resolved
        app.state.catalog = catalog
        app.state.settings = settings
        app.state.uploads = uploads
        app.state.jobs = service
        # Reclaim whatever an earlier process left behind before serving.
        service.sweep()
        logger.info(
            "api ready profiles=%d settings=%d state_root=%s", len(catalog), len(settings), resolved.state_root
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
        described = {"code": error.code, "message": str(error)}
        if error.setting:
            described["setting"] = error.setting
        return JSONResponse(
            status_code=error.status,
            content={"error": described, "correlation_id": identifier},
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

    @app.get(f"{PREFIX}/profiles/effective", tags=["profiles"])
    def effective_profile(
        request: Request,
        machine: str = Query(max_length=512),
        process: str = Query(max_length=512),
        filament: str = Query(max_length=512),
    ) -> Dict[str, Any]:
        """Show what one selected chain gives every curated setting.

        The browser renders these as each control's profile default, so a blank
        override visibly means "keep the profile's value".
        """
        catalog = request.app.state.catalog
        try:
            selected = catalog.select(machine, process, filament)
            values = catalog.effective_values(selected, request.app.state.settings.scopes())
            machine_profile = catalog.resolve(selected["machine"])
        except ProfileCatalogError as error:
            raise from_catalog_error(error) from error
        return {
            "profiles": {kind: entry.describe() for kind, entry in selected.items()},
            "values": values,
            # The plater draws and validates against the same bed the worker
            # checks a submitted placement against.
            "bed": {
                "printable_area": machine_profile.get("printable_area", []),
                "printable_height": machine_profile.get("printable_height", ""),
            },
        }

    @app.get(f"{PREFIX}/settings", tags=["profiles"])
    def settings_catalog(request: Request) -> Dict[str, Any]:
        """The engine's own definition of every setting a request may override."""
        return request.app.state.settings.describe()

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
        # The display geometry is produced by a worker like any other job, so
        # the API still never parses the model it just stored.
        described = record.describe()
        described["inspection"] = request.app.state.jobs.inspect(record.upload_id, correlation_id(request))
        return described

    @app.post(f"{PREFIX}/uploads/{{upload_id}}/edits", status_code=202, tags=["uploads"])
    def edit_upload(
        request: Request, submission: EditSubmission, upload_id: str = PathParam(max_length=64)
    ) -> Dict[str, Any]:
        """Cut or repair one object. Each resulting model becomes a new, inspected upload."""
        return request.app.state.jobs.edit(upload_id, submission.to_spec(), correlation_id(request))

    @app.post(f"{PREFIX}/jobs", status_code=202, tags=["jobs"])
    def submit_job(request: Request, submission: SliceSubmission) -> Dict[str, Any]:
        return request.app.state.jobs.submit(submission.to_request(), correlation_id(request))

    @app.post(f"{PREFIX}/projects", status_code=202, tags=["jobs"])
    def export_project(request: Request, submission: ProjectSubmission) -> Dict[str, Any]:
        """Save a plate as a 3MF project through a worker; the job publishes `project`."""
        return request.app.state.jobs.submit(submission.to_request("export_project"), correlation_id(request))

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

    @app.get(f"{PREFIX}/jobs/{{job_id}}/preview/layers", tags=["jobs"])
    def preview_layers(
        request: Request,
        job_id: str = Depends(_job_id),
        start: int = Query(ge=0),
        end: int = Query(ge=0),
    ) -> Response:
        """Stream the preview segments of layers `start` to `end`, inclusive."""
        path, offset, length = request.app.state.jobs.preview_page(job_id, start, end)

        def chunks() -> Iterator[bytes]:
            with path.open("rb") as stream:
                stream.seek(offset)
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(PREVIEW_CHUNK_BYTES, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            chunks(), media_type="application/octet-stream", headers={"Content-Length": str(length)}
        )

    # Mounted last so every API route is matched before the catch-all, and only
    # when a build exists; `html=True` serves index.html for unknown paths.
    if resolved.frontend_dist is not None:
        app.mount(
            "/", StaticFiles(directory=resolved.frontend_dist, html=True), name="frontend"
        )

    return app
