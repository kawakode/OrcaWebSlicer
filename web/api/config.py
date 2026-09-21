"""Everything one API instance needs to run, resolved from the environment once."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional, Tuple

from web_job_directory import JobDirectoryLimits, limits_from_environment as job_limits_from_environment
from web_profile_catalog import vendors_from_environment
from web_worker_executor import ExecutorLimits, limits_from_environment as executor_limits_from_environment
from web_worker_sandbox import SandboxError, SandboxPolicy, policy_from_environment

from . import REPO_ROOT
from .auth import AUTH_MODE_REQUIRED, AUTH_MODES
from .errors import ApiError
from .quotas import QuotaLimits, limits_from_environment as quota_limits_from_environment


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
    sandbox: SandboxPolicy = dataclasses.field(default_factory=SandboxPolicy)
    quotas: QuotaLimits = dataclasses.field(default_factory=QuotaLimits)
    # Serving the built browser screen from the API keeps the app on one origin.
    # When it is absent the API is a bare JSON service and the Vite dev server
    # proxies to it instead.
    frontend_dist: Optional[Path] = None
    # Secure by default: every deployment that does not explicitly opt into
    # `disabled` must supply a working verifier before it will start.
    auth_mode: str = AUTH_MODE_REQUIRED
    auth_issuer: Optional[str] = None
    auth_audience: Optional[str] = None
    auth_jwks_path: Optional[Path] = None

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
        try:
            self.sandbox.validate()
        except SandboxError as error:
            raise ApiError("invalid_api_configuration", str(error), 500) from error
        self.quotas.validate()
        if self.quotas.max_storage_bytes < self.job_limits.max_input_bytes:
            # Otherwise the largest upload the service accepts could never fit.
            raise ApiError(
                "invalid_api_configuration",
                "the storage quota must be at least the maximum input size.",
                500,
            )
        self._validate_auth()

    def _validate_auth(self) -> None:
        if self.auth_mode not in AUTH_MODES:
            raise ApiError(
                "invalid_api_configuration",
                f"ORCA_WEB_AUTH_MODE must be one of {', '.join(AUTH_MODES)}.",
                500,
            )
        if self.auth_mode != AUTH_MODE_REQUIRED:
            return
        if not self.auth_issuer:
            raise ApiError(
                "invalid_api_configuration", "required auth mode needs ORCA_WEB_AUTH_ISSUER.", 500
            )
        if not self.auth_audience:
            raise ApiError(
                "invalid_api_configuration", "required auth mode needs ORCA_WEB_AUTH_AUDIENCE.", 500
            )
        if self.auth_jwks_path is None:
            raise ApiError(
                "invalid_api_configuration", "required auth mode needs ORCA_WEB_AUTH_JWKS_PATH.", 500
            )
        if not self.auth_jwks_path.is_file():
            raise ApiError(
                "invalid_api_configuration",
                f"the configured JWKS file does not exist: {self.auth_jwks_path}",
                500,
            )


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
    try:
        sandbox = policy_from_environment()
    except SandboxError as error:
        raise ApiError("invalid_api_configuration", str(error), 500) from error

    jwks_env = os.environ.get("ORCA_WEB_AUTH_JWKS_PATH")
    config = ApiConfig(
        repo_root=repo_root,
        state_root=state_root,
        worker_command=(worker,),
        profile_vendors=vendors_from_environment(),
        max_concurrent_jobs=max_concurrent_jobs,
        executor_limits=executor_limits_from_environment(),
        job_limits=job_limits_from_environment(),
        sandbox=sandbox,
        quotas=quota_limits_from_environment(),
        frontend_dist=dist if dist.is_dir() else None,
        auth_mode=os.environ.get("ORCA_WEB_AUTH_MODE") or AUTH_MODE_REQUIRED,
        auth_issuer=os.environ.get("ORCA_WEB_AUTH_ISSUER") or None,
        auth_audience=os.environ.get("ORCA_WEB_AUTH_AUDIENCE") or None,
        auth_jwks_path=Path(jwks_env).resolve() if jwks_env else None,
    )
    config.validate()
    return config
