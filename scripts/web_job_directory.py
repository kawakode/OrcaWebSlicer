#!/usr/bin/env python3
"""Create, populate, and reclaim the disposable job directories workers run in."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence


# Matches the job_id the worker accepts in its envelope, so a directory name can
# never encode something the worker would later reject.
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MANIFEST_NAME = "request.json"
MAX_MANIFEST_BYTES = 1024 * 1024


class JobDirectoryError(RuntimeError):
    """A stable job-directory failure that is safe to return to the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class JobDirectoryLimits:
    max_input_bytes: int = 250 * 1024 * 1024
    max_total_input_bytes: int = 512 * 1024 * 1024
    max_inputs: int = 16
    retention_seconds: int = 24 * 60 * 60

    def validate(self) -> None:
        for field in dataclasses.fields(self):
            if getattr(self, field.name) <= 0:
                raise JobDirectoryError("invalid_job_directory_limit", f"{field.name} must be positive.")
        if self.max_input_bytes > self.max_total_input_bytes:
            raise JobDirectoryError(
                "invalid_job_directory_limit", "max_input_bytes cannot exceed max_total_input_bytes."
            )


@dataclasses.dataclass(frozen=True)
class JobInput:
    """One declared file to stage, named by where it lands inside the job."""

    relative_path: str
    source_path: Path
    kind: str = "input"


@dataclasses.dataclass(frozen=True)
class PreparedJob:
    job_id: str
    path: Path
    manifest_path: Path
    staged_bytes: int


@dataclasses.dataclass
class SweepReport:
    removed: List[str] = dataclasses.field(default_factory=list)
    retained: List[str] = dataclasses.field(default_factory=list)
    failed: Dict[str, str] = dataclasses.field(default_factory=dict)


def _validate_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
        raise JobDirectoryError(
            "invalid_job_id", "job_id must be 1-128 letters, digits, dots, dashes, or underscores."
        )
    if job_id in {".", ".."}:
        raise JobDirectoryError("invalid_job_id", "job_id cannot name a directory entry.")


def _canonical_root(root: Path) -> Path:
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve(strict=True)
    except OSError as error:
        raise JobDirectoryError("job_root_unavailable", "The job root could not be created.") from error


def _resolve_staged_path(job_root: Path, relative_path: str) -> Path:
    """Resolve a declared destination, rejecting anything that leaves the job."""
    if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 512:
        raise JobDirectoryError("invalid_staged_path", "A declared path must be a non-empty relative path.")
    if "\\" in relative_path or relative_path.startswith("/"):
        raise JobDirectoryError("invalid_staged_path", "A declared path must use relative POSIX separators.")

    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise JobDirectoryError("invalid_staged_path", "A declared path cannot traverse or repeat separators.")
    if candidate.name == MANIFEST_NAME and len(candidate.parts) == 1:
        raise JobDirectoryError("invalid_staged_path", f"A declared path cannot replace {MANIFEST_NAME}.")

    # Every component must already be a real directory inside the job, so a
    # symlink planted by an earlier job cannot redirect the copy.
    current = job_root
    for part in candidate.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise JobDirectoryError("invalid_staged_path", "A declared path crosses a symbolic link.")
        current.mkdir(mode=0o700, exist_ok=True)
    destination = current / candidate.name
    if destination.exists() or destination.is_symlink():
        raise JobDirectoryError("duplicate_staged_path", "A declared path was staged more than once.")
    return destination


