#!/usr/bin/env python3
"""Run one OrcaSlicer web worker behind bounded, disposable-process controls."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


TERMINAL_STATES = {"succeeded", "failed", "canceled"}
MAX_MANIFEST_BYTES = 1024 * 1024


class ExecutorError(RuntimeError):
    """A stable executor failure that is safe to return to the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class ExecutorLimits:
    wall_time_ms: int = 300_000
    cpu_time_ms: int = 300_000
    memory_bytes: int = 4 * 1024 * 1024 * 1024
    output_bytes: int = 1024 * 1024 * 1024
    process_count: int = 64
    open_files: int = 128
    termination_grace_ms: int = 5_000
    event_bytes: int = 1024 * 1024
    event_line_bytes: int = 64 * 1024
    event_count: int = 4_096
    log_bytes: int = 1024 * 1024

    def validate(self) -> None:
        for field in dataclasses.fields(self):
            if getattr(self, field.name) <= 0:
                raise ExecutorError("invalid_executor_limit", f"{field.name} must be positive.")
        if self.event_line_bytes > self.event_bytes:
            raise ExecutorError(
                "invalid_executor_limit", "event_line_bytes cannot exceed event_bytes."
            )


@dataclasses.dataclass
class ExecutionResult:
    status: str
    worker_exit_code: Optional[int]
    forced_termination: bool
    events: List[Dict[str, Any]]
    result: Optional[Dict[str, Any]]
    event_bytes: int
    log_bytes: int
    captured_log_bytes: int
    stderr: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return (
            self.status == "completed"
            and self.result is not None
            and self.result.get("outcome") == "succeeded"
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "worker_exit_code": self.worker_exit_code,
            "forced_termination": self.forced_termination,
            "event_count": len(self.events),
            "event_bytes": self.event_bytes,
            "log_bytes": self.log_bytes,
            "stderr_truncated": self.log_bytes > self.captured_log_bytes,
            "outcome": self.result.get("outcome") if self.result else None,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
        }


class _ProtocolCollector:
    def __init__(self, limits: ExecutorLimits) -> None:
        self._limits = limits
        self._line = bytearray()
        self._discard_line = False
        self.events: List[Dict[str, Any]] = []
        self.total_bytes = 0
        self.error: Optional[ExecutorError] = None
        self.limit_reached = threading.Event()

    def consume(self, stream: Any) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            self.total_bytes += len(chunk)
            if self.total_bytes > self._limits.event_bytes:
                self._fail(
                    "event_volume_limit_exceeded",
                    "Worker event output exceeded the configured byte limit.",
                )
            self._consume_chunk(chunk)
        if self._line and not self._discard_line:
            self._consume_line(bytes(self._line))

    def _consume_chunk(self, chunk: bytes) -> None:
        for value in chunk:
            if value == 0x0A:
                if not self._discard_line:
                    self._consume_line(bytes(self._line).rstrip(b"\r"))
                self._line.clear()
                self._discard_line = False
            elif not self._discard_line:
                self._line.append(value)
                if len(self._line) > self._limits.event_line_bytes:
                    self._fail(
                        "event_line_limit_exceeded",
                        "A worker event exceeded the configured line limit.",
                    )
                    self._line.clear()
                    self._discard_line = True

    def _consume_line(self, line: bytes) -> None:
        if not line:
            self._fail("malformed_worker_output", "Worker stdout contained an empty line.")
            return
        if len(self.events) >= self._limits.event_count:
            self._fail("event_count_limit_exceeded", "Worker emitted too many protocol events.")
            return
        try:
            decoded = line.decode("utf-8")
            event = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._fail("malformed_worker_output", "Worker stdout was not valid UTF-8 NDJSON.")
            return
        if not isinstance(event, dict):
            self._fail("malformed_worker_output", "Each worker event must be a JSON object.")
            return
        self.events.append(event)

    def _fail(self, code: str, message: str) -> None:
        if self.error is None:
            self.error = ExecutorError(code, message)
        self.limit_reached.set()


