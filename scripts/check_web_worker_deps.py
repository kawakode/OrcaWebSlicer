#!/usr/bin/env python3
"""Fail when the web worker has a forbidden GUI or media runtime dependency."""

from __future__ import annotations

import argparse
import pathlib
import platform
import re
import shutil
import subprocess
import sys


FORBIDDEN_DEPENDENCIES = {
    "Orca GUI": re.compile(r"libslic3r[_-]gui", re.IGNORECASE),
    "wxWidgets": re.compile(r"(?:libwx|wxbase|wxmsw)", re.IGNORECASE),
    "OpenGL": re.compile(
        r"(?:lib(?:EGL|GL|GLX|GLU)\.so|libOpenGL|OpenGL32|OpenGL\.framework)", re.IGNORECASE
    ),
    "GLFW": re.compile(r"glfw", re.IGNORECASE),
    "ImGui": re.compile(r"imgui", re.IGNORECASE),
    "WebKit": re.compile(r"webkit", re.IGNORECASE),
    "WebView": re.compile(r"webview2", re.IGNORECASE),
    "Python": re.compile(r"python(?:\d|\.)", re.IGNORECASE),
    "device access": re.compile(r"(?:hidapi|libusb|libudev)", re.IGNORECASE),
    "media stack": re.compile(r"(?:avcodec|avformat|gstreamer)", re.IGNORECASE),
}


def dependency_command(binary: pathlib.Path) -> list[str]:
    system = platform.system()
    if system == "Linux":
        return ["ldd", str(binary)]
    if system == "Darwin":
        return ["otool", "-L", str(binary)]
    if system == "Windows":
        dumpbin = shutil.which("dumpbin")
        if dumpbin is None:
            raise RuntimeError("dumpbin is required to inspect dependencies on Windows")
        return [dumpbin, "/DEPENDENTS", str(binary)]
    raise RuntimeError(f"unsupported platform: {system}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    args = parser.parse_args()

    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"worker binary does not exist: {binary}")

    try:
        command = dependency_command(binary)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    dependencies = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()

    # ldd returns 1 for a fully static executable, which is valid for this check.
    is_static_linux_binary = platform.system() == "Linux" and "not a dynamic executable" in dependencies
    if completed.returncode != 0 and not is_static_linux_binary:
        print(dependencies, file=sys.stderr)
        return completed.returncode

    violations = [name for name, pattern in FORBIDDEN_DEPENDENCIES.items() if pattern.search(dependencies)]
    if violations:
        print(dependencies, file=sys.stderr)
        print(f"forbidden worker dependencies: {', '.join(violations)}", file=sys.stderr)
        return 1

    print(dependencies or "No dynamic dependencies.")
    print("Worker dependency check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