def _copy_bounded(source: Path, destination: Path, remaining: int) -> int:
    """Copy at most remaining bytes, failing if the source is larger."""
    try:
        with open(source, "rb") as reader:
            source_stat = os.fstat(reader.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise JobDirectoryError("invalid_job_input", "A declared input is not a regular file.")
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            copied = 0
            with open(descriptor, "wb") as writer:
                while True:
                    chunk = reader.read(64 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > remaining:
                        raise JobDirectoryError(
                            "job_input_size_limit_exceeded",
                            "The declared inputs exceed the configured size limit.",
                        )
                    writer.write(chunk)
            return copied
    except JobDirectoryError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise JobDirectoryError("job_input_unreadable", "A declared input could not be copied.") from error


def create_job_directory(root: Path, job_id: str) -> Path:
    """Create one private, previously unused directory for this job."""
    _validate_job_id(job_id)
    canonical_root = _canonical_root(root)
    path = canonical_root / job_id
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as error:
        raise JobDirectoryError(
            "job_directory_exists", "A job directory for this job_id already exists."
        ) from error
    except OSError as error:
        raise JobDirectoryError("job_directory_unavailable", "The job directory could not be created.") from error
    return path


def prepare_job_directory(
    root: Path,
    job_id: str,
    manifest: Dict[str, Any],
    inputs: Sequence[JobInput],
    limits: Optional[JobDirectoryLimits] = None,
) -> PreparedJob:
    """Stage exactly the declared inputs plus the manifest, or leave nothing behind."""
    limits = limits or JobDirectoryLimits()
    limits.validate()
    if manifest.get("job_id") != job_id:
        raise JobDirectoryError("job_id_mismatch", "The manifest job_id does not match the requested job.")
    if len(inputs) > limits.max_inputs:
        raise JobDirectoryError("too_many_job_inputs", "The request declares too many input files.")

    serialized = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_MANIFEST_BYTES:
        raise JobDirectoryError("manifest_too_large", "The worker manifest exceeds the 1 MiB limit.")

    path = create_job_directory(root, job_id)
    staged_bytes = 0
    try:
        for declared in inputs:
            destination = _resolve_staged_path(path, declared.relative_path)
            allowance = min(limits.max_input_bytes, limits.max_total_input_bytes - staged_bytes)
            staged_bytes += _copy_bounded(Path(declared.source_path), destination, allowance)

        # The manifest lands last and atomically, so a directory holding one is
        # always fully staged.
        manifest_path = path / MANIFEST_NAME
        temporary = path / f".{MANIFEST_NAME}.partial"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "wb") as writer:
            writer.write(serialized)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, manifest_path)
    except BaseException:
        remove_job_directory(path)
        raise

    return PreparedJob(job_id=job_id, path=path, manifest_path=manifest_path, staged_bytes=staged_bytes)


def remove_job_directory(path: Path) -> bool:
    """Delete one job directory and everything it holds. Never raises.

    Also removes a stray file or link left in the job root, which is never a
    job and would otherwise accumulate forever.
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def sweep_job_directories(
    root: Path,
    limits: Optional[JobDirectoryLimits] = None,
    active_job_ids: Iterable[str] = (),
    now: Optional[float] = None,
) -> SweepReport:
    """Reclaim expired and abandoned job directories, keeping running jobs.

    Expiry is measured from a directory's last modification, so a job that is
    still writing stays young. Jobs the caller reports as running are never
    swept, which covers a worker that is quiet for longer than the retention
    window.
    """
    limits = limits or JobDirectoryLimits()
    limits.validate()
    report = SweepReport()
    active = set(active_job_ids)
    deadline = (time.time() if now is None else now) - limits.retention_seconds

    try:
        entries = sorted(Path(root).iterdir())
    except FileNotFoundError:
        return report
    except OSError as error:
        raise JobDirectoryError("job_root_unavailable", "The job root could not be listed.") from error

    for entry in entries:
        name = entry.name
        if name in active:
            report.retained.append(name)
            continue
        # A stray file or symlink in the job root is not a job directory and is
        # removed on sight rather than left to accumulate.
        if entry.is_symlink() or not entry.is_dir():
            if remove_job_directory(entry):
                report.removed.append(name)
            else:
                report.failed[name] = "unremovable_job_entry"
            continue
        try:
            modified = entry.stat().st_mtime
        except OSError:
            report.failed[name] = "unreadable_job_directory"
            continue
        if modified > deadline:
            report.retained.append(name)
            continue
        if remove_job_directory(entry):
            report.removed.append(name)
        else:
            report.failed[name] = "unremovable_job_directory"
    return report


def limits_from_environment() -> JobDirectoryLimits:
    defaults = JobDirectoryLimits()

    def read(name: str, default: int) -> int:
        serialized = os.environ.get(name)
        if serialized is None or serialized == "":
            return default
        try:
            value = int(serialized)
        except ValueError as error:
            raise JobDirectoryError(
                "invalid_job_directory_limit", f"{name} must be a positive integer."
            ) from error
        if value <= 0:
            raise JobDirectoryError("invalid_job_directory_limit", f"{name} must be a positive integer.")
        return value

    return JobDirectoryLimits(
        max_input_bytes=read("ORCA_WEB_MAX_INPUT_BYTES", defaults.max_input_bytes),
        max_total_input_bytes=read("ORCA_WEB_MAX_TOTAL_INPUT_BYTES", defaults.max_total_input_bytes),
        max_inputs=read("ORCA_WEB_MAX_JOB_INPUTS", defaults.max_inputs),
        retention_seconds=read("ORCA_WEB_JOB_RETENTION_SECONDS", defaults.retention_seconds),
    )