class _LogCollector:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._captured = bytearray()
        self.total_bytes = 0
        self.limit_reached = threading.Event()

    def consume(self, stream: Any) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            self.total_bytes += len(chunk)
            remaining = self._limit - len(self._captured)
            if remaining > 0:
                self._captured.extend(chunk[:remaining])
            if self.total_bytes > self._limit:
                self.limit_reached.set()

    def text(self) -> str:
        return self._captured.decode("utf-8", errors="replace")

    @property
    def captured_bytes(self) -> int:
        return len(self._captured)


def _read_positive_environment(name: str, default: int) -> int:
    serialized = os.environ.get(name)
    if serialized is None or serialized == "":
        return default
    try:
        value = int(serialized)
    except ValueError as error:
        raise ExecutorError("invalid_executor_limit", f"{name} must be a positive integer.") from error
    if value <= 0:
        raise ExecutorError("invalid_executor_limit", f"{name} must be a positive integer.")
    return value


def limits_from_environment() -> ExecutorLimits:
    defaults = ExecutorLimits()
    return ExecutorLimits(
        wall_time_ms=_read_positive_environment("ORCA_WEB_MAX_WALL_TIME_MS", defaults.wall_time_ms),
        cpu_time_ms=_read_positive_environment("ORCA_WEB_MAX_CPU_TIME_MS", defaults.cpu_time_ms),
        memory_bytes=_read_positive_environment("ORCA_WEB_MAX_MEMORY_BYTES", defaults.memory_bytes),
        output_bytes=_read_positive_environment("ORCA_WEB_MAX_OUTPUT_BYTES", defaults.output_bytes),
        process_count=_read_positive_environment("ORCA_WEB_MAX_PROCESSES", defaults.process_count),
        open_files=_read_positive_environment("ORCA_WEB_MAX_OPEN_FILES", defaults.open_files),
        termination_grace_ms=_read_positive_environment(
            "ORCA_WEB_TERMINATION_GRACE_MS", defaults.termination_grace_ms
        ),
        event_bytes=_read_positive_environment("ORCA_WEB_MAX_EVENT_BYTES", defaults.event_bytes),
        event_line_bytes=_read_positive_environment(
            "ORCA_WEB_MAX_EVENT_LINE_BYTES", defaults.event_line_bytes
        ),
        event_count=_read_positive_environment("ORCA_WEB_MAX_EVENTS", defaults.event_count),
        log_bytes=_read_positive_environment("ORCA_WEB_MAX_LOG_BYTES", defaults.log_bytes),
    )


def _linux_limit_setup(limits: ExecutorLimits) -> Callable[[], None]:
    if sys.platform != "linux":
        raise ExecutorError(
            "unsupported_executor_platform",
            "Hard worker limits currently require the canonical Linux container runtime.",
        )

    import resource

    cpu_seconds = max(1, math.ceil(limits.cpu_time_ms / 1000))

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.output_bytes, limits.output_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.process_count, limits.process_count))
        resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _load_job_id(manifest_path: Path) -> str:
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ExecutorError(
                "executor_manifest_too_large", "Executor manifest exceeds the 1 MiB limit."
            )
        envelope = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except ExecutorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutorError(
            "executor_manifest_unreadable", "Executor could not read the worker manifest."
        ) from error
    job_id = envelope.get("job_id") if isinstance(envelope, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise ExecutorError("executor_manifest_invalid", "Executor manifest has no usable job_id.")
    return job_id


def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _stop_process(process: subprocess.Popen[bytes], grace_ms: int) -> bool:
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_ms / 1000)
        return False
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        process.wait()
        return True


