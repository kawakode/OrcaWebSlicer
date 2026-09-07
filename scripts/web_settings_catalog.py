#!/usr/bin/env python3
"""Read the engine's curated setting metadata and validate overrides against it.

The catalog is produced by `orca-slicer-worker --export-settings-catalog`, which
serializes `PrintConfigDef` itself. Nothing here restates a type, a range, an
enum, or a default: this module only transports what the engine declared and
checks a submitted override against it so an obvious mistake is refused with a
stable code instead of costing a worker process.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


SETTINGS_CATALOG_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 60.0
# The export is a fixed-size document; anything larger means a broken worker.
MAX_CATALOG_BYTES = 4 * 1024 * 1024

_NUMERIC_TYPES = frozenset({"float", "floats", "int", "ints", "percent", "percents"})
_PERCENT_TYPES = frozenset({"percent", "percents", "float_or_percent", "floats_or_percents"})
_INTEGER_TYPES = frozenset({"int", "ints"})
_BOOLEAN_TYPES = frozenset({"bool", "bools"})
_ENUM_TYPES = frozenset({"enum", "enums"})
_BOOLEAN_LITERALS = frozenset({"0", "1", "true", "false", "yes", "no"})


class SettingsCatalogError(RuntimeError):
    """A stable settings failure that is safe to return to the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class SettingsCatalog:
    """The curated setting definitions one API instance offers."""

    catalog_version: int
    engine_version: str
    groups: Tuple[Dict[str, Any], ...]
    settings: Tuple[Dict[str, Any], ...]

    def __len__(self) -> int:
        return len(self.settings)

    def describe(self) -> Dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "engine_version": self.engine_version,
            "groups": [dict(group) for group in self.groups],
            "settings": [dict(setting) for setting in self.settings],
        }

    def definition(self, key: str) -> Optional[Dict[str, Any]]:
        for setting in self.settings:
            if setting["key"] == key:
                return setting
        return None

    def validate_overrides(self, overrides: Mapping[str, str]) -> None:
        """Refuse an override the engine's own definition cannot accept.

        The worker still validates everything it is given; this check only turns
        the common mistakes into an immediate, categorized answer.
        """
        for key, value in overrides.items():
            definition = self.definition(key)
            if definition is None:
                raise SettingsCatalogError(
                    "unknown_setting", f"{key} is not one of the settings this deployment exposes."
                )
            if definition.get("missing"):
                raise SettingsCatalogError(
                    "unavailable_setting", f"{key} is not available in this engine build."
                )
            _validate_value(definition, value)


def _fail(key: str, message: str) -> None:
    raise SettingsCatalogError("invalid_setting_value", f"{key}: {message}")


def _validate_value(definition: Mapping[str, Any], value: str) -> None:
    key = definition["key"]
    kind = definition.get("type", "")
    if not value:
        _fail(key, "a value is required.")
    # A vector option is serialized as one comma-separated string, so every
    # element is held to the scalar rule.
    elements = value.split(",") if definition.get("vector") else [value]
    if definition.get("vector") and len(elements) > 64:
        _fail(key, "at most 64 values may be given.")
    for element in elements:
        _validate_element(definition, key, kind, element.strip())


