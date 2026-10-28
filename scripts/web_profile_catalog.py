#!/usr/bin/env python3
"""Enumerate the bundled profiles the web API offers and resolve them for a job."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from web_baseline import resolve_profile


# Resolves `compatible_printers_condition` expressions. The engine owns the
# expression language, so the catalog is handed an evaluator rather than
# guessing what an expression means.
CompatibilityEvaluator = Callable[[Path], Dict[str, Any]]


DEFAULT_VENDORS: Tuple[str, ...] = ("Anycubic",)
KINDS: Tuple[str, ...] = ("machine", "process", "filament")
_LIST_KEY = {"machine": "machine_list", "process": "process_list", "filament": "filament_list"}


class ProfileCatalogError(RuntimeError):
    """A stable catalog failure that is safe to return to the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class ProfileEntry:
    """One user-selectable bundled profile, reduced to the fields the API serves."""

    profile_id: str
    kind: str
    name: str
    vendor: str
    source: Path
    printer_model: str = ""
    nozzle_diameter: str = ""
    default_process: str = ""
    # Empty means the profile declares no restriction and suits every printer.
    compatible_printers: Tuple[str, ...] = ()
    # A declared list wins over the expression, exactly as the desktop resolves
    # the two. Only a profile with an expression and no list needs the engine.
    compatible_condition: str = ""
    # The names of the inherited profiles this one flattens, root first, ending
    # with this profile. This is the effective chain the job report shows.
    inherits_chain: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        described: Dict[str, Any] = {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "name": self.name,
            "vendor": self.vendor,
            "inherits_chain": list(self.inherits_chain),
        }
        if self.kind == "machine":
            described["printer_model"] = self.printer_model
            described["nozzle_diameter"] = self.nozzle_diameter
            described["default_process"] = self.default_process
        return described


class _InheritanceIndex:
    """Locates an inherited profile inside one vendor's kind directory.

    A vendor may keep a shared base beside its children (`fdm_filament_pla`) or
    one directory above a branded subdirectory, so the sibling file wins and the
    rest of the tree is searched by file name.
    """

    def __init__(self, kind_root: Path) -> None:
        self._by_name: Dict[str, Path] = {}
        for path in sorted(kind_root.rglob("*.json")):
            self._by_name.setdefault(path.stem, path)

    def __call__(self, name: str, child: Path) -> Path:
        sibling = child.parent / f"{name}.json"
        if sibling.is_file():
            return sibling
        # Falling back to the sibling keeps the resolver's "does not exist"
        # message pointing at the path the profile actually asked for.
        return self._by_name.get(name, sibling)


