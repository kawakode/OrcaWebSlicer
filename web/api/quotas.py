"""Per-owner quotas: concurrent jobs, stored bytes, worker CPU time, and submissions.

Every quota is keyed by the opaque `owner_id` ADR 0004 derives from the
assertion's issuer and subject. The ledger itself lives in memory; after a
restart `JobService` replays the submissions and CPU charges its persisted job
records carry. Stored bytes are measured from what is on disk (and from the
jobs the service holds) rather than remembered.

Quotas are admission checks. A job admitted under budget runs to its own
executor limits, so one owner can overshoot the CPU budget by at most one
job's CPU limit per concurrent slot, and the storage budget by at most the
executor's output limit per concurrent slot.
"""

from __future__ import annotations

import dataclasses
import math
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Tuple, Type, TypeVar

from .errors import ApiError


QUOTA_STATUS = 429
_Limits = TypeVar("_Limits")


@dataclasses.dataclass(frozen=True)
class QuotaLimits:
    max_active_jobs: int = 2
    max_storage_bytes: int = 5 * 1024 * 1024 * 1024
    # Worker CPU time and job submissions are budgeted over one rolling window.
    cpu_time_ms: int = 3_600_000
    max_submissions: int = 120
    window_seconds: int = 3_600

    def validate(self) -> None:
        validate_positive_ints(self, "quota")

    def describe(self) -> Dict[str, int]:
        return dataclasses.asdict(self)


_ENVIRONMENT = {
    "max_active_jobs": "ORCA_WEB_QUOTA_ACTIVE_JOBS",
    "max_storage_bytes": "ORCA_WEB_QUOTA_STORAGE_BYTES",
    "cpu_time_ms": "ORCA_WEB_QUOTA_CPU_TIME_MS",
    "max_submissions": "ORCA_WEB_QUOTA_SUBMISSIONS",
    "window_seconds": "ORCA_WEB_QUOTA_WINDOW_SECONDS",
}