def _validate_element(definition: Mapping[str, Any], key: str, kind: str, element: str) -> None:
    if not element:
        _fail(key, "an empty value is not allowed.")
    if kind in _BOOLEAN_TYPES:
        if element.lower() not in _BOOLEAN_LITERALS:
            _fail(key, "expected 0 or 1.")
        return
    if kind in _ENUM_TYPES:
        allowed = [item["value"] for item in definition.get("enum", [])]
        if allowed and element not in allowed:
            _fail(key, "expected one of " + ", ".join(allowed) + ".")
        return
    if kind not in _NUMERIC_TYPES and kind not in _PERCENT_TYPES:
        return

    numeric = element
    is_percent = numeric.endswith("%")
    if is_percent:
        if kind not in _PERCENT_TYPES:
            _fail(key, "a percentage is not accepted here.")
        numeric = numeric[:-1]
    try:
        parsed = float(numeric)
    except ValueError:
        _fail(key, "expected a number." if not is_percent else "expected a percentage.")
        return
    if kind in _INTEGER_TYPES and parsed != int(parsed):
        _fail(key, "expected a whole number.")
    # A ratio expressed as a percentage is bounded by the setting it is a ratio
    # over, not by this setting's own absolute range.
    if is_percent and definition.get("ratio_over"):
        return
    minimum = definition.get("min")
    maximum = definition.get("max")
    if minimum is not None and parsed < minimum:
        _fail(key, f"must be at least {_number(minimum)}{definition.get('unit', '')}.")
    if maximum is not None and parsed > maximum:
        _fail(key, f"must be at most {_number(maximum)}{definition.get('unit', '')}.")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def parse_settings_catalog(serialized: str) -> SettingsCatalog:
    try:
        document = json.loads(serialized)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SettingsCatalogError(
            "unreadable_settings_catalog", f"The engine settings catalog could not be read: {error}"
        ) from error
    if not isinstance(document, dict):
        raise SettingsCatalogError("unreadable_settings_catalog", "The settings catalog is not an object.")
    if document.get("catalog_version") != SETTINGS_CATALOG_VERSION:
        raise SettingsCatalogError(
            "unsupported_settings_catalog_version",
            f"The worker exports settings catalog version {document.get('catalog_version')!r}, "
            f"but this API speaks version {SETTINGS_CATALOG_VERSION}.",
        )

    groups = document.get("groups")
    settings = document.get("settings")
    if not isinstance(groups, list) or not isinstance(settings, list) or not settings:
        raise SettingsCatalogError(
            "unreadable_settings_catalog", "The settings catalog declares no groups or no settings."
        )
    group_ids = {group.get("id") for group in groups if isinstance(group, dict)}
    seen = set()
    for setting in settings:
        if not isinstance(setting, dict) or not isinstance(setting.get("key"), str):
            raise SettingsCatalogError("unreadable_settings_catalog", "A setting declares no key.")
        if setting["key"] in seen:
            raise SettingsCatalogError(
                "unreadable_settings_catalog", f"The settings catalog lists {setting['key']} twice."
            )
        seen.add(setting["key"])
        if setting.get("group") not in group_ids:
            raise SettingsCatalogError(
                "unreadable_settings_catalog",
                f"{setting['key']} belongs to the undeclared group {setting.get('group')!r}.",
            )
    return SettingsCatalog(
        catalog_version=SETTINGS_CATALOG_VERSION,
        engine_version=str(document.get("engine_version", "")),
        groups=tuple(dict(group) for group in groups if isinstance(group, dict)),
        settings=tuple(dict(setting) for setting in settings),
    )


def load_settings_catalog(
    worker_command: Sequence[str], timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> SettingsCatalog:
    """Ask the worker for the engine's curated setting metadata.

    This runs once per API process against a trusted executable with no job
    input, so it needs none of the isolation a slicing job does.
    """
    serialized = _run_worker(
        list(worker_command) + ["--export-settings-catalog"], None, timeout_seconds, "settings_catalog"
    )
    return parse_settings_catalog(serialized)


def evaluate_compatibility(
    worker_command: Sequence[str],
    request_path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Resolve `compatible_printers_condition` expressions through the engine."""
    serialized = _run_worker(
        list(worker_command) + ["--evaluate-compatibility", request_path], None, timeout_seconds, "compatibility"
    )
    try:
        document = json.loads(serialized)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SettingsCatalogError(
            "unreadable_compatibility_result", f"The compatibility result could not be read: {error}"
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("compatibility"), dict):
        raise SettingsCatalogError(
            "unreadable_compatibility_result", "The compatibility result declares no compatibility map."
        )
    return document


def _run_worker(command: Sequence[str], stdin: Optional[bytes], timeout_seconds: float, subject: str) -> str:
    try:
        completed = subprocess.run(
            list(command),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except OSError as error:
        raise SettingsCatalogError(
            f"{subject}_unavailable", f"The slicing worker could not be started: {error}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SettingsCatalogError(
            f"{subject}_unavailable", "The slicing worker did not answer in time."
        ) from error
    if completed.returncode != 0:
        # stderr carries the worker's own stable error document; its first line
        # is enough to name the failure without echoing an unbounded log.
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SettingsCatalogError(
            f"{subject}_unavailable",
            f"The slicing worker refused the request: {detail[0] if detail else completed.returncode}",
        )
    if len(completed.stdout) > MAX_CATALOG_BYTES:
        raise SettingsCatalogError(f"{subject}_unavailable", "The slicing worker returned an oversized document.")
    return completed.stdout.decode("utf-8", "replace")


def curated_keys(catalog: SettingsCatalog) -> Iterable[str]:
    return (setting["key"] for setting in catalog.settings)
