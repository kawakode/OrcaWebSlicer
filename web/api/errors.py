"""Stable API failures and the mapping from lower-layer codes onto HTTP status."""

from __future__ import annotations

from typing import Dict

from web_job_directory import JobDirectoryError
from web_profile_catalog import ProfileCatalogError
from web_settings_catalog import SettingsCatalogError


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
    "unreadable_compatibility_result": 500,
}

# A catalog the worker could not produce is a service-side gap, not a bad
# request, so it is reported as unavailable rather than blamed on the caller.
_SETTINGS_STATUS: Dict[str, int] = {
    "unknown_setting": 422,
    "unavailable_setting": 422,
    "invalid_setting_value": 422,
    "settings_catalog_unavailable": 503,
    "compatibility_unavailable": 503,
    "unreadable_settings_catalog": 500,
    "unsupported_settings_catalog_version": 500,
    "unreadable_compatibility_result": 500,
}


def from_job_directory_error(error: JobDirectoryError) -> ApiError:
    return ApiError(error.code, str(error), _JOB_DIRECTORY_STATUS.get(error.code, 400))


def from_catalog_error(error: ProfileCatalogError) -> ApiError:
    return ApiError(error.code, str(error), _CATALOG_STATUS.get(error.code, 400))


def from_settings_error(error: SettingsCatalogError) -> ApiError:
    return ApiError(error.code, str(error), _SETTINGS_STATUS.get(error.code, 400))
