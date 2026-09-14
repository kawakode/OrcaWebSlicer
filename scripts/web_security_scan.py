#!/usr/bin/env python3
"""Generate web SBOMs and scan them with the pinned security toolchain."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Callable, Sequence


POLICY_FAILURE_EXIT = 2


class ScanError(RuntimeError):
    """The scanner failed or produced an invalid artifact."""


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), check=False)


def _run(command: Sequence[str], runner: Runner, *, allowed: set[int] | None = None) -> int:
    rendered = " ".join(str(part) for part in command)
    print(f"+ {rendered}", flush=True)
    completed = runner(command)
    if completed.returncode not in (allowed or {0}):
        raise ScanError(f"command exited {completed.returncode}: {rendered}")
    return completed.returncode


def _load_json(path: pathlib.Path, label: str) -> dict:
    if not path.is_file():
        raise ScanError(f"{label} was not created: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScanError(f"{label} is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScanError(f"{label} must contain a JSON object: {path}")
    return value


def validate_cyclonedx(path: pathlib.Path) -> None:
    document = _load_json(path, "CycloneDX SBOM")
    if document.get("bomFormat") != "CycloneDX":
        raise ScanError(f"SBOM has an unexpected bomFormat: {path}")
    if not isinstance(document.get("specVersion"), str):
        raise ScanError(f"SBOM has no specVersion: {path}")
    if not isinstance(document.get("components"), list):
        raise ScanError(f"SBOM has no component list: {path}")


def validate_grype_report(path: pathlib.Path) -> None:
    document = _load_json(path, "Grype report")
    for field, expected_type in (("matches", list), ("source", dict), ("descriptor", dict)):
        if not isinstance(document.get(field), expected_type):
            raise ScanError(f"Grype report has no valid {field!r} field: {path}")


def syft_command(
    source: str,
    output: pathlib.Path,
    syft: str = "syft",
    *,
    source_name: str | None = None,
    excludes: Sequence[str] = (),
) -> list[str]:
    command = [syft, source, "--output", f"cyclonedx-json={output}"]
    if source_name:
        command.extend(("--source-name", source_name))
    for pattern in excludes:
        command.extend(("--exclude", pattern))
    return command


def grype_report_command(sbom: pathlib.Path, output: pathlib.Path, grype: str = "grype") -> list[str]:
    return [grype, f"sbom:{sbom}", "--output", "json", "--file", str(output)]


def grype_policy_command(sbom: pathlib.Path, grype: str = "grype") -> list[str]:
    return [grype, f"sbom:{sbom}", "--only-fixed", "--fail-on", "high", "--output", "table"]


def run_scan(
    *,
    image: str,
    frontend: pathlib.Path,
    output_dir: pathlib.Path,
    syft: str = "syft",
    grype: str = "grype",
    runner: Runner = _default_runner,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = (
        (
            "image",
            "dir:/",
            output_dir / "image.sbom.cdx.json",
            output_dir / "image.grype.json",
            image,
            (
                "./dev/**",
                "./output/**",
                "./proc/**",
                "./source/**",
                "./sys/**",
                "./tmp/**",
                "./usr/local/bin/grype",
                "./usr/local/bin/syft",
                "./usr/local/bin/web-security-scan",
                "./var/cache/**",
            ),
        ),
        (
            "frontend",
            f"dir:{frontend}",
            output_dir / "frontend.sbom.cdx.json",
            output_dir / "frontend.grype.json",
            "orca-web-frontend",
            (),
        ),
    )

    for _, source, sbom_path, _, source_name, excludes in targets:
        _run(syft_command(source, sbom_path, syft, source_name=source_name, excludes=excludes), runner)
        validate_cyclonedx(sbom_path)

    for _, _, sbom_path, report_path, _, _ in targets:
        _run(grype_report_command(sbom_path, report_path, grype), runner)
        validate_grype_report(report_path)

    policy_failures: list[str] = []
    for name, _, sbom_path, _, _, _ in targets:
        status = _run(grype_policy_command(sbom_path, grype), runner, allowed={0, POLICY_FAILURE_EXIT})
        if status == POLICY_FAILURE_EXIT:
            policy_failures.append(name)
    return policy_failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="orca-web-build:latest", help="Local Docker image to inventory.")
    parser.add_argument(
        "--frontend",
        type=pathlib.Path,
        default=pathlib.Path("/source/web/frontend"),
        help="Mounted frontend directory containing package-lock.json.",
    )
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("/output"),
        help="Directory for generated SBOMs and vulnerability reports.",
    )
    parser.add_argument("--syft", default="syft", help="Syft executable.")
    parser.add_argument("--grype", default="grype", help="Grype executable.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        failures = run_scan(
            image=args.image,
            frontend=args.frontend,
            output_dir=args.output_dir,
            syft=args.syft,
            grype=args.grype,
        )
    except ScanError as exc:
        print(f"security scan failed: {exc}", file=sys.stderr)
        return 1
    if failures:
        print(
            "security policy failed for: " + ", ".join(failures)
            + "; see the complete JSON reports in " + str(args.output_dir),
            file=sys.stderr,
        )
        return POLICY_FAILURE_EXIT
    print(f"Security scan passed; reports are in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
