"""Drive one disposable worker per slice request and hold the resulting job state."""

from __future__ import annotations

import dataclasses
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
from web_worker_executor import ExecutorError, execute_worker, resolve_artifact_path

from . import PROTOCOL_VERSION
from .config import ApiConfig
from .errors import ApiError, from_catalog_error, from_job_directory_error
from .uploads import UploadRecord, UploadStore


MAX_SETTINGS = 256
MAX_SETTING_VALUE_CHARS = 4096
MAX_PLATE_INDEX = 64
SETTING_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
OUTPUT_GCODE = "output/model.gcode"
# Artifacts are downloaded by name, never by the worker's own relative path.
ARTIFACT_NAMES = ("gcode", "result")

# An executor status that is not "completed" never carries a worker result, so
# the API states the failure in the worker's own error vocabulary instead.
_EXECUTOR_FAILURES = {
    "canceled": ("cancellation", "job_canceled"),
    "timed_out": ("resource_limit", "wall_time_limit_exceeded"),
    "output_limit": ("resource_limit", None),
    "failed": ("internal", None),
}


@dataclasses.dataclass(frozen=True)
class SliceRequest:
    """The immutable inputs of one job, reused verbatim by a retry."""

    upload_id: str
    machine_profile: str
    process_profile: str
    filament_profile: str
    settings: Dict[str, str] = dataclasses.field(default_factory=dict)
    plate_index: int = 1

    def describe(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "machine_profile": self.machine_profile,
            "process_profile": self.process_profile,
            "filament_profile": self.filament_profile,
            "settings": dict(self.settings),
            "plate_index": self.plate_index,
        }


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
    cancellation: threading.Event = dataclasses.field(default_factory=threading.Event)

    def describe(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "state": self.state,
            "progress": dict(self.progress),
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error else None,
            "artifacts": self._describe_artifacts(),
            "request": self.request.describe(),
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
            if isinstance(artifact, dict) and artifact.get("kind") == "gcode":
                described.append(
                    {
                        "name": "gcode",
                        "media_type": "text/x.gcode",
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
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
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

            if record.state == "succeeded" and record.result is not None:
                for artifact in record.result.get("artifacts", []):
                    if not isinstance(artifact, dict) or artifact.get("kind") != "gcode":
                        continue
                    path = resolve_artifact_path(record.path, artifact.get("path"))
                    if path is not None:
                        return self._readable(path), "text/x.gcode", f"{job_id}.gcode"
            raise ApiError("artifact_unavailable", "This job published no G-code.", 409)

    # Commands

    def submit(
        self, request: SliceRequest, correlation_id: str, retry_of: Optional[str] = None
    ) -> Dict[str, Any]:
        upload = self._uploads.get(request.upload_id)
        self._validate(request)
        self.sweep()

        job_id = f"job-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="orca-profiles-") as staging:
            try:
                profiles = self._catalog.materialize(
                    request.machine_profile,
                    request.process_profile,
                    request.filament_profile,
                    Path(staging),
                )
            except ProfileCatalogError as error:
                raise from_catalog_error(error) from error
            inputs = [JobInput(f"input/model.{upload.model_format}", upload.path, "model")]
            inputs += [
                JobInput(f"profiles/{kind}.json", path, "profile")
                for kind, path in sorted(profiles.items())
            ]
            try:
                prepared = prepare_job_directory(
                    self._config.jobs_root,
                    job_id,
                    self._manifest(job_id, upload, request),
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

    def _validate(self, request: SliceRequest) -> None:
        if not isinstance(request.plate_index, int) or not 1 <= request.plate_index <= MAX_PLATE_INDEX:
            raise ApiError("invalid_plate_index", "plate_index must be a 1-based plate number.")
        if len(request.settings) > MAX_SETTINGS:
            raise ApiError(
                "invalid_settings", f"A request may override at most {MAX_SETTINGS} settings."
            )
        for key, value in request.settings.items():
            if not SETTING_KEY_PATTERN.match(key):
                raise ApiError("invalid_settings", f"{key!r} is not a setting name.")
            if not isinstance(value, str) or len(value) > MAX_SETTING_VALUE_CHARS:
                raise ApiError(
                    "invalid_settings",
                    f"The value of {key} must be a string of at most {MAX_SETTING_VALUE_CHARS} characters.",
                )

    def _manifest(self, job_id: str, upload: UploadRecord, request: SliceRequest) -> Dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "operation": {
                "name": "slice",
                "version": 1,
                "payload": {
                    "input_model": f"input/model.{upload.model_format}",
                    "output_gcode": OUTPUT_GCODE,
                    "profiles": {
                        "machine": "profiles/machine.json",
                        "process": "profiles/process.json",
                        "filament": "profiles/filament.json",
                    },
                    "settings": dict(request.settings),
                    "plate_index": request.plate_index,
                },
            },
        }