def validate_positive_ints(limits: Any, label: str) -> None:
    """Every field of a limits dataclass must be a positive integer."""
    for field in dataclasses.fields(limits):
        value = getattr(limits, field.name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ApiError("invalid_api_configuration", f"{label} {field.name} must be a positive integer.", 500)


def positive_ints_from_environment(cls: Type[_Limits], variables: Dict[str, str]) -> _Limits:
    """Build a limits dataclass from its environment variables; unset keeps a default."""
    values = {}
    for field, variable in variables.items():
        raw = os.environ.get(variable, "")
        if not raw:
            continue
        try:
            values[field] = int(raw)
        except ValueError as error:
            raise ApiError("invalid_api_configuration", f"{variable} must be a positive integer.", 500) from error
    limits = cls(**values)
    limits.validate()
    return limits


def limits_from_environment() -> QuotaLimits:
    return positive_ints_from_environment(QuotaLimits, _ENVIRONMENT)


def quota_error(code: str, message: str, retry_after: float = 0.0) -> ApiError:
    error = ApiError(code, message, QUOTA_STATUS)
    if retry_after > 0:
        error.retry_after = max(1, math.ceil(retry_after))
    return error


class QuotaLedger:
    """Rolling per-owner CPU charges, submissions, and in-flight upload reservations.

    Callers that must make a check and a mutation atomic with their own state
    (the job table) hold their own lock around both; this lock only keeps the
    ledger's own structures consistent, and is always taken last.
    """

    def __init__(self, limits: QuotaLimits, clock: Callable[[], float] = time.monotonic) -> None:
        limits.validate()
        self.limits = limits
        self._clock = clock
        self._lock = threading.Lock()
        self._cpu: Dict[str, Deque[Tuple[float, int]]] = {}
        self._submissions: Dict[str, Deque[float]] = {}
        self._reserved: Dict[str, int] = {}

    def usage(self, owner_id: str) -> Tuple[int, int, int]:
        """(CPU milliseconds in the window, submissions in the window, reserved bytes)."""
        with self._lock:
            self._expire(owner_id)
            cpu = sum(charge for _, charge in self._cpu.get(owner_id, ()))
            return cpu, len(self._submissions.get(owner_id, ())), self._reserved.get(owner_id, 0)

    def check_submission(self, owner_id: str, active_jobs: int) -> None:
        """Refuse a new job when any per-owner job budget is already spent."""
        limits = self.limits
        if active_jobs >= limits.max_active_jobs:
            raise quota_error(
                "concurrent_job_quota_exceeded",
                f"At most {limits.max_active_jobs} jobs may be queued or running at once.",
            )
        with self._lock:
            now = self._expire(owner_id)
            submissions = self._submissions.get(owner_id, deque())
            if len(submissions) >= limits.max_submissions:
                raise quota_error(
                    "job_submission_quota_exceeded",
                    f"At most {limits.max_submissions} jobs may be submitted per {limits.window_seconds} seconds.",
                    submissions[0] + limits.window_seconds - now,
                )
            charges = self._cpu.get(owner_id, deque())
            spent = sum(charge for _, charge in charges)
            if spent >= limits.cpu_time_ms:
                # The window frees enough budget once the oldest charges that
                # push it over have aged out.
                excess = spent - limits.cpu_time_ms
                released = 0
                wait = 0.0
                for timestamp, charge in charges:
                    released += charge
                    wait = timestamp + limits.window_seconds - now
                    if released > excess:
                        break
                raise quota_error(
                    "cpu_time_quota_exceeded",
                    f"The worker CPU time budget of {limits.cpu_time_ms} ms per "
                    f"{limits.window_seconds} seconds is spent.",
                    wait,
                )

    # `age_seconds` backdates an entry that is being restored from job metadata
    # after a restart. Restored entries must be replayed oldest first, because
    # each window expires from its front.

    def record_submission(self, owner_id: str, age_seconds: float = 0.0) -> None:
        with self._lock:
            self._submissions.setdefault(owner_id, deque()).append(self._clock() - age_seconds)

    def charge_cpu(self, owner_id: str, cpu_time_ms: int, age_seconds: float = 0.0) -> None:
        if cpu_time_ms <= 0:
            return
        with self._lock:
            self._cpu.setdefault(owner_id, deque()).append((self._clock() - age_seconds, int(cpu_time_ms)))

    def reserve_storage(self, owner_id: str, stored_bytes: int, requested: int) -> int:
        """Reserve up to `requested` bytes of what remains of the storage budget.

        `stored_bytes` is what the caller measured as already held. Returns the
        reservation, which the caller must release with `release_storage`.
        """
        with self._lock:
            reserved = self._reserved.get(owner_id, 0)
            remaining = self.limits.max_storage_bytes - stored_bytes - reserved
            if remaining <= 0:
                raise storage_quota_error(self.limits)
            granted = min(remaining, requested)
            self._reserved[owner_id] = reserved + granted
            return granted

    def release_storage(self, owner_id: str, granted: int) -> None:
        with self._lock:
            remaining = self._reserved.get(owner_id, 0) - granted
            if remaining > 0:
                self._reserved[owner_id] = remaining
            else:
                self._reserved.pop(owner_id, None)

    def _expire(self, owner_id: str) -> float:
        now = self._clock()
        horizon = now - self.limits.window_seconds
        charges = self._cpu.get(owner_id)
        if charges is not None:
            while charges and charges[0][0] <= horizon:
                charges.popleft()
            if not charges:
                del self._cpu[owner_id]
        submissions = self._submissions.get(owner_id)
        if submissions is not None:
            while submissions and submissions[0] <= horizon:
                submissions.popleft()
            if not submissions:
                del self._submissions[owner_id]
        return now


def storage_quota_error(limits: QuotaLimits) -> ApiError:
    # No Retry-After: stored bytes are reclaimed by retention, not by a window.
    return quota_error(
        "storage_quota_exceeded",
        f"Uploads and job artifacts may hold at most {limits.max_storage_bytes} bytes per owner.",
    )
