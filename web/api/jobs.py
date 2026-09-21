"""Drive one disposable worker per slice request and hold the resulting job state."""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from web_job_directory import (
    JOB_ID_PATTERN,
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
from .quotas import QuotaLedger, storage_quota_error
from .store import JobStore
from .uploads import UploadRecord, UploadStore


MAX_SETTINGS = 256
MAX_SETTING_VALUE_CHARS = 4096
MAX_PLATE_INDEX = 64
SETTING_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
JOB_STATES = TERMINAL_STATES | {"queued", "running"}
# The shape of one job record in the metadata store; see `JobRecord.to_stored`.
RECORD_VERSION = 1
_OWNER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_GCODE = "output/model.gcode"
OUTPUT_PREVIEW = "output/preview.json"
OUTPUT_SCENE = "output/scene.json"
# Two job kinds share one executor call site; the flag is a property of the
# job rather than something the executor hardcodes.
OPERATION_SLICE = "slice"
OPERATION_INSPECT = "inspect"
_MANIFEST_FLAGS = {OPERATION_SLICE: "--slice-manifest", OPERATION_INSPECT: "--inspect-manifest"}
# A slice may place at most this many objects, each naming a 4x4 column-major
# transform in millimetres; a source_object may repeat (a duplicate).
MAX_OBJECT_TRANSFORMS = 64
TRANSFORM_LENGTH = 16
# The worker accepts 1-16 filament slots; an object's `filament` indexes into
# whichever count the request actually declared.
MAX_FILAMENT_PROFILES = 16
# Artifacts are downloaded by name, never by the worker's own relative path.
ARTIFACT_NAMES = ("gcode", "result", "preview")
_ARTIFACT_MEDIA = {
    "gcode": ("text/x.gcode", "{job_id}.gcode"),
    "result": ("application/json", "{job_id}-result.json"),
    "preview": ("application/json", "{job_id}-preview.json"),
}
# The preview index names byte ranges into its binary companion, so it is read
# once per job and kept; a layer request then seeks instead of parsing again.
MAX_PREVIEW_INDEX_BYTES = 32 * 1024 * 1024
# The scene index works the same way for a scene's binary triangle data.
MAX_SCENE_INDEX_BYTES = 32 * 1024 * 1024

# An executor status that is not "completed" never carries a worker result, so
# the API states the failure in the worker's own error vocabulary instead.
_EXECUTOR_FAILURES = {
    "canceled": ("cancellation", "job_canceled"),
    "timed_out": ("resource_limit", "wall_time_limit_exceeded"),
    "output_limit": ("resource_limit", None),
    "failed": ("internal", None),
}


def _directory_bytes(root: Path) -> int:
    """Bytes of the regular files under a job directory, never following links."""
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                status = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            if stat.S_ISREG(status.st_mode):
                total += status.st_size
    return total


def _requires_admission(method):
    """Hold a lifecycle lease across validation, staging, and queueing."""
    @functools.wraps(method)
    def admitted(self, *args, **kwargs):
        with self.admission():
            return method(self, *args, **kwargs)

    return admitted


@dataclasses.dataclass(frozen=True)
class SliceRequest:
    """The immutable inputs of one job, reused verbatim by a retry."""

    upload_id: str
    machine_profile: str
    process_profile: str
    # One profile id per filament slot, in order. A tuple (not a list) so the
    # frozen dataclass stays hashable field-for-field, matching the other
    # immutable fields here.
    filament_profiles: Tuple[str, ...]
    settings: Dict[str, str] = dataclasses.field(default_factory=dict)
    plate_index: int = 1
    # Explicit per-object placement: each entry names a source object from the
    # upload's scene, a 4x4 column-major transform in millimetres, and
    # optionally a 1-based `filament` slot to assign it to. Empty means the
    # worker places the model the same way it always has.
    objects: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def describe(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "machine_profile": self.machine_profile,
            "process_profile": self.process_profile,
            "filament_profiles": list(self.filament_profiles),
            "settings": dict(self.settings),
            "plate_index": self.plate_index,
            "objects": [dict(entry) for entry in self.objects],
        }


@dataclasses.dataclass(frozen=True)
class SceneRequest:
    """The immutable inputs of one inspect job, reused verbatim by a retry.

    A scene is derived from an upload, not from a slice job. It still names a
    full profile chain, matching a slice of the same upload, because that is
    the only way to flatten a machine profile's inheritance into a job-local
    file without the worker ever reading the bundled profile tree: only the
    machine profile reaches the inspect manifest, but process and filament are
    validated for compatibility exactly as they would be for a real slice.
    """

    upload_id: str
    machine_profile: str
    process_profile: str
    filament_profile: str
    plate_index: int = 1

    def describe(self) -> Dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "machine_profile": self.machine_profile,
            "process_profile": self.process_profile,
            "filament_profile": self.filament_profile,
            "plate_index": self.plate_index,
        }


