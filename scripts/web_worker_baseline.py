#!/usr/bin/env python3
"""Run worker slices against the latest recorded native semantic baseline."""

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile

from web_baseline import expand, resolve_profile, summarize_gcode, validate_manifest


def latest_native_run(root):
    runs = sorted(path.parent for path in root.glob("*/report.json"))
    if not runs:
        raise ValueError("no native baseline report found under {}".format(root))
    return runs[-1]


def profile_sources(manifest, repo):
    variables = {"repo": str(repo)}
    profiles = manifest.get("profiles", {})
    required = {"machine", "process", "filament"}
    if set(profiles) != required:
        raise ValueError("baseline manifest must define machine, process, and filament profiles")
    return {label: pathlib.Path(expand(value, variables)) for label, value in profiles.items()}


def model_source(case, repo):
    candidates = [
        pathlib.Path(value.replace("{repo}", str(repo)))
        for value in case["arguments"]
        if pathlib.Path(value).suffix.lower() in (".stl", ".obj")
    ]
    if len(candidates) != 1 or not candidates[0].is_file():
        raise ValueError("case {} must resolve to one STL or OBJ input".format(case["name"]))
    return candidates[0]


def materialize_profile_set(sources, destination):
    destination.mkdir()
    for label, source in sources.items():
        profile, _ = resolve_profile(source)
        with (destination / "{}.json".format(label)).open("w", encoding="utf-8") as stream:
            json.dump(profile, stream, indent=2, sort_keys=True)
            stream.write("\n")


def run_case(worker, case, repo, sources, native_run, output_root):
    job = output_root / case["name"]
    job.mkdir()
    source_model = model_source(case, repo)
    input_model = "model{}".format(source_model.suffix.lower())
    shutil.copy2(source_model, job / input_model)
    materialize_profile_set(sources, job / "profiles")

    request = {
        "protocol_version": 1,
        "job_id": "baseline-{}".format(case["name"]),
        "operation": {
            "name": "slice",
            "version": 1,
            "payload": {
                "input_model": input_model,
                "output_gcode": "result.gcode",
                "profiles": {
                    "machine": "profiles/machine.json",
                    "process": "profiles/process.json",
                    "filament": "profiles/filament.json",
                },
            },
        },
    }
    request_path = job / "request.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")

    completed = subprocess.run(
        [str(worker), "--slice-manifest", str(request_path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {
            "matches": False,
            "worker_exit_code": completed.returncode,
            "worker_stdout": completed.stdout,
            "worker_stderr": completed.stderr,
        }

    native_files = sorted((native_run / case["name"] / "run-1").glob("*.gcode"))
    if len(native_files) != 1:
        raise ValueError("native case {} must contain exactly one G-code file".format(case["name"]))
    expected = summarize_gcode(native_files[0])
    actual = summarize_gcode(job / "result.gcode")
    differences = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    }
    return {
        "matches": not differences,
        "worker_exit_code": completed.returncode,
        "differences": differences,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    parser.add_argument("--native-baselines", required=True, type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()

    repo = pathlib.Path(__file__).resolve().parent.parent
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    native_run = latest_native_run(args.native_baselines)
    sources = profile_sources(manifest, repo)

    temporary = None
    if args.output is None:
        temporary = tempfile.TemporaryDirectory(prefix="orca-worker-baseline-")
        output_root = pathlib.Path(temporary.name)
    else:
        output_root = args.output
        output_root.mkdir(parents=True, exist_ok=False)

    results = {
        case["name"]: run_case(args.worker, case, repo, sources, native_run, output_root)
        for case in manifest["cases"]
    }
    report = {
        "native_run": native_run.name,
        "matches": all(result["matches"] for result in results.values()),
        "cases": results,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if temporary is not None:
        temporary.cleanup()
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
