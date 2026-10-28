"""Drive one disposable worker per slice request and hold the resulting job state."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from web_job_directory import (
    MANIFEST_NAME,
    JobDirectoryError,
    JobInput,
    prepare_job_directory,
    remove_job_directory,
    sweep_job_directories,
)
from web_profile_catalog import ProfileCatalog, ProfileCatalogError
from web_settings_catalog import SettingsCatalog, SettingsCatalogError
from web_worker_executor import ExecutorError, execute_worker, resolve_artifact_path

from . import PROTOCOL_VERSION
from .config import ApiConfig
from .errors import ApiError, from_catalog_error, from_job_directory_error, from_settings_error
from .uploads import UploadStore


MAX_SETTINGS = 256
MAX_SETTING_VALUE_CHARS = 4096
MAX_LAYER_RANGES = 32
MAX_PLATE_INDEX = 64
# Mirrors the worker's own plate bounds so an oversized plate is refused here.
MAX_PLATE_OBJECTS = 256
MAX_OBJECT_INSTANCES = 64
MAX_PLATE_UPLOADS = 16
# One preview page is read in chunks, never held whole.
MAX_PREVIEW_PAGE_BYTES = 256 * 1024 * 1024
PREVIEW_CHUNK_BYTES = 1024 * 1024
SETTING_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
OUTPUT_GCODE = "output/model.gcode"
OUTPUT_PREVIEW = "output/preview.bin"
OUTPUT_PREVIEW_INDEX = "output/preview.json"
OUTPUT_MESH = "output/mesh.bin"
OUTPUT_INSPECTION = "output/inspection.json"
# Artifacts are downloaded by name, never by the worker's own relative path.
# A worker artifact's kind is its public name.
ARTIFACT_MEDIA_TYPES = {
    "gcode": ("text/x.gcode", ".gcode"),
    "preview": ("application/octet-stream", "-preview.bin"),
    "preview_index": ("application/json", "-preview.json"),
    "mesh": ("application/octet-stream", "-mesh.bin"),
    "inspection": ("application/json", "-inspection.json"),
}
ARTIFACT_NAMES = ("result",) + tuple(ARTIFACT_MEDIA_TYPES)

# An executor status that is not "completed" never carries a worker result, so
# the API states the failure in the worker's own error vocabulary instead.
_EXECUTOR_FAILURES = {
    "canceled": ("cancellation", "job_canceled"),
    "timed_out": ("resource_limit", "wall_time_limit_exceeded"),
    "output_limit": ("resource_limit", None),
    "failed": ("internal", None),
}


@dataclasses.dataclass(frozen=True)
class LayerRange:
    """Settings for one object between two heights above its bottom, in mm."""

    min_z: float
    max_z: float
    settings: Dict[str, str] = dataclasses.field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {"min_z": self.min_z, "max_z": self.max_z, "settings": dict(self.settings)}


@dataclasses.dataclass(frozen=True)
class PlacedObject:
    """One object of an upload, with the instance transforms the plater chose.

    Each transform is a row-major 4x4 matrix in millimetres that replaces the
    placement the file declared. `settings` and `layer_ranges` override the
    plate's settings for this object only.
    """

    upload_id: str
    object_index: int = 0
    instances: Tuple[Tuple[float, ...], ...] = ()
    settings: Dict[str, str] = dataclasses.field(default_factory=dict)
    layer_ranges: Tuple[LayerRange, ...] = ()

    def describe(self) -> Dict[str, Any]:
        described: Dict[str, Any] = {
            "upload_id": self.upload_id,
            "object": self.object_index,
            "instances": [{"transform": list(transform)} for transform in self.instances],
        }
        # Only stated when used, so a plate without overrides is described
        # and staged exactly as before.
        if self.settings:
            described["settings"] = dict(self.settings)
        if self.layer_ranges:
            described["layer_ranges"] = [layer_range.describe() for layer_range in self.layer_ranges]
        return described


@dataclasses.dataclass(frozen=True)
class SliceRequest:
    """The immutable inputs of one job, reused verbatim by a retry.

    A slice names either one `upload_id`, which the worker arranges, or the
    placed `objects` of a plate. An inspection names one upload and nothing
    else.
    """

    upload_id: Optional[str] = None
    machine_profile: str = ""
    process_profile: str = ""
    filament_profile: str = ""
    settings: Dict[str, str] = dataclasses.field(default_factory=dict)
    plate_index: int = 1
    objects: Tuple[PlacedObject, ...] = ()
    operation: str = "slice"

    def describe(self) -> Dict[str, Any]:
        described: Dict[str, Any] = {"operation": self.operation, "upload_id": self.upload_id}
        if self.operation == "slice":
            described.update(
                {
                    "machine_profile": self.machine_profile,
                    "process_profile": self.process_profile,
                    "filament_profile": self.filament_profile,
                    "settings": dict(self.settings),
                    "plate_index": self.plate_index,
                }
            )
            if self.objects:
                described["objects"] = [placed.describe() for placed in self.objects]
        return described

    def upload_ids(self) -> List[str]:
        """The uploads this job stages, each once, in first-use order."""
        if not self.objects:
            return [self.upload_id] if self.upload_id else []
        ordered: List[str] = []
        for placed in self.objects:
            if placed.upload_id not in ordered:
                ordered.append(placed.upload_id)
        return ordered


@dataclasses.dataclass
class JobRecord:
    job_id: str
    correlation_id: str
    request: SliceRequest
    path: Path
    created_at: float
    state: str = "queued"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress: Dict[str, Any] = dataclasses.field(
        default_factory=lambda: {"stage": "queued", "percent": 0, "message": ""}
    )
    warnings: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    error: Optional[Dict[str, str]] = None
    result: Optional[Dict[str, Any]] = None
    retry_of: Optional[str] = None
    # The effective profile chain this job was resolved from, kept so a report
    # names every inherited profile and not only the selected leaf.
    profiles: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # The parsed preview index, read once on the first page request.
    preview_index: Optional[Dict[str, Any]] = None
    cancellation: threading.Event = dataclasses.field(default_factory=threading.Event)

    def describe(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "operation": self.request.operation,
            "correlation_id": self.correlation_id,
            "state": self.state,
            "progress": dict(self.progress),
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error else None,
            "artifacts": self._describe_artifacts(),
            "request": self.request.describe(),
            "profiles": dict(self.profiles),
            "retry_of": self.retry_of,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timing": (self.result or {}).get("timing"),
        }

    def _describe_artifacts(self) -> List[Dict[str, Any]]:
        """Name the artifacts that are downloadable now.

        The worker's own relative paths stay internal: a client asks for an
        artifact by name and the API resolves the path it validated.
        """
        if self.result is None:
            return []
        described = [{"name": "result", "media_type": "application/json"}]
        for artifact in self.result.get("artifacts", []):
            kind = artifact.get("kind") if isinstance(artifact, dict) else None
            if kind in ARTIFACT_MEDIA_TYPES:
                described.append(
                    {
                        "name": kind,
                        "media_type": ARTIFACT_MEDIA_TYPES[kind][0],
                        "size_bytes": artifact.get("size_bytes"),
                        "sha256": artifact.get("sha256"),
                    }
                )
        return described


class JobService:
    """Owns job state, the worker pool, and the disposable job directories."""

    def __init__(
        self,
        config: ApiConfig,
        catalog: ProfileCatalog,
        uploads: UploadStore,
        settings: Optional[SettingsCatalog] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        # Without a catalog only the shape of an override is checked and the
        # worker remains the sole judge of its value.
        self._settings = settings
        self._uploads = uploads
        self._log = logger or logging.getLogger("orca.web.api.jobs")
        self._lock = threading.RLock()
        self._jobs: Dict[str, JobRecord] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=config.max_concurrent_jobs, thread_name_prefix="orca-slice-job"
        )

    # Queries

    def get(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._require(job_id).describe()

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = sorted(self._jobs.values(), key=lambda record: record.created_at)
            return [record.describe() for record in records]

    def artifact(self, job_id: str, name: str) -> Tuple[Path, str, str]:
        """Resolve a downloadable artifact to a path, media type, and filename."""
        if name not in ARTIFACT_NAMES:
            raise ApiError("unknown_artifact", f"No artifact is named {name}.", 404)
        with self._lock:
            record = self._require(job_id)
            if name == "result":
                # A failed run's report is the useful part, so any outcome the
                # executor validated is served.
                if record.result is None:
                    raise ApiError(
                        "artifact_unavailable", "This job published no validated result.", 409
                    )
                return self._readable(record.path / "result.json"), "application/json", f"{job_id}-result.json"

            path = self._artifact_path(record, name)
            media_type, suffix = ARTIFACT_MEDIA_TYPES[name]
            return self._readable(path), media_type, f"{job_id}{suffix}"

    def preview_page(self, job_id: str, start: int, end: int) -> Tuple[Path, int, int]:
        """Locate the bytes of preview layers `start` to `end`, inclusive.

        The index is validated against the segment file once, so a page is a
        single contiguous range that is streamed rather than loaded.
        """
        with self._lock:
            record = self._require(job_id)
            segments = self._readable(self._artifact_path(record, "preview"))
            if record.preview_index is None:
                record.preview_index = self._load_preview_index(record, segments.stat().st_size)
            layers = record.preview_index["layers"]
        if not 0 <= start <= end < len(layers):
            raise ApiError("invalid_preview_range", f"Layers must lie within 0 to {len(layers) - 1}.", 416)
        offset = layers[start]["offset"]
        length = layers[end]["offset"] + layers[end]["count"] * record.preview_index["segment_bytes"] - offset
        if length > MAX_PREVIEW_PAGE_BYTES:
            raise ApiError("preview_page_too_large", "Request fewer layers at a time.", 413)
        return segments, offset, length

    def _artifact_path(self, record: JobRecord, name: str) -> Path:
        if record.state == "succeeded" and record.result is not None:
            for artifact in record.result.get("artifacts", []):
                if not isinstance(artifact, dict) or artifact.get("kind") != name:
                    continue
                path = resolve_artifact_path(record.path, artifact.get("path"))
                if path is not None:
                    return path
        raise ApiError("artifact_unavailable", f"This job published no {name} artifact.", 409)

    def _load_preview_index(self, record: JobRecord, segment_file_bytes: int) -> Dict[str, Any]:
        path = self._readable(self._artifact_path(record, "preview_index"))
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
            segment_bytes = int(index["segment_bytes"])
            layers = [{"offset": int(layer["offset"]), "count": int(layer["count"])} for layer in index["layers"]]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ApiError("preview_unreadable", "The preview index could not be read.", 500) from error
        expected = 0
        for layer in layers:
            # Layers are contiguous and ordered, so every page is one range.
            if layer["offset"] != expected or layer["count"] < 0:
                raise ApiError("preview_unreadable", "The preview index does not match its segments.", 500)
            expected += layer["count"] * segment_bytes
        if segment_bytes <= 0 or expected != segment_file_bytes:
            raise ApiError("preview_unreadable", "The preview index does not match its segments.", 500)
        return {"segment_bytes": segment_bytes, "layers": layers}

    # Commands

    def submit(
        self, request: SliceRequest, correlation_id: str, retry_of: Optional[str] = None
    ) -> Dict[str, Any]:
        uploads = [self._uploads.get(upload_id) for upload_id in request.upload_ids()]
        self._validate(request, len(uploads))
        self.sweep()

        job_id = f"job-{uuid.uuid4().hex}"
        # A single legacy upload keeps its original staged name.
        models = {
            upload.upload_id: (
                f"input/model.{upload.model_format}"
                if not request.objects
                else f"input/model-{position}.{upload.model_format}"
            )
            for position, upload in enumerate(uploads)
        }
        inputs = [JobInput(models[upload.upload_id], upload.path, "model") for upload in uploads]
        selected: Dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="orca-profiles-") as staging:
            if request.operation == "slice":
                try:
                    selected = self._catalog.select(
                        request.machine_profile, request.process_profile, request.filament_profile
                    )
                    profiles = self._catalog.materialize(
                        request.machine_profile,
                        request.process_profile,
                        request.filament_profile,
                        Path(staging),
                    )
                except ProfileCatalogError as error:
                    raise from_catalog_error(error) from error
                inputs += [
                    JobInput(f"profiles/{kind}.json", path, "profile")
                    for kind, path in sorted(profiles.items())
                ]
            try:
                prepared = prepare_job_directory(
                    self._config.jobs_root,
                    job_id,
                    self._manifest(job_id, models, request),
                    inputs,
                    self._config.job_limits,
                )
            except JobDirectoryError as error:
                raise from_job_directory_error(error) from error

        record = JobRecord(
            job_id=job_id,
            correlation_id=correlation_id,
            request=request,
            path=prepared.path,
            created_at=time.time(),
            retry_of=retry_of,
            profiles={
                kind: {"profile_id": entry.profile_id, "inherits_chain": list(entry.inherits_chain)}
                for kind, entry in selected.items()
            },
        )
        with self._lock:
            self._jobs[job_id] = record
            # Snapshot before queueing: a worker can start before this returns,
            # and an acceptance must report the job as accepted.
            accepted = record.describe()
        self._log.info(
            "job accepted job_id=%s correlation_id=%s retry_of=%s staged_bytes=%d",
            job_id,
            correlation_id,
            retry_of,
            prepared.staged_bytes,
        )
        self._pool.submit(self._run, job_id)
        return accepted

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._require(job_id)
            if record.state in TERMINAL_STATES:
                raise ApiError("job_not_cancelable", "This job has already finished.", 409)
            record.cancellation.set()
            if record.state == "queued":
                # No worker exists yet, so the job is canceled here and _run
                # returns without launching one.
                self._finish(record, "canceled", self._error("cancellation", "job_canceled", "The job was canceled."))
            self._log.info("job cancellation requested job_id=%s state=%s", job_id, record.state)
            return record.describe()

    def retry(self, job_id: str, correlation_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._require(job_id)
            if record.state not in TERMINAL_STATES:
                raise ApiError("job_not_retryable", "This job has not finished yet.", 409)
            request = record.request
        return self.submit(request, correlation_id, retry_of=job_id)

    def sweep(self) -> Dict[str, Any]:
        """Reclaim expired jobs and uploads, and forget the jobs that went away."""
        with self._lock:
            active = [
                record.job_id for record in self._jobs.values() if record.state not in TERMINAL_STATES
            ]
        try:
            report = sweep_job_directories(self._config.jobs_root, self._config.job_limits, active)
        except JobDirectoryError as error:
            raise from_job_directory_error(error) from error
        with self._lock:
            for job_id in report.removed:
                self._jobs.pop(job_id, None)
        uploads = self._uploads.sweep(self._config.job_limits.retention_seconds)
        return {"jobs_removed": report.removed, "jobs_retained": report.retained, "uploads_removed": uploads}

    def shutdown(self) -> None:
        with self._lock:
            for record in self._jobs.values():
                if record.state not in TERMINAL_STATES:
                    record.cancellation.set()
        self._pool.shutdown(wait=True)

    # Execution

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            record.state = "running"
            record.started_at = time.time()
            record.progress = {"stage": "input", "percent": 0, "message": ""}
            manifest_path = record.path / MANIFEST_NAME

        try:
            execution = execute_worker(
                self._config.worker_command,
                manifest_path,
                self._config.executor_limits,
                record.cancellation,
                lambda event: self._observe(record, event),
            )
        except ExecutorError as error:
            self._log.warning("job executor failure job_id=%s code=%s", job_id, error.code)
            self._fail(record, "internal", error.code, str(error))
            return
        except Exception:
            # One job's failure must never take the API process down with it.
            self._log.exception("job execution raised job_id=%s", job_id)
            self._fail(record, "internal", "internal_error", "The job failed unexpectedly.")
            return

        self._conclude(record, execution)

    def _observe(self, record: JobRecord, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        with self._lock:
            if kind == "progress" and isinstance(event.get("progress"), dict):
                progress = event["progress"]
                record.progress = {
                    "stage": str(progress.get("stage", "")),
                    "percent": int(progress.get("percent", 0)),
                    "message": str(progress.get("message", "")),
                }
            elif kind == "warning" and isinstance(event.get("warning"), dict):
                record.warnings.append(dict(event["warning"]))

    def _conclude(self, record: JobRecord, execution: Any) -> None:
        if execution.status == "completed" and execution.result is not None:
            result = execution.result
            outcome = str(result.get("outcome"))
            error = result.get("error")
            with self._lock:
                record.result = result
                record.warnings = [item for item in result.get("warnings", []) if isinstance(item, dict)]
                self._finish(record, outcome, dict(error) if isinstance(error, dict) else None)
            self._log.info(
                "job finished job_id=%s outcome=%s correlation_id=%s",
                record.job_id,
                outcome,
                record.correlation_id,
            )
            return

        category, code = _EXECUTOR_FAILURES.get(execution.status, ("internal", None))
        code = code or execution.error_code or "worker_failed"
        state = "canceled" if category == "cancellation" else "failed"
        self._log.warning(
            "job terminated job_id=%s status=%s code=%s forced=%s",
            record.job_id,
            execution.status,
            code,
            execution.forced_termination,
        )
        # A forcibly terminated worker may have left files the executor never
        # validated, so nothing from this job is ever downloadable.
        remove_job_directory(record.path)
        with self._lock:
            self._finish(record, state, self._error(category, code, execution.error_message or "The job failed."))

    def _fail(self, record: JobRecord, category: str, code: str, message: str) -> None:
        remove_job_directory(record.path)
        with self._lock:
            self._finish(record, "failed", self._error(category, code, message))

    def _finish(self, record: JobRecord, state: str, error: Optional[Dict[str, Any]]) -> None:
        record.state = state
        record.error = error
        record.finished_at = time.time()
        if state == "succeeded":
            record.progress = {"stage": "finalize", "percent": 100, "message": ""}

    # Helpers

    @staticmethod
    def _error(category: str, code: str, message: str) -> Dict[str, str]:
        return {"category": category, "code": code, "message": message}

    def _require(self, job_id: str) -> JobRecord:
        record = self._jobs.get(job_id)
        if record is None:
            raise ApiError("unknown_job", "No job is known under that id.", 404)
        return record

    @staticmethod
    def _readable(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ApiError("artifact_unavailable", "The artifact is no longer available.", 410)
        return path

    def inspect(self, upload_id: str, correlation_id: str) -> Dict[str, Any]:
        """Export an upload's display geometry through a disposable worker."""
        return self.submit(SliceRequest(upload_id=upload_id, operation="inspect"), correlation_id)

    def _validate(self, request: SliceRequest, upload_count: int) -> None:
        if request.operation == "inspect":
            if upload_count != 1 or request.objects:
                raise ApiError("invalid_inspection", "An inspection names exactly one upload.")
            return
        if bool(request.upload_id) == bool(request.objects):
            raise ApiError("invalid_plate", "A slice names either upload_id or objects, not both.")
        if len(request.objects) > MAX_PLATE_OBJECTS or upload_count > MAX_PLATE_UPLOADS:
            raise ApiError(
                "invalid_plate",
                f"A plate holds at most {MAX_PLATE_OBJECTS} objects from {MAX_PLATE_UPLOADS} uploads.",
            )
        for placed in request.objects:
            if not 1 <= len(placed.instances) <= MAX_OBJECT_INSTANCES:
                raise ApiError("invalid_plate", f"Every object needs 1 to {MAX_OBJECT_INSTANCES} instances.")
            if any(len(transform) != 16 for transform in placed.instances):
                raise ApiError("invalid_instance_transform", "Every transform is a row-major 4x4 matrix.")
            self._validate_settings(placed.settings, "object")
            self._validate_layer_ranges(placed.layer_ranges)
        if not isinstance(request.plate_index, int) or not 1 <= request.plate_index <= MAX_PLATE_INDEX:
            raise ApiError("invalid_plate_index", "plate_index must be a 1-based plate number.")
        self._validate_settings(request.settings, "plate")

    def _validate_settings(self, settings: Dict[str, str], level: str) -> None:
        if len(settings) > MAX_SETTINGS:
            raise ApiError(
                "invalid_settings", f"A request may override at most {MAX_SETTINGS} settings."
            )
        for key, value in settings.items():
            if not SETTING_KEY_PATTERN.match(key):
                raise ApiError("invalid_settings", f"{key!r} is not a setting name.")
            if not isinstance(value, str) or len(value) > MAX_SETTING_VALUE_CHARS:
                raise ApiError(
                    "invalid_settings",
                    f"The value of {key} must be a string of at most {MAX_SETTING_VALUE_CHARS} characters.",
                )
        if self._settings is not None:
            try:
                self._settings.validate_overrides(settings, level)
            except SettingsCatalogError as error:
                raise from_settings_error(error) from error

    def _validate_layer_ranges(self, layer_ranges: Tuple[LayerRange, ...]) -> None:
        if len(layer_ranges) > MAX_LAYER_RANGES:
            raise ApiError("invalid_layer_ranges", f"An object holds at most {MAX_LAYER_RANGES} layer ranges.")
        ordered = sorted(layer_ranges, key=lambda layer_range: layer_range.min_z)
        for position, layer_range in enumerate(ordered):
            if not 0 <= layer_range.min_z < layer_range.max_z or not layer_range.settings:
                raise ApiError(
                    "invalid_layer_ranges", "A layer range needs 0 <= min_z < max_z and at least one setting."
                )
            if position and layer_range.min_z < ordered[position - 1].max_z:
                raise ApiError("invalid_layer_ranges", "Layer ranges of one object may not overlap.")
            self._validate_settings(layer_range.settings, "range")

    def _manifest(self, job_id: str, models: Dict[str, str], request: SliceRequest) -> Dict[str, Any]:
        if request.operation == "inspect":
            payload: Dict[str, Any] = {
                "input_model": models[request.upload_id or ""],
                "output_mesh": OUTPUT_MESH,
                "output_inspection": OUTPUT_INSPECTION,
            }
        else:
            payload = {
                "output_gcode": OUTPUT_GCODE,
                "output_preview": OUTPUT_PREVIEW,
                "output_preview_index": OUTPUT_PREVIEW_INDEX,
                "profiles": {
                    "machine": "profiles/machine.json",
                    "process": "profiles/process.json",
                    "filament": "profiles/filament.json",
                },
                "settings": dict(request.settings),
                "plate_index": request.plate_index,
            }
            if request.objects:
                payload["objects"] = []
                for placed in request.objects:
                    described = placed.describe()
                    del described["upload_id"]
                    payload["objects"].append({"model": models[placed.upload_id], **described})
            else:
                payload["input_model"] = models[request.upload_id or ""]
        return {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "operation": {"name": request.operation, "version": 1, "payload": payload},
        }