@dataclasses.dataclass
class JobRecord:
    job_id: str
    correlation_id: str
    # SliceRequest for a "slice" operation, SceneRequest for "inspect".
    request: Any
    path: Path
    created_at: float
    # The owner this job was accepted for. Never rendered by `describe()`:
    # ownership is an authorization fact, not something a client needs to see.
    owner_id: str
    # Which worker operation this job runs; selects the manifest flag the
    # executor is invoked with, so the executor call site stays generic.
    operation: str = OPERATION_SLICE
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
    # Bytes this job's directory holds against its owner's storage quota: the
    # staged inputs, then the published artifacts too, and 0 once removed.
    stored_bytes: int = 0
    # The flattened profile chain and the overrides that displace it, recorded
    # when the job was accepted so the report explains what was actually sliced.
    profiles: Dict[str, Any] = dataclasses.field(default_factory=dict)
    overrides: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    # What this job's worker run was charged against its owner's CPU budget,
    # and when, so a restart can replay the charge into the quota window.
    cpu_charged_ms: int = 0
    cpu_charged_at: Optional[float] = None
    # Parsed once and kept, so paging through layers/objects never re-reads
    # the index.
    preview_index: Optional[Dict[str, Any]] = None
    scene_index: Optional[Dict[str, Any]] = None
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
            "profiles": dict(self.profiles),
            "overrides": list(self.overrides),
            "retry_of": self.retry_of,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timing": (self.result or {}).get("timing"),
        }

    def to_stored(self) -> Dict[str, Any]:
        """The durable part of the record. Caches and the cancellation event
        belong to one process and are rebuilt, not stored."""
        return {
            "record_version": RECORD_VERSION,
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "owner_id": self.owner_id,
            "operation": self.operation,
            "request": self.request.describe(),
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error else None,
            "result": self.result,
            "retry_of": self.retry_of,
            "stored_bytes": self.stored_bytes,
            "profiles": dict(self.profiles),
            "overrides": list(self.overrides),
            "cpu_charged_ms": self.cpu_charged_ms,
            "cpu_charged_at": self.cpu_charged_at,
        }

    @classmethod
    def from_stored(cls, stored: Dict[str, Any], jobs_root: Path) -> "JobRecord":
        """Rebuild a record `to_stored` wrote. Raises `ValueError`, `KeyError`,
        or `TypeError` for anything else, so a damaged row is never served."""
        if stored.get("record_version") != RECORD_VERSION:
            raise ValueError("unsupported job record version")
        job_id, owner_id, state = stored["job_id"], stored["owner_id"], stored["state"]
        # The job ID becomes a path below the job root, so it is held to the
        # same pattern a fresh one is.
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("invalid job id")
        if not isinstance(owner_id, str) or not _OWNER_ID_PATTERN.fullmatch(owner_id):
            raise ValueError("invalid owner id")
        if state not in JOB_STATES:
            raise ValueError("invalid job state")
        operation = stored["operation"]
        fields = dict(stored["request"])
        if operation == OPERATION_SLICE:
            fields["filament_profiles"] = tuple(fields["filament_profiles"])
            request: Any = SliceRequest(**fields)
        elif operation == OPERATION_INSPECT:
            request = SceneRequest(**fields)
        else:
            raise ValueError("unknown operation")
        return cls(
            job_id=job_id,
            correlation_id=str(stored["correlation_id"]),
            request=request,
            path=Path(jobs_root) / job_id,
            created_at=float(stored["created_at"]),
            owner_id=owner_id,
            operation=operation,
            state=state,
            started_at=stored.get("started_at"),
            finished_at=stored.get("finished_at"),
            progress=dict(stored["progress"]),
            warnings=list(stored.get("warnings") or []),
            error=stored.get("error"),
            result=stored.get("result"),
            retry_of=stored.get("retry_of"),
            stored_bytes=int(stored.get("stored_bytes", 0)),
            profiles=dict(stored.get("profiles") or {}),
            overrides=list(stored.get("overrides") or []),
            cpu_charged_ms=int(stored.get("cpu_charged_ms", 0)),
            cpu_charged_at=stored.get("cpu_charged_at"),
        )

    def _describe_artifacts(self) -> List[Dict[str, Any]]:
        """Name the artifacts that are downloadable now.

        The worker's own relative paths stay internal: a client asks for an
        artifact by name and the API resolves the path it validated.
        """
        if self.result is None:
            return []
        described = [{"name": "result", "media_type": "application/json"}]
        media_types = {"gcode": "text/x.gcode", "preview": "application/json"}
        for artifact in self.result.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            media_type = media_types.get(artifact.get("kind"))
            if media_type is None:
                continue
            described.append(
                {
                    "name": artifact["kind"],
                    "media_type": media_type,
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
        settings_catalog: Optional[SettingsCatalog] = None,
        quotas: Optional[QuotaLedger] = None,
        store: Optional[JobStore] = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        # Absent when the worker could not describe its settings. The worker
        # still validates every override it is given, so the job path stays
        # correct; only the early, friendlier rejection is lost.
        self._settings = settings_catalog
        self._uploads = uploads
        self._log = logger or logging.getLogger("orca.web.api.jobs")
        self._lock = threading.RLock()
        self._admission_condition = threading.Condition(self._lock)
        self._shutdown_lock = threading.Lock()
        self._jobs: Dict[str, JobRecord] = {}
        self._quotas = quotas or QuotaLedger(config.quotas)
        # Serializes storage admission, so two uploads by one owner can never
        # both measure the same remaining budget. Always taken before `_lock`,
        # never inside it, and held across the upload-directory scan so that
        # disk reads never block job-state readers.
        self._storage_lock = threading.Lock()
        # Admission closes before the executor pool is shut down. Keeping this
        # state under the same lock as `_jobs` makes the final accepting check
        # and queue submission atomic with respect to `begin_shutdown()`.
        self._accepting = True
        self._admissions_in_flight = 0
        self._shutdown_complete = False
        # The most recent inspect job for one (owner, upload, machine, process,
        # filament, plate) request, so a repeated scene request returns the
        # job already in flight or finished rather than starting another.
        # Keyed on the owner too, so deduplication never crosses owners.
        self._scenes: Dict[Tuple[str, SceneRequest], str] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=config.max_concurrent_jobs, thread_name_prefix="orca-slice-job"
        )
        # Write-through copy of `_jobs`, read back once here so jobs outlive a
        # restart of this process.
        self._store = store or JobStore(config.metadata_path)
        self._recover()

    # Queries

    def get(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._require(job_id, owner_id).describe()

    def list(self, owner_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            records = sorted(
                (record for record in self._jobs.values() if record.owner_id == owner_id),
                key=lambda record: record.created_at,
            )
            return [record.describe() for record in records]

    def is_accepting(self) -> bool:
        """Whether this instance may accept work that needs a worker later."""
        with self._lock:
            return self._accepting

    def require_accepting(self) -> None:
        """Refuse new mutable work once graceful shutdown has begun."""
        with self._lock:
            if not self._accepting:
                raise ApiError(
                    "service_draining",
                    "This API instance is draining and cannot accept new work.",
                    503,
                )

    @contextmanager
    def admission(self):
        """Keep shutdown from completing while one accepted request mutates state."""
        with self._admission_condition:
            if not self._accepting:
                raise ApiError(
                    "service_draining",
                    "This API instance is draining and cannot accept new work.",
                    503,
                )
            self._admissions_in_flight += 1
        try:
            yield
        finally:
            with self._admission_condition:
                self._admissions_in_flight -= 1
                if self._admissions_in_flight == 0:
                    self._admission_condition.notify_all()

    def quota(self, owner_id: str) -> Dict[str, Any]:
        """One owner's quota limits beside what they currently use of each."""
        with self._storage_lock:
            stored = self._stored_bytes(owner_id)
        cpu_time_ms, submissions, reserved = self._quotas.usage(owner_id)
        with self._lock:
            active = self._active_jobs(owner_id)
        return {
            "limits": self._quotas.limits.describe(),
            "usage": {
                "active_jobs": active,
                "storage_bytes": stored + reserved,
                "cpu_time_ms": cpu_time_ms,
                "submissions": submissions,
            },
        }

    def artifact(self, job_id: str, name: str, owner_id: str) -> Tuple[Path, str, str]:
        """Resolve a downloadable artifact to a path, media type, and filename."""
        if name not in ARTIFACT_NAMES:
            raise ApiError("unknown_artifact", f"No artifact is named {name}.", 404)
        media_type, filename = _ARTIFACT_MEDIA[name]
        with self._lock:
            record = self._require(job_id, owner_id)
            if name == "result":
                # A failed run's report is the useful part, so any outcome the
                # executor validated is served.
                if record.result is None:
                    raise ApiError(
                        "artifact_unavailable", "This job published no validated result.", 409
                    )
                return self._readable(record.path / "result.json"), media_type, filename.format(job_id=job_id)
            path = self._published(record, name)
            return self._readable(path), media_type, filename.format(job_id=job_id)

    def preview(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        """Read the layer preview index, which names byte ranges per layer."""
        return dict(self._preview_index(job_id, owner_id))

    def preview_layer(self, job_id: str, layer: int, owner_id: str) -> Tuple[Path, int, int]:
        """Locate one layer inside the preview blob without reading the rest."""
        index = self._preview_index(job_id, owner_id)
        layers = index.get("layers", [])
        if not isinstance(layers, list) or not 0 <= layer < len(layers):
            raise ApiError("unknown_preview_layer", "This preview has no such layer.", 404)
        described = layers[layer]
        with self._lock:
            record = self._require(job_id, owner_id)
            path = self._published(record, "preview_data")
        offset = int(described.get("offset", 0))
        length = int(described.get("length", 0))
        size = self._readable(path).stat().st_size
        if offset < 0 or length < 0 or offset + length > size:
            raise ApiError("artifact_unavailable", "The preview data no longer matches its index.", 410)
        return path, offset, length

    def scene(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        """Read the scene index: the bed and every object's geometry range."""
        return dict(self._scene_index(job_id, owner_id))

    def scene_object(self, job_id: str, index: int, owner_id: str) -> Tuple[Path, int, int]:
        """Locate one object's triangle soup inside the scene blob without reading the rest."""
        scene = self._scene_index(job_id, owner_id)
        objects = scene.get("objects", [])
        if not isinstance(objects, list) or not 0 <= index < len(objects):
            raise ApiError("unknown_scene_object", "This scene has no such object.", 404)
        described = objects[index]
        with self._lock:
            record = self._require(job_id, owner_id)
            path = self._published(record, "scene_data")
        offset = int(described.get("offset", 0))
        length = int(described.get("length", 0))
        size = self._readable(path).stat().st_size
        if offset < 0 or length < 0 or offset + length > size:
            raise ApiError("artifact_unavailable", "The scene data no longer matches its index.", 410)
        return path, offset, length

    # Commands

    def create_upload(self, filename: Optional[str], reader: BinaryIO, owner_id: str) -> UploadRecord:
        """Store one model within its owner's storage quota.

        The upload is bounded by whatever remains of the budget, reserved for
        the length of the stream so a concurrent upload by the same owner
        cannot spend it twice.
        """
        with self._storage_lock:
            granted = self._quotas.reserve_storage(
                owner_id, self._stored_bytes(owner_id), self._config.job_limits.max_input_bytes
            )
        try:
            return self._uploads.create(filename, reader, owner_id, quota=(granted, self._quotas.limits))
        finally:
            # Released only after `create` has written the upload's metadata,
            # so a concurrent measurement sees the bytes at least once.
            self._quotas.release_storage(owner_id, granted)

    @_requires_admission
    def submit(
        self,
        request: SliceRequest,
        correlation_id: str,
        owner_id: str,
        retry_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.require_accepting()
        upload = self._uploads.get(request.upload_id, owner_id)
        self._validate(request)
        self.sweep()
        self._check_quota(owner_id, upload.size_bytes)

        job_id = f"job-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="orca-profiles-") as staging:
            try:
                profiles = self._catalog.materialize(
                    request.machine_profile,
                    request.process_profile,
                    request.filament_profiles,
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
                    self._slice_manifest(job_id, upload, request),
                    inputs,
                    self._config.job_limits,
                )
            except JobDirectoryError as error:
                raise from_job_directory_error(error) from error

        return self._accept(
            job_id,
            OPERATION_SLICE,
            request,
            prepared.path,
            prepared.staged_bytes,
            correlation_id,
            owner_id,
            retry_of,
            self._describe_chain(request),
            self._describe_overrides(request.settings),
        )

    @_requires_admission
    def submit_scene(
        self,
        request: SceneRequest,
        correlation_id: str,
        owner_id: str,
        retry_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start (or return) the inspect job for one upload, machine, and plate.

        A direct request is idempotent: a matching job already queued, running,
        or succeeded is returned rather than duplicated. A retry (`retry_of`
        set) always starts a fresh job, exactly like a slice retry. The dedup
        key is scoped to the owner, so two owners inspecting the same upload
        (never possible today, since an upload has one owner, but kept
        explicit) never share a job.
        """
        self.require_accepting()
        upload = self._uploads.get(request.upload_id, owner_id)
        self._validate_plate_index(request.plate_index)
        self.sweep()

        scene_key = (owner_id, request)
        if retry_of is None:
            with self._lock:
                existing_job_id = self._scenes.get(scene_key)
                existing = self._jobs.get(existing_job_id) if existing_job_id else None
                if existing is not None and existing.state not in ("failed", "canceled"):
                    return existing.describe()
        self._check_quota(owner_id, upload.size_bytes)

        job_id = f"job-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="orca-profiles-") as staging:
            try:
                profiles = self._catalog.materialize(
                    request.machine_profile,
                    request.process_profile,
                    [request.filament_profile],
                    Path(staging),
                )
            except ProfileCatalogError as error:
                raise from_catalog_error(error) from error
            # The inspect manifest names only the machine profile; process and
            # filament were validated above but are never staged or read.
            inputs = [
                JobInput(f"input/model.{upload.model_format}", upload.path, "model"),
                JobInput("profiles/machine.json", profiles["machine"], "profile"),
            ]
            try:
                prepared = prepare_job_directory(
                    self._config.jobs_root,
                    job_id,
                    self._inspect_manifest(job_id, upload, request),
                    inputs,
                    self._config.job_limits,
                )
            except JobDirectoryError as error:
                raise from_job_directory_error(error) from error

        accepted = self._accept(
            job_id,
            OPERATION_INSPECT,
            request,
            prepared.path,
            prepared.staged_bytes,
            correlation_id,
            owner_id,
            retry_of,
            self._describe_chain(request),
            [],
        )
        with self._lock:
            self._scenes[scene_key] = job_id
        return accepted

    def cancel(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._require(job_id, owner_id)
            if record.state in TERMINAL_STATES:
                raise ApiError("job_not_cancelable", "This job has already finished.", 409)
            record.cancellation.set()
            if record.state == "queued":
                # No worker exists yet, so the job is canceled here and _run
                # returns without launching one.
                record.stored_bytes = 0
                self._finish(record, "canceled", self._error("cancellation", "job_canceled", "The job was canceled."))
                if not remove_job_directory(record.path):
                    self._log.warning(
                        "queued job cleanup failed job_id=%s path=%s", job_id, record.path
                    )
            self._log.info("job cancellation requested job_id=%s state=%s", job_id, record.state)
            return record.describe()

    def retry(self, job_id: str, correlation_id: str, owner_id: str) -> Dict[str, Any]:
        """Rerun a finished job's inputs. The original owner is retained even
        though `owner_id` (already checked by `_require`) must match it: a
        caller can never transfer a job by retrying it."""
        with self._lock:
            record = self._require(job_id, owner_id)
            if record.state not in TERMINAL_STATES:
                raise ApiError("job_not_retryable", "This job has not finished yet.", 409)
            operation = record.operation
            request = record.request
            original_owner = record.owner_id
        if operation == OPERATION_INSPECT:
            return self.submit_scene(request, correlation_id, original_owner, retry_of=job_id)
        return self.submit(request, correlation_id, original_owner, retry_of=job_id)

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
        # A finished job's record is kept exactly as long as its directory
        # would be. Without this, jobs whose directories were removed when they
        # failed or were canceled would be remembered forever.
        horizon = time.time() - self._config.job_limits.retention_seconds
        with self._lock:
            removed = set(report.removed)
            removed.update(
                job_id
                for job_id, record in self._jobs.items()
                if record.state in TERMINAL_STATES and (record.finished_at or record.created_at) <= horizon
            )
            for job_id in removed:
                self._jobs.pop(job_id, None)
            if removed:
                # Never point a future scene request at a job directory that
                # is gone.
                self._scenes = {
                    key: job_id for key, job_id in self._scenes.items() if job_id not in removed
                }
                try:
                    self._store.delete(removed)
                except ApiError:
                    self._log.error("job metadata delete failed count=%d", len(removed))
        uploads = self._uploads.sweep(self._config.job_limits.retention_seconds)
        return {"jobs_removed": report.removed, "jobs_retained": report.retained, "uploads_removed": uploads}

    def begin_shutdown(self) -> None:
        """Close admission and request cancellation without waiting for workers."""
        queued_paths: List[Path] = []
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            for record in self._jobs.values():
                if record.state in TERMINAL_STATES:
                    continue
                record.cancellation.set()
                if record.state == "queued":
                    record.stored_bytes = 0
                    self._finish(
                        record,
                        "canceled",
                        self._error("cancellation", "job_canceled", "The job was canceled."),
                    )
                    queued_paths.append(record.path)
        for path in queued_paths:
            if not remove_job_directory(path):
                self._log.warning("shutdown job cleanup failed path=%s", path)
        self._log.info("job service draining queued_canceled=%d", len(queued_paths))

    def shutdown(self) -> None:
        """Drain active work, escalating worker cancellation in the executor."""
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self.begin_shutdown()
            with self._admission_condition:
                while self._admissions_in_flight:
                    self._admission_condition.wait()
            # Queued records were made terminal above; canceling their futures
            # ensures they never consume a worker slot just to observe that state.
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._store.close()
            self._shutdown_complete = True

    # Execution

    def _accept(
        self,
        job_id: str,
        operation: str,
        request: Any,
        path: Path,
        staged_bytes: int,
        correlation_id: str,
        owner_id: str,
        retry_of: Optional[str],
        profiles: Dict[str, Any],
        overrides: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Record a job the job-directory layer already staged, and queue it.

        Shared by `submit` and `submit_scene`: the two operations differ only
        in how their manifest and inputs are built, not in how a job is held,
        logged, or handed to the worker pool.
        """
        record = JobRecord(
            job_id=job_id,
            correlation_id=correlation_id,
            request=request,
            path=path,
            created_at=time.time(),
            owner_id=owner_id,
            operation=operation,
            retry_of=retry_of,
            profiles=profiles,
            overrides=overrides,
            stored_bytes=staged_bytes,
        )
        rejected: Optional[ApiError] = None
        with self._lock:
            if not self._accepting:
                rejected = ApiError(
                    "service_draining",
                    "This API instance is draining and cannot accept new work.",
                    503,
                )
            else:
                # Checked again under the same lock that records the job, so
                # concurrent submissions by one owner cannot all pass the
                # precheck in `_check_quota` and exceed the job budgets.
                try:
                    self._quotas.check_submission(owner_id, self._active_jobs(owner_id))
                except ApiError as error:
                    rejected = error
            if rejected is None:
                # Durable before it is queued: a job the store never recorded
                # is refused rather than run and then forgotten by a restart.
                try:
                    self._store.save(record.to_stored())
                except ApiError as error:
                    rejected = error
            if rejected is None:
                self._quotas.record_submission(owner_id)
                self._jobs[job_id] = record
                # Queue while holding the admission lock. Shutdown cannot close
                # the pool between recording and submitting this job.
                self._pool.submit(self._run, job_id)
                # Snapshot before the worker takes the lock: an acceptance must
                # report the job as accepted even if it starts immediately.
                accepted = record.describe()
        if rejected is not None:
            remove_job_directory(record.path)
            raise rejected
        self._log.info(
            "job accepted job_id=%s operation=%s correlation_id=%s retry_of=%s staged_bytes=%d",
            job_id,
            operation,
            correlation_id,
            retry_of,
            staged_bytes,
        )
        return accepted

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state in TERMINAL_STATES:
                return
            record.state = "running"
            record.started_at = time.time()
            record.progress = {"stage": "input", "percent": 0, "message": ""}
            self._persist(record)
            manifest_path = record.path / MANIFEST_NAME
            manifest_flag = _MANIFEST_FLAGS[record.operation]

        started = time.monotonic()
        execution = None
        try:
            execution = execute_worker(
                self._config.worker_command,
                manifest_path,
                self._config.executor_limits,
                record.cancellation,
                lambda event: self._observe(record, event),
                manifest_flag=manifest_flag,
                sandbox=self._config.sandbox,
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
        finally:
            self._charge_cpu(record, execution, started)

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
            stored_bytes = _directory_bytes(record.path)
            with self._lock:
                record.result = result
                record.stored_bytes = stored_bytes
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
            record.stored_bytes = 0
            self._finish(record, state, self._error(category, code, execution.error_message or "The job failed."))

    def _fail(self, record: JobRecord, category: str, code: str, message: str) -> None:
        remove_job_directory(record.path)
        with self._lock:
            record.stored_bytes = 0
            self._finish(record, "failed", self._error(category, code, message))

    def _finish(self, record: JobRecord, state: str, error: Optional[Dict[str, Any]]) -> None:
        """Make a record terminal. Call with `_lock` held, after any field the
        terminal state depends on (`result`, `stored_bytes`) is already set."""
        record.state = state
        record.error = error
        record.finished_at = time.time()
        if state == "succeeded":
            record.progress = {"stage": "finalize", "percent": 100, "message": ""}
        self._persist(record)

    def _persist(self, record: JobRecord) -> None:
        """Write one record through to the store. Call with `_lock` held.

        A failed write is logged rather than raised: the in-memory table stays
        authoritative while this process runs, and the worst a stale stored
        copy can cause is a job recovered as interrupted after a restart.
        """
        try:
            self._store.save(record.to_stored())
        except ApiError:
            self._log.error("job metadata write failed job_id=%s state=%s", record.job_id, record.state)

    def _recover(self) -> None:
        """Reload the stored job table and settle what an earlier process left.

        A job still queued or running in the store has no worker any more, and
        its directory may hold files no executor validated, so it fails as
        `job_interrupted` and its directory is removed; a retry reruns its
        inputs. Submissions and CPU charges still inside the quota window are
        replayed into the ledger, oldest first.
        """
        now = time.time()
        window = self._quotas.limits.window_seconds
        interrupted = 0
        with self._lock:
            for stored in self._store.load():
                try:
                    record = JobRecord.from_stored(stored, self._config.jobs_root)
                except (KeyError, TypeError, ValueError):
                    self._log.warning("unreadable job record skipped")
                    continue
                if record.state not in TERMINAL_STATES:
                    remove_job_directory(record.path)
                    record.stored_bytes = 0
                    self._finish(
                        record,
                        "failed",
                        self._error("internal", "job_interrupted", "The service restarted before this job finished."),
                    )
                    interrupted += 1
                self._jobs[record.job_id] = record
                if record.operation == OPERATION_INSPECT:
                    # Loaded oldest first, so the newest job for a request wins.
                    self._scenes[(record.owner_id, record.request)] = record.job_id
            records = list(self._jobs.values())
        for record in sorted(records, key=lambda item: item.created_at):
            age = now - record.created_at
            if age < window:
                self._quotas.record_submission(record.owner_id, max(0.0, age))
        charged = [record for record in records if record.cpu_charged_at is not None]
        for record in sorted(charged, key=lambda item: item.cpu_charged_at):
            age = now - record.cpu_charged_at
            if age < window:
                self._quotas.charge_cpu(record.owner_id, record.cpu_charged_ms, max(0.0, age))
        if records:
            self._log.info("job metadata recovered jobs=%d interrupted=%d", len(records), interrupted)

    # Helpers

    @staticmethod
    def _error(category: str, code: str, message: str) -> Dict[str, str]:
        return {"category": category, "code": code, "message": message}

    def _active_jobs(self, owner_id: str) -> int:
        """Queued and running jobs held for one owner. Call with `_lock` held."""
        return sum(
            1 for record in self._jobs.values() if record.owner_id == owner_id and record.state not in TERMINAL_STATES
        )

    def _stored_bytes(self, owner_id: str) -> int:
        """Bytes one owner holds in uploads and in this process's job directories.

        Call with `_storage_lock` held (never `_lock`): the upload scan reads
        disk, and `_lock` is taken only briefly for the job table.
        """
        uploads = self._uploads.owner_bytes(owner_id)
        with self._lock:
            jobs = sum(record.stored_bytes for record in self._jobs.values() if record.owner_id == owner_id)
        return uploads + jobs

    def _check_quota(self, owner_id: str, staged_bytes: int) -> None:
        """Refuse a job before it is staged when its owner has no budget left.

        `_accept` repeats the job-count checks atomically with recording the
        job; this earlier pass keeps a refused job from ever copying inputs.
        """
        with self._lock:
            self._quotas.check_submission(owner_id, self._active_jobs(owner_id))
        with self._storage_lock:
            if self._stored_bytes(owner_id) + staged_bytes > self._quotas.limits.max_storage_bytes:
                raise storage_quota_error(self._quotas.limits)

    def _charge_cpu(self, record: JobRecord, execution: Any, started: float) -> None:
        """Charge one finished worker run to its owner's CPU budget.

        The worker is untrusted, so its reported CPU time is never taken below
        the wall time the executor itself measured, and never above the CPU
        limit the kernel enforced on it.
        """
        charged = int((time.monotonic() - started) * 1000)
        timing = (execution.result or {}).get("timing") if execution is not None else None
        reported = timing.get("cpu_time_ms") if isinstance(timing, dict) else None
        if isinstance(reported, int) and not isinstance(reported, bool):
            charged = max(charged, min(reported, self._config.executor_limits.cpu_time_ms))
        self._quotas.charge_cpu(record.owner_id, charged)
        with self._lock:
            record.cpu_charged_ms = charged
            record.cpu_charged_at = time.time()
            self._persist(record)

    def _require(self, job_id: str, owner_id: str) -> JobRecord:
        record = self._jobs.get(job_id)
        # A job that belongs to another owner is reported exactly like one
        # that does not exist, so a job ID is never a cross-owner existence
        # oracle.
        if record is None or record.owner_id != owner_id:
            raise ApiError("unknown_job", "No job is known under that id.", 404)
        return record

    @staticmethod
    def _readable(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ApiError("artifact_unavailable", "The artifact is no longer available.", 410)
        return path

    @staticmethod
    def _published(record: JobRecord, kind: str) -> Path:
        """Find the path the executor validated for one published artifact kind."""
        if record.state == "succeeded" and record.result is not None:
            for artifact in record.result.get("artifacts", []):
                if not isinstance(artifact, dict) or artifact.get("kind") != kind:
                    continue
                path = resolve_artifact_path(record.path, artifact.get("path"))
                if path is not None:
                    return path
        raise ApiError("artifact_unavailable", f"This job published no {kind}.", 409)

    def _preview_index(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._require(job_id, owner_id)
            if record.preview_index is not None:
                return record.preview_index
            path = self._readable(self._published(record, "preview"))
            if path.stat().st_size > MAX_PREVIEW_INDEX_BYTES:
                raise ApiError("artifact_unavailable", "The preview index is too large to serve.", 409)
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ApiError("artifact_unavailable", "The preview index could not be read.", 410) from error
            if not isinstance(index, dict) or not isinstance(index.get("layers"), list):
                raise ApiError("artifact_unavailable", "The preview index is not a preview.", 410)
            record.preview_index = index
            return index

    def _scene_index(self, job_id: str, owner_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._require(job_id, owner_id)
            if record.scene_index is not None:
                return record.scene_index
            path = self._readable(self._published(record, "scene"))
            if path.stat().st_size > MAX_SCENE_INDEX_BYTES:
                raise ApiError("artifact_unavailable", "The scene index is too large to serve.", 409)
            try:
                index = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ApiError("artifact_unavailable", "The scene index could not be read.", 410) from error
            if not isinstance(index, dict) or not isinstance(index.get("objects"), list):
                raise ApiError("artifact_unavailable", "The scene index is not a scene.", 410)
            record.scene_index = index
            return index

    def _validate(self, request: SliceRequest) -> None:
        self._validate_plate_index(request.plate_index)
        self._validate_filament_profiles(request.filament_profiles)
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
        if self._settings is not None:
            try:
                self._settings.validate_overrides(request.settings)
            except SettingsCatalogError as error:
                raise from_settings_error(error) from error
        self._validate_objects(request.objects, len(request.filament_profiles))

    @staticmethod
    def _validate_plate_index(plate_index: Any) -> None:
        if not isinstance(plate_index, int) or not 1 <= plate_index <= MAX_PLATE_INDEX:
            raise ApiError("invalid_plate_index", "plate_index must be a 1-based plate number.")

    @staticmethod
    def _validate_filament_profiles(filament_profiles: Any) -> None:
        # Re-checked here, not only in the request schema: a retry replays a
        # stored SliceRequest without ever passing through that schema.
        if not isinstance(filament_profiles, (tuple, list)) or not (
            1 <= len(filament_profiles) <= MAX_FILAMENT_PROFILES
        ):
            raise ApiError(
                "invalid_filament_profiles",
                f"A slice must name 1 to {MAX_FILAMENT_PROFILES} filament profiles.",
            )

    @staticmethod
    def _validate_objects(objects: List[Dict[str, Any]], filament_count: int) -> None:
        """Check bounds, shape, and finiteness before a worker ever sees these.

        `source_object` is not checked against a scene's actual object count:
        that count lives in a scene the worker produced, and only the worker
        that runs the slice can say whether an index it names is in range.
        """
        if len(objects) > MAX_OBJECT_TRANSFORMS:
            raise ApiError(
                "invalid_objects", f"A request may place at most {MAX_OBJECT_TRANSFORMS} objects."
            )
        for entry in objects:
            if not isinstance(entry, dict):
                raise ApiError("invalid_objects", "Each placed object must be an object.")
            source = entry.get("source_object")
            if not isinstance(source, int) or isinstance(source, bool) or source < 0:
                raise ApiError("invalid_objects", "source_object must be a non-negative integer.")
            transform = entry.get("transform")
            if not isinstance(transform, list) or len(transform) != TRANSFORM_LENGTH:
                raise ApiError(
                    "invalid_objects", f"transform must be an array of {TRANSFORM_LENGTH} numbers."
                )
            for value in transform:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ApiError("invalid_objects", "transform must contain only finite numbers.")
            # 0 (or omitted) means "leave this object's own assignment alone",
            # matching the worker's own meaning for the field.
            filament = entry.get("filament", 0)
            if isinstance(filament, bool) or not isinstance(filament, int) or not 0 <= filament <= filament_count:
                raise ApiError(
                    "invalid_object_filament",
                    f"filament must be 0 (unchanged) or a 1-based index up to {filament_count}.",
                )

    def _describe_chain(self, request: Any) -> Dict[str, Any]:
        """Name the effective profile chain each selection flattens.

        Shared by a slice and a scene request: `filaments` is always a list,
        even for a scene, which only ever names one filament (for
        compatibility validation; a scene manifest never references it).
        """
        filament_profiles = (
            request.filament_profiles if isinstance(request, SliceRequest) else (request.filament_profile,)
        )
        return {
            "machine": self._catalog.get(request.machine_profile, "machine").describe(),
            "process": self._catalog.get(request.process_profile, "process").describe(),
            "filaments": [self._catalog.get(filament_id, "filament").describe() for filament_id in filament_profiles],
        }

    def _describe_overrides(self, settings: Dict[str, str]) -> List[Dict[str, Any]]:
        """Pair each override with the engine's own description of the setting."""
        described = []
        for key, value in sorted(settings.items()):
            entry: Dict[str, Any] = {"key": key, "value": value}
            definition = self._settings.definition(key) if self._settings else None
            if definition is not None:
                entry.update(
                    {
                        "label": definition.get("label", ""),
                        "unit": definition.get("unit", ""),
                        "scope": definition.get("scope", ""),
                    }
                )
            described.append(entry)
        return described

    def _slice_manifest(self, job_id: str, upload: UploadRecord, request: SliceRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "input_model": f"input/model.{upload.model_format}",
            "output_gcode": OUTPUT_GCODE,
            "output_preview": OUTPUT_PREVIEW,
            "profiles": {
                "machine": "profiles/machine.json",
                "process": "profiles/process.json",
                # Always the array form, even for one filament: a one-element
                # array takes the worker's unchanged single-filament path, so
                # the API only ever has to speak one spelling.
                "filaments": [
                    f"profiles/filament-{index}.json" for index in range(len(request.filament_profiles))
                ],
            },
            "settings": dict(request.settings),
            "plate_index": request.plate_index,
        }
        if request.objects:
            payload["objects"] = [dict(entry) for entry in request.objects]
        return {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "operation": {"name": "slice", "version": 1, "payload": payload},
        }

    def _inspect_manifest(self, job_id: str, upload: UploadRecord, request: SceneRequest) -> Dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "operation": {
                "name": "inspect",
                "version": 1,
                "payload": {
                    "input_model": f"input/model.{upload.model_format}",
                    "output_scene": OUTPUT_SCENE,
                    "profiles": {"machine": "profiles/machine.json"},
                    "plate_index": request.plate_index,
                },
            },
        }
