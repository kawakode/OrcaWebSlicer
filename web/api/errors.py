"""Stable API failures and the mapping from lower-layer codes onto HTTP status."""

from __future__ import annotations

from typing import Dict

from web_job_directory import JobDirectoryError
from web_profile_catalog import ProfileCatalogError


class ApiError(RuntimeError):
    """A failure the API can return verbatim: a stable code and a safe message."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# Lower layers already produce stable codes. Only the codes whose HTTP meaning
# differs from a plain client error are listed; anything else is a 400, and an
# unrecognised code from a layer that should not fail is a 500.
_JOB_DIRECTORY_STATUS: Dict[str, int] = {
    "job_input_size_limit_exceeded": 413,
    "manifest_too_large": 413,
    "job_directory_exists": 409,
    "job_root_unavailable": 500,
    "job_directory_unavailable": 500,
    "job_input_unreadable": 500,
}

_CATALOG_STATUS: Dict[str, int] = {
    "unknown_profile": 404,
    "unknown_profile_vendor": 404,
    "unknown_profile_kind": 404,
    "incompatible_profile": 409,
    "unreadable_profile": 500,
    "duplicate_profile_id": 500,
}


def from_job_directory_error(error: JobDirectoryError) -> ApiError:
    return ApiError(error.code, str(error), _JOB_DIRECTORY_STATUS.get(error.code, 400))


def from_catalog_error(error: ProfileCatalogError) -> ApiError:
    return ApiError(error.code, str(error), _CATALOG_STATUS.get(error.code, 400))
