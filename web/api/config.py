"""Everything one API instance needs to run, resolved from the environment once."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional, Tuple

from web_job_directory import JobDirectoryLimits, limits_from_environment as job_limits_from_environment
from web_profile_catalog import vendors_from_environment
from web_worker_executor import ExecutorLimits, limits_from_environment as executor_limits_from_environment

from . import REPO_ROOT
from .errors import ApiError


DEFAULT_WORKER = Path("build-worker") / "src" / "Release" / "orca-slicer-worker"


@dataclasses.dataclass(frozen=True)
class ApiConfig:
    repo_root: Path
    state_root: Path
    worker_command: Tuple[str, ...]
    profile_vendors: Tuple[str, ...]
    max_concurrent_jobs: int = 2
    executor_limits: ExecutorLimits = dataclasses.field(default_factory=ExecutorLimits)
    job_limits: JobDirectoryLimits = dataclasses.field(default_factory=JobDirectoryLimits)
    # Serving the built browser screen from the API keeps the app on one origin.
    # When it is absent the API is a bare JSON service and the Vite dev server
    # proxies to it instead.
    frontend_dist: Optional[Path] = None

    @property
    def uploads_root(self) -> Path:
        return self.state_root / "uploads"

    @property
    def jobs_root(self) -> Path:
        return self.state_root / "jobs"

    def validate(self) -> None:
        if self.max_concurrent_jobs <= 0:
            raise ApiError("invalid_api_configuration", "max_concurrent_jobs must be positive.", 500)
        if not self.worker_command:
            raise ApiError("invalid_api_configuration", "A worker command must be configured.", 500)
        self.executor_limits.validate()
        self.job_limits.validate()


def from_environment() -> ApiConfig:
    repo_root = Path(os.environ.get("ORCA_WEB_REPO_ROOT") or REPO_ROOT).resolve()
    state_root = Path(os.environ.get("ORCA_WEB_STATE_ROOT") or repo_root / "build" / "web-state")
    worker = os.environ.get("ORCA_WEB_WORKER") or str(repo_root / DEFAULT_WORKER)
    concurrency = os.environ.get("ORCA_WEB_MAX_CONCURRENT_JOBS", "")
    try:
        max_concurrent_jobs = int(concurrency) if concurrency else 2
    except ValueError as error:
        raise ApiError(
            "invalid_api_configuration", "ORCA_WEB_MAX_CONCURRENT_JOBS must be a positive integer.", 500
        ) from error

    dist = Path(os.environ.get("ORCA_WEB_FRONTEND_DIST") or repo_root / "web" / "frontend" / "dist")
    config = ApiConfig(
        repo_root=repo_root,
        state_root=state_root,
        worker_command=(worker,),
        profile_vendors=vendors_from_environment(),
        max_concurrent_jobs=max_concurrent_jobs,
        executor_limits=executor_limits_from_environment(),
        job_limits=job_limits_from_environment(),
        frontend_dist=dist if dist.is_dir() else None,
    )
    config.validate()
    return config
