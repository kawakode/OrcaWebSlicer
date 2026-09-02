#!/usr/bin/env python3
"""Run repeatable OrcaSlicer CLI cases and record semantic G-code summaries."""

import argparse
import collections
import ctypes
import datetime
import glob
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import subprocess
import sys
import time


SCHEMA_VERSION = 1
MOVE_RE = re.compile(r"^\s*(G[0123])(?:\s|$)", re.IGNORECASE)
WORD_RE = re.compile(r"([A-Za-z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
TOOL_RE = re.compile(r"^\s*T(\d+)\s*(?:;.*)?$", re.IGNORECASE)

if sys.platform == "win32":
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]


    OPEN_PROCESS = ctypes.windll.kernel32.OpenProcess
    OPEN_PROCESS.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    OPEN_PROCESS.restype = ctypes.c_void_p
    CLOSE_HANDLE = ctypes.windll.kernel32.CloseHandle
    CLOSE_HANDLE.argtypes = [ctypes.c_void_p]
    CLOSE_HANDLE.restype = ctypes.c_int
    GET_PROCESS_MEMORY_INFO = ctypes.windll.psapi.GetProcessMemoryInfo
    GET_PROCESS_MEMORY_INFO.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    GET_PROCESS_MEMORY_INFO.restype = ctypes.c_int


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def process_rss_bytes(pid):
    """Return resident bytes for one process, or None on unsupported platforms."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/{}/status".format(pid), "r", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        process_vm_read = 0x0010
        handle = OPEN_PROCESS(
            process_query_limited_information | process_vm_read, False, pid
        )
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not GET_PROCESS_MEMORY_INFO(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return int(counters.WorkingSetSize)
        finally:
            CLOSE_HANDLE(handle)

    return None


def git_metadata(repo):
    injected_commit = os.environ.get("ORCA_WEB_GIT_COMMIT", "").strip()
    injected_dirty = os.environ.get("ORCA_WEB_GIT_DIRTY", "").strip().lower()
    if injected_commit and injected_dirty in ("true", "false"):
        return {
            "commit": injected_commit,
            "dirty": injected_dirty == "true",
            "source": "environment",
        }

    def run_git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", str(repo)] + list(args),
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    commit = run_git("rev-parse", "HEAD")
    status = run_git("status", "--porcelain")
    return {
        "commit": commit,
        "dirty": None if status is None else bool(status),
        "source": "git",
    }


def parse_words(code):
    return {name.upper(): float(value) for name, value in WORD_RE.findall(code)}


def summarize_gcode(path):
    command_counts = collections.Counter()
    role_counts = collections.Counter()
    temperature_commands = []
    tool_sequence = []
    positive_extrusion = collections.defaultdict(float)
    xy_bounds = [math.inf, math.inf, -math.inf, -math.inf]
    linear_xy_distance = 0.0
    layer_z = []

    x = y = z = e = 0.0
    active_tool = 0
    xyz_absolute = True
    e_absolute = True
    pending_layer = False

    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith(";LAYER_CHANGE"):
                pending_layer = True
                continue
            if line.startswith("; FEATURE:"):
                role_counts[line.partition(":")[2].strip()] += 1
                continue
            if line.startswith(";TYPE:"):
                role_counts[line.partition(":")[2].strip()] += 1
                continue

            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            command = code.split(None, 1)[0].upper()
            command_counts[command] += 1

            if command == "G90":
                xyz_absolute = True
                continue
            if command == "G91":
                xyz_absolute = False
                continue
            if command == "M82":
                e_absolute = True
                continue
            if command == "M83":
                e_absolute = False
                continue
            if command == "G92":
                words = parse_words(code)
                x = words.get("X", x)
                y = words.get("Y", y)
                z = words.get("Z", z)
                e = words.get("E", e)
                continue

            tool_match = TOOL_RE.match(code)
            if tool_match:
                active_tool = int(tool_match.group(1))
                tool_sequence.append(active_tool)
                continue

            if command in ("M104", "M109", "M140", "M190"):
                words = parse_words(code)
                if "S" in words:
                    temperature_commands.append(
                        {"command": command, "temperature": words["S"]}
                    )

            move_match = MOVE_RE.match(code)
            if not move_match:
                continue

            words = parse_words(code)
            new_x = words.get("X", x if xyz_absolute else 0.0)
            new_y = words.get("Y", y if xyz_absolute else 0.0)
            new_z = words.get("Z", z if xyz_absolute else 0.0)
            if not xyz_absolute:
                new_x += x
                new_y += y
                new_z += z

            if "X" in words or "Y" in words:
                xy_bounds[0] = min(xy_bounds[0], new_x)
                xy_bounds[1] = min(xy_bounds[1], new_y)
                xy_bounds[2] = max(xy_bounds[2], new_x)
                xy_bounds[3] = max(xy_bounds[3], new_y)
                if command in ("G0", "G1"):
                    linear_xy_distance += math.hypot(new_x - x, new_y - y)

            if "E" in words:
                new_e = words["E"] if e_absolute else e + words["E"]
                delta_e = new_e - e
                if delta_e > 0:
                    positive_extrusion[active_tool] += delta_e
                e = new_e

            if pending_layer and "Z" in words:
                layer_z.append(new_z)
                pending_layer = False
            x, y, z = new_x, new_y, new_z

    finite_bounds = None
    if all(math.isfinite(value) for value in xy_bounds):
        finite_bounds = [round(value, 5) for value in xy_bounds]

    return {
        "layer_count": len(layer_z),
        "z_range": None
        if not layer_z
        else [round(min(layer_z), 5), round(max(layer_z), 5)],
        "tool_sequence": tool_sequence,
        "role_counts": dict(sorted(role_counts.items())),
        "xy_bounds": finite_bounds,
        "linear_xy_distance": round(linear_xy_distance, 5),
        "positive_extrusion_by_tool": {
            str(tool): round(value, 5)
            for tool, value in sorted(positive_extrusion.items())
        },
        "temperature_commands": temperature_commands,
        "command_counts": dict(sorted(command_counts.items())),
    }


def expand(value, variables):
    if isinstance(value, str):
        return value.format_map(variables)
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, variables) for key, item in value.items()}
    return value


def resolve_profile(path, stack=None):
    path = path.resolve()
    stack = [] if stack is None else stack
    if path in stack:
        chain = " -> ".join(str(item) for item in stack + [path])
        raise ValueError("profile inheritance cycle: {}".format(chain))
    if not path.is_file():
        raise ValueError("profile file does not exist: {}".format(path))

    with open(path, "r", encoding="utf-8") as stream:
        profile = json.load(stream)
    if not isinstance(profile, dict):
        raise ValueError("profile must contain a JSON object: {}".format(path))

    sources = []
    parent_name = profile.get("inherits")
    if parent_name:
        parent_path = path.parent / "{}.json".format(parent_name)
        parent, sources = resolve_profile(parent_path, stack + [path])
        merged = dict(parent)
        merged.update(profile)
        profile = merged

    profile.pop("inherits", None)
    return profile, sources + [path]


def materialize_profiles(manifest, repo, output_root):
    specs = manifest.get("profiles", {})
    if not specs:
        return {}, {}
    if not isinstance(specs, dict):
        raise ValueError("manifest profiles must be an object")

    profile_dir = output_root / "profiles"
    profile_dir.mkdir()
    variables = {"repo": str(repo), "output": str(output_root)}
    paths = {}
    records = {}
    for label, source_template in sorted(specs.items()):
        if not isinstance(source_template, str) or not source_template:
            raise ValueError("profile {} must be a non-empty path".format(label))
        source = pathlib.Path(expand(source_template, variables))
        resolved, sources = resolve_profile(source)
        target = profile_dir / "{}.json".format(label)
        with open(target, "w", encoding="utf-8") as stream:
            json.dump(resolved, stream, indent=2, sort_keys=True)
            stream.write("\n")
        paths["{}_profile".format(label)] = str(target)
        records[label] = {
            "path": str(target),
            "sha256": sha256_file(target),
            "sources": [str(item) for item in sources],
        }
    return paths, records


def validate_manifest(manifest):
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "manifest schema_version must be {}".format(SCHEMA_VERSION)
        )
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest cases must be a non-empty array")
    names = set()
    for case in cases:
        name = case.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("every case needs a non-empty name")
        if name in names:
            raise ValueError("duplicate case name: {}".format(name))
        names.add(name)
        if not isinstance(case.get("arguments"), list):
            raise ValueError("case {} arguments must be an array".format(name))


def output_matches(case_output, patterns):
    matches = {}
    for pattern in patterns:
        resolved = glob.glob(str(case_output / pattern), recursive=True)
        matches[pattern] = sorted(
            str(pathlib.Path(item).relative_to(case_output)) for item in resolved
        )
    return matches


def read_result_json(case_output):
    path = case_output / "result.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {"parse_error": True}


def run_repetition(
    slicer, repo, output_root, case, repetition, defaults, profile_variables
):
    case_output = output_root / case["name"] / "run-{}".format(repetition)
    case_output.mkdir(parents=True, exist_ok=False)
    variables = dict(profile_variables)
    variables.update({
        "repo": str(repo),
        "output": str(output_root),
        "case_output": str(case_output),
    })
    resolved = expand(case, variables)
    arguments = [str(slicer)] + [str(item) for item in resolved["arguments"]]
    environment = os.environ.copy()
    environment.update(
        {str(key): str(value) for key, value in resolved.get("environment", {}).items()}
    )
    timeout_seconds = resolved.get(
        "timeout_seconds", defaults.get("timeout_seconds", 300)
    )

    stdout_path = case_output / "stdout.log"
    stderr_path = case_output / "stderr.log"
    started = time.perf_counter()
    timed_out = False
    launch_error = None
    exit_code = None
    peak_rss_bytes = None
    with open(stdout_path, "wb") as stdout_stream, open(
        stderr_path, "wb"
    ) as stderr_stream:
        try:
            process = subprocess.Popen(
                arguments,
                cwd=str(repo),
                env=environment,
                stdout=stdout_stream,
                stderr=stderr_stream,
            )
        except OSError as error:
            launch_error = str(error)
            stderr_stream.write((launch_error + "\n").encode("utf-8"))
        else:
            deadline = started + timeout_seconds
            while True:
                rss_bytes = process_rss_bytes(process.pid)
                if rss_bytes is not None:
                    peak_rss_bytes = max(peak_rss_bytes or 0, rss_bytes)
                exit_code = process.poll()
                if exit_code is not None:
                    break
                if time.perf_counter() >= deadline:
                    timed_out = True
                    process.kill()
                    process.wait()
                    exit_code = None
                    break
                time.sleep(0.05)
    wall_seconds = time.perf_counter() - started

    patterns = resolved.get("expected_outputs", [])
    matches = output_matches(case_output, patterns)
    missing_outputs = [pattern for pattern, items in matches.items() if not items]
    gcode_files = sorted(case_output.rglob("*.gcode"))
    artifacts = []
    for artifact in sorted(path for path in case_output.rglob("*") if path.is_file()):
        artifacts.append(
            {
                "path": str(artifact.relative_to(case_output)),
                "size": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            }
        )

    expected_exit_codes = resolved.get(
        "expected_exit_codes", defaults.get("expected_exit_codes", [0])
    )
    record = {
        "repetition": repetition,
        "arguments": arguments,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "exit_code": exit_code,
        "wall_seconds": round(wall_seconds, 5),
        "peak_rss_bytes": peak_rss_bytes,
        "expected_output_matches": matches,
        "missing_outputs": missing_outputs,
        "result": read_result_json(case_output),
        "artifacts": artifacts,
        "gcode": [
            {
                "path": str(path.relative_to(case_output)),
                "semantic": summarize_gcode(path),
            }
            for path in gcode_files
        ],
    }
    record["passed"] = (
        launch_error is None
        and not timed_out
        and exit_code in expected_exit_codes
        and not missing_outputs
        and bool(record["gcode"] or resolved.get("allow_no_gcode", False))
    )
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--slicer", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--list", action="store_true", dest="list_cases")
    args = parser.parse_args(argv)

    manifest_path = args.manifest.resolve()
    with open(manifest_path, "r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    validate_manifest(manifest)

    if args.list_cases:
        for case in manifest["cases"]:
            if case.get("enabled", True):
                print(case["name"])
        return 0
    if args.validate_only:
        print("Manifest is valid: {}".format(manifest_path))
        return 0
    if args.slicer is None:
        parser.error("--slicer is required unless --validate-only or --list is used")

    slicer = args.slicer.resolve()
    if not slicer.is_file():
        parser.error("slicer executable does not exist: {}".format(slicer))

    repo = pathlib.Path(__file__).resolve().parent.parent
    base_output = (
        args.output.resolve() if args.output else repo / "build" / "web-baseline"
    )
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    output_root = base_output / "{}-{}".format(timestamp, os.getpid())
    output_root.mkdir(parents=True, exist_ok=False)
    profile_variables, profile_records = materialize_profiles(
        manifest, repo, output_root
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repository": git_metadata(repo),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "slicer": {
            "path": str(slicer),
            "sha256": sha256_file(slicer),
        },
        "manifest": str(manifest_path),
        "profiles": profile_records,
        "cases": [],
    }

    defaults = manifest.get("defaults", {})
    repetitions = int(defaults.get("repetitions", 2))
    all_passed = True
    for case in manifest["cases"]:
        if not case.get("enabled", True):
            continue
        print("Running {}".format(case["name"]), flush=True)
        case_record = {"name": case["name"], "runs": []}
        for repetition in range(1, repetitions + 1):
            run = run_repetition(
                slicer,
                repo,
                output_root,
                case,
                repetition,
                defaults,
                profile_variables,
            )
            case_record["runs"].append(run)

        semantic_runs = [run["gcode"] for run in case_record["runs"]]
        deterministic = all(item == semantic_runs[0] for item in semantic_runs[1:])
        case_record["deterministic"] = deterministic
        case_record["passed"] = deterministic and all(
            run["passed"] for run in case_record["runs"]
        )
        all_passed = all_passed and case_record["passed"]
        report["cases"].append(case_record)

    report["passed"] = all_passed
    report_path = output_root / "report.json"
    with open(report_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("Baseline report: {}".format(report_path))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