class ProfileCatalog:
    """The bundled profiles one API instance offers, indexed for lookup."""

    # Read by `effective_values`, which never needs more than the curated form.
    MAX_EFFECTIVE_KEYS = 256

    def __init__(
        self,
        entries: Iterable[ProfileEntry],
        lookups: Optional[Dict[Tuple[str, str], "_InheritanceIndex"]] = None,
        condition_matches: Optional[Dict[str, FrozenSet[str]]] = None,
    ) -> None:
        self._lookups = lookups or {}
        # Profile id -> the machine profile ids its condition accepted. A
        # profile absent from this map declares no condition to resolve.
        self._condition_matches = dict(condition_matches or {})
        self._by_id: Dict[str, ProfileEntry] = {}
        self._by_kind: Dict[str, List[ProfileEntry]] = {kind: [] for kind in KINDS}
        for entry in entries:
            if entry.profile_id in self._by_id:
                raise ProfileCatalogError(
                    "duplicate_profile_id", f"Two bundled profiles share the id {entry.profile_id}."
                )
            self._by_id[entry.profile_id] = entry
            self._by_kind[entry.kind].append(entry)
        for kind in KINDS:
            self._by_kind[kind].sort(key=lambda entry: entry.name)

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, profile_id: str, kind: str) -> ProfileEntry:
        entry = self._by_id.get(profile_id)
        if entry is None:
            raise ProfileCatalogError("unknown_profile", f"No bundled profile is named {profile_id}.")
        if entry.kind != kind:
            raise ProfileCatalogError(
                "profile_kind_mismatch",
                f"Profile {profile_id} is a {entry.kind} profile, not a {kind} profile.",
            )
        return entry

    def list(self, kind: str, printer: Optional[ProfileEntry] = None) -> List[ProfileEntry]:
        if kind not in self._by_kind:
            raise ProfileCatalogError("unknown_profile_kind", f"{kind} is not a bundled profile kind.")
        entries = self._by_kind[kind]
        if printer is None or kind == "machine":
            return list(entries)
        return [entry for entry in entries if self.is_compatible(entry, printer)]

    def is_compatible(self, entry: ProfileEntry, printer: ProfileEntry) -> bool:
        """Report whether a process or filament profile suits a selected printer.

        A declared `compatible_printers` list wins over the expression, exactly
        as the desktop resolves the two. An expression the engine never
        evaluated is treated as compatible, which is also the desktop's answer
        for a condition it cannot parse.
        """
        if entry.compatible_printers:
            return printer.name in entry.compatible_printers
        matches = self._condition_matches.get(entry.profile_id)
        return matches is None or printer.profile_id in matches

    def select(self, machine_id: str, process_id: str, filament_id: str) -> Dict[str, ProfileEntry]:
        """Look up one machine, process, and filament and refuse a mismatched chain."""
        machine = self.get(machine_id, "machine")
        selected = {
            "machine": machine,
            "process": self.get(process_id, "process"),
            "filament": self.get(filament_id, "filament"),
        }
        for kind in ("process", "filament"):
            if not self.is_compatible(selected[kind], machine):
                raise ProfileCatalogError(
                    "incompatible_profile",
                    f"The selected {kind} profile is not compatible with {machine.name}.",
                )
        return selected

    def effective_values(
        self, selected: Dict[str, ProfileEntry], scopes: Mapping[str, str]
    ) -> Dict[str, Any]:
        """Return the flattened value the selected chain gives each named key.

        `scopes` maps a key to the profile kind that carries it, as the settings
        catalog declares, so a filament setting reads the filament chain even if
        a process happens to mention it. Keys no profile sets are omitted.
        """
        resolved = {kind: self.resolve(entry) for kind, entry in selected.items()}
        values: Dict[str, Any] = {}
        for key, scope in list(scopes.items())[: self.MAX_EFFECTIVE_KEYS]:
            kinds = (scope,) if scope in resolved else ("filament", "process", "machine")
            for kind in kinds:
                if key in resolved[kind]:
                    values[key] = resolved[kind][key]
                    break
        return values

    def resolve(self, entry: ProfileEntry) -> Dict[str, Any]:
        resolved, _ = _resolve(entry.source, self._lookups.get((entry.vendor, entry.kind)))
        return resolved

    def materialize(
        self, machine_id: str, process_id: str, filament_id: str, destination: Path
    ) -> Dict[str, Path]:
        """Resolve one selected chain into job-local profile files.

        Inheritance is flattened here so the worker never reads the bundled
        profile tree and never resolves a path outside its own job directory.
        """
        selected = self.select(machine_id, process_id, filament_id)
        destination.mkdir(parents=True, exist_ok=True)
        return {
            kind: _write_profile(self.resolve(entry), destination / f"{kind}.json")
            for kind, entry in selected.items()
        }