def _kill_remaining_process_group(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    _signal_process_group(process, signal.SIGKILL)
    return True


def _artifact_path(job_root: Path, relative: Any) -> Optional[Path]:
    if not isinstance(relative, str) or not relative:
        return None
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    unresolved = job_root / candidate
    current = job_root
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            return None
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(job_root.resolve())
    except ValueError:
        return None
    return resolved


def _validate_completion(
    events: Sequence[Dict[str, Any]],
    job_id: str,
    job_root: Path,
    exit_code: int,
    max_result_bytes: int,
) -> Dict[str, Any]:
    if not events:
        raise ExecutorError("missing_worker_events", "Worker emitted no protocol events.")
    for index, event in enumerate(events):
        if event.get("protocol_version") != 1:
            raise ExecutorError("malformed_worker_output", "Worker event protocol_version is invalid.")
        if event.get("job_id") != job_id:
            raise ExecutorError("malformed_worker_output", "Worker event job_id does not match the request.")
        if event.get("sequence") != index:
            raise ExecutorError("malformed_worker_output", "Worker event sequence is not contiguous.")

    terminal = [
        event
        for event in events
        if event.get("type") == "state" and event.get("state") in TERMINAL_STATES
    ]
    if len(terminal) != 1 or terminal[0] is not events[-1]:
        raise ExecutorError("missing_terminal_event", "Worker did not emit exactly one final terminal state.")

    result_path = job_root / "result.json"
    try:
        if result_path.is_symlink() or result_path.stat().st_size > max_result_bytes:
            raise ExecutorError(
                "malformed_worker_result", "Worker result.json is unsafe or exceeds its size limit."
            )
        result = json.loads(result_path.read_bytes().decode("utf-8"))
    except ExecutorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutorError(
            "missing_worker_result", "Worker did not publish a valid result.json."
        ) from error
    if not isinstance(result, dict) or result.get("protocol_version") != 1 or result.get("job_id") != job_id:
        raise ExecutorError("malformed_worker_result", "Worker result envelope does not match the request.")
    if result.get("outcome") != terminal[0].get("state"):
        raise ExecutorError(
            "worker_result_mismatch", "Worker result outcome does not match its terminal event."
        )

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        raise ExecutorError("malformed_worker_result", "Worker result artifacts must be an array.")
    if result.get("outcome") != "succeeded" and artifacts:
        raise ExecutorError("worker_result_mismatch", "A non-successful worker result published artifacts.")
    for artifact in artifacts:
        path = _artifact_path(job_root, artifact.get("path") if isinstance(artifact, dict) else None)
        if path is None or not path.is_file() or path.is_symlink():
            raise ExecutorError("missing_worker_artifact", "A declared worker artifact is missing or unsafe.")
        if artifact.get("size_bytes") != path.stat().st_size:
            raise ExecutorError(
                "worker_artifact_mismatch", "A worker artifact size does not match result.json."
            )

    expected_exit = {"succeeded": 0, "canceled": 6}
    outcome = result.get("outcome")
    if outcome in expected_exit and exit_code != expected_exit[outcome]:
        raise ExecutorError("worker_exit_mismatch", "Worker exit code does not match its result outcome.")
    if outcome == "failed" and exit_code not in {4, 5, 7, 8}:
        raise ExecutorError("worker_exit_mismatch", "Worker failure used an unexpected exit code.")
    return result


def execute_worker(
    worker_command: Sequence[str],
    manifest_path: Path,
    limits: ExecutorLimits,
    cancellation_requested: Optional[threading.Event] = None,
) -> ExecutionResult:
    limits.validate()
    if not worker_command:
        raise ExecutorError("invalid_worker_command", "Worker command cannot be empty.")
    manifest_path = manifest_path.resolve()
    job_root = manifest_path.parent
    job_id = _load_job_id(manifest_path)
    if (job_root / "result.json").exists() or (job_root / "result.json").is_symlink():
        raise ExecutorError(
            "job_directory_not_fresh", "Executor job directory already contains result.json."
        )
    preexec_fn = _linux_limit_setup(limits)

    try:
        process = subprocess.Popen(
            [*worker_command, "--slice-manifest", str(manifest_path)],
            cwd=job_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExecutorError("worker_launch_failed", "Executor could not launch the worker.") from error

    assert process.stdout is not None
    assert process.stderr is not None
    protocol = _ProtocolCollector(limits)
    logs = _LogCollector(limits.log_bytes)
    stdout_thread = threading.Thread(target=protocol.consume, args=(process.stdout,), daemon=True)
    stderr_thread = threading.Thread(target=logs.consume, args=(process.stderr,), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    started = time.monotonic()
    termination_code: Optional[str] = None
    termination_message: Optional[str] = None
    forced = False
    while process.poll() is None:
        if protocol.limit_reached.is_set():
            assert protocol.error is not None
            termination_code = protocol.error.code
            termination_message = str(protocol.error)
            break
        if logs.limit_reached.is_set():
            termination_code = "log_volume_limit_exceeded"
            termination_message = "Worker log output exceeded the configured byte limit."
            break
        if cancellation_requested is not None and cancellation_requested.is_set():
            termination_code = "executor_canceled"
            termination_message = "The executor was asked to cancel the worker."
            break
        if (time.monotonic() - started) * 1000 >= limits.wall_time_ms:
            termination_code = "executor_wall_time_exceeded"
            termination_message = "Worker exceeded the executor wall-time limit."
            break
        time.sleep(0.01)

    if termination_code is not None and process.poll() is None:
        forced = _stop_process(process, limits.termination_grace_ms)
    else:
        process.wait()
    descendants_killed = _kill_remaining_process_group(process)
    if descendants_killed:
        forced = True
        if termination_code is None:
            termination_code = "worker_left_descendants"
            termination_message = "Worker exited while child processes were still running."

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        termination_code = termination_code or "worker_pipe_close_failed"
        termination_message = termination_message or "Worker output pipes did not close after termination."
    elif termination_code is None and protocol.error is not None:
        termination_code = protocol.error.code
        termination_message = str(protocol.error)
    elif termination_code is None and logs.limit_reached.is_set():
        termination_code = "log_volume_limit_exceeded"
        termination_message = "Worker log output exceeded the configured byte limit."

    stderr = logs.text()
    if termination_code is not None:
        status = {
            "executor_canceled": "canceled",
            "executor_wall_time_exceeded": "timed_out",
        }.get(termination_code, "output_limit" if "limit" in termination_code else "failed")
        return ExecutionResult(
            status=status,
            worker_exit_code=process.returncode,
            forced_termination=forced,
            events=protocol.events,
            result=None,
            event_bytes=protocol.total_bytes,
            log_bytes=logs.total_bytes,
            captured_log_bytes=logs.captured_bytes,
            stderr=stderr,
            error_code=termination_code,
            error_message=termination_message,
        )

    try:
        result = _validate_completion(
            protocol.events, job_id, job_root, process.returncode, limits.event_bytes
        )
    except ExecutorError as error:
        return ExecutionResult(
            status="failed",
            worker_exit_code=process.returncode,
            forced_termination=False,
            events=protocol.events,
            result=None,
            event_bytes=protocol.total_bytes,
            log_bytes=logs.total_bytes,
            captured_log_bytes=logs.captured_bytes,
            stderr=stderr,
            error_code=error.code,
            error_message=str(error),
        )

    return ExecutionResult(
        status="completed",
        worker_exit_code=process.returncode,
        forced_termination=False,
        events=protocol.events,
        result=result,
        event_bytes=protocol.total_bytes,
        log_bytes=logs.total_bytes,
        captured_log_bytes=logs.captured_bytes,
        stderr=stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    cancellation = threading.Event()

    def request_cancellation(_signal: int, _frame: Any) -> None:
        cancellation.set()

    signal.signal(signal.SIGINT, request_cancellation)
    signal.signal(signal.SIGTERM, request_cancellation)

    try:
        execution = execute_worker(
            [str(args.worker.resolve())], args.manifest, limits_from_environment(), cancellation
        )
    except ExecutorError as error:
        print(json.dumps({"status": "failed", "error": {"code": error.code, "message": str(error)}}))
        return 2

    print(json.dumps(execution.summary(), separators=(",", ":")))
    return 0 if execution.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