def _write_profile(resolved: Dict[str, Any], target: Path) -> Path:
    with target.open("w", encoding="utf-8") as stream:
        json.dump(resolved, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return target


def _resolve(
    source: Path,
    lookup: Optional["_InheritanceIndex"] = None,
    cache: Optional[Dict[Path, Tuple[Dict[str, Any], List[Path]]]] = None,
) -> Tuple[Dict[str, Any], List[Path]]:
    try:
        return resolve_profile(source, None, lookup, cache)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ProfileCatalogError(
            "unreadable_profile", f"A bundled profile could not be read: {error}"
        ) from error


def _vendor_index(profiles_root: Path, vendor: str) -> Dict[str, Any]:
    if not vendor or "/" in vendor or "\\" in vendor or vendor.startswith("."):
        raise ProfileCatalogError("invalid_profile_vendor", f"{vendor!r} is not a bundled vendor name.")
    path = profiles_root / f"{vendor}.json"
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileCatalogError(
            "unknown_profile_vendor", f"No bundled vendor is named {vendor}."
        ) from error
    if not isinstance(index, dict):
        raise ProfileCatalogError("invalid_profile_vendor", f"The {vendor} vendor index is not an object.")
    return index


def _entry_source(vendor_root: Path, sub_path: Any) -> Optional[Path]:
    if not isinstance(sub_path, str) or not sub_path:
        return None
    candidate = Path(sub_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    source = vendor_root / candidate
    return source if source.is_file() else None


def _first_value(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _compatible_printers(profile: Dict[str, Any]) -> Tuple[str, ...]:
    declared = profile.get("compatible_printers")
    if not isinstance(declared, list):
        return ()
    return tuple(item for item in declared if isinstance(item, str))


def load_catalog(
    repo_root: Path,
    vendors: Sequence[str] = DEFAULT_VENDORS,
    evaluator: Optional[CompatibilityEvaluator] = None,
) -> ProfileCatalog:
    """Read the bundled vendor indexes and keep only user-selectable profiles.

    `evaluator` resolves `compatible_printers_condition` expressions through
    the engine. Without one, a conditional profile is offered for every
    printer, so a catalog can still be read where no worker is built.
    """
    if not vendors:
        raise ProfileCatalogError("invalid_profile_vendor", "At least one bundled vendor must be configured.")
    profiles_root = Path(repo_root) / "resources" / "profiles"
    entries: List[ProfileEntry] = []
    lookups: Dict[Tuple[str, str], _InheritanceIndex] = {}
    # One cache per load keeps a shared base such as `fdm_process_common` from
    # being re-read once per profile that inherits it.
    cache: Dict[Path, Tuple[Dict[str, Any], List[Path]]] = {}
    for vendor in vendors:
        index = _vendor_index(profiles_root, vendor)
        vendor_root = profiles_root / vendor
        for kind in KINDS:
            listed = index.get(_LIST_KEY[kind])
            if not isinstance(listed, list):
                continue
            lookup = lookups.setdefault((vendor, kind), _InheritanceIndex(vendor_root / kind))
            for item in listed:
                if not isinstance(item, dict):
                    continue
                source = _entry_source(vendor_root, item.get("sub_path"))
                if source is None:
                    continue
                profile, sources = _resolve(source, lookup, cache)
                # Non-instantiable profiles are inheritance bases, not choices.
                if profile.get("instantiation") != "true" or profile.get("type") != kind:
                    continue
                name = profile.get("name")
                if not isinstance(name, str) or not name:
                    continue
                entries.append(
                    ProfileEntry(
                        profile_id=f"{vendor}/{kind}/{name}",
                        kind=kind,
                        name=name,
                        vendor=vendor,
                        source=source,
                        printer_model=str(profile.get("printer_model", "")),
                        nozzle_diameter=_first_value(profile.get("nozzle_diameter")),
                        default_process=str(profile.get("default_print_profile", "")),
                        compatible_printers=_compatible_printers(profile),
                        compatible_condition=str(profile.get("compatible_printers_condition") or ""),
                        # A bundled profile's file is named after the profile,
                        # so the chain is read from the resolved sources.
                        inherits_chain=tuple(path.stem for path in sources),
                    )
                )
    catalog = ProfileCatalog(entries, lookups)
    if evaluator is None:
        return catalog
    return ProfileCatalog(entries, lookups, _evaluate_conditions(catalog, entries, evaluator))


def _evaluate_conditions(
    catalog: ProfileCatalog, entries: Sequence[ProfileEntry], evaluator: CompatibilityEvaluator
) -> Dict[str, FrozenSet[str]]:
    """Ask the engine which printers each conditional profile accepts.

    Every printer is flattened into a private directory beside the request, so
    the evaluator resolves nothing outside it, exactly like a slice job.
    """
    candidates = [
        {"id": entry.profile_id, "condition": entry.compatible_condition}
        for entry in entries
        if entry.kind != "machine" and entry.compatible_condition and not entry.compatible_printers
    ]
    if not candidates:
        return {}
    with tempfile.TemporaryDirectory(prefix="orca-compatibility-") as staging:
        root = Path(staging)
        (root / "printers").mkdir()
        printers = []
        for index, printer in enumerate(catalog.list("machine")):
            relative = f"printers/{index}.json"
            _write_profile(catalog.resolve(printer), root / relative)
            printers.append({"id": printer.profile_id, "name": printer.name, "profile": relative})
        request = root / "request.json"
        request.write_text(
            json.dumps({"catalog_version": 1, "printers": printers, "candidates": candidates}),
            encoding="utf-8",
        )
        answered = evaluator(request).get("compatibility", {})
    return {
        candidate["id"]: frozenset(str(item) for item in answered.get(candidate["id"], []))
        for candidate in candidates
    }


def vendors_from_environment() -> Tuple[str, ...]:
    configured = os.environ.get("ORCA_WEB_PROFILE_VENDORS", "")
    vendors = tuple(item.strip() for item in configured.split(",") if item.strip())
    return vendors or DEFAULT_VENDORS
