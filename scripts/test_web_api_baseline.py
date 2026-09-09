#!/usr/bin/env python3
"""Slice a baseline case through the HTTP API and compare it to the native run.

This is the G4 exit check: the same fixture that produced the recorded native
semantic baseline is uploaded, sliced, and downloaded through the API, and the
G-code the browser would receive must be semantically identical.
"""

import argparse
import json
import pathlib
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from web.api.config import ApiConfig  # noqa: E402
from web.api.app import create_app  # noqa: E402
from web_baseline import case_lanes, expand, summarize_gcode, validate_manifest  # noqa: E402
from web_job_directory import JobDirectoryLimits  # noqa: E402
from web_worker_executor import ExecutorLimits  # noqa: E402

TERMINAL_STATES = {"succeeded", "failed", "canceled"}


def latest_native_run(root):
    runs = sorted(path.parent for path in root.glob("*/report.json"))
    if not runs:
        raise ValueError("no native baseline report found under {}".format(root))
    return runs[-1]


def catalog_identity(source):
    """Map a bundled profile path onto the catalog id the API serves it under."""
    profile = json.loads(source.read_text(encoding="utf-8"))
    vendor, kind = source.parts[-3], source.parts[-2]
    return vendor, "{}/{}/{}".format(vendor, kind, profile["name"])


def selected_profiles(manifest):
    declared = manifest.get("profiles", {})
    if set(declared) != {"machine", "process", "filament"}:
        raise ValueError("baseline manifest must define machine, process, and filament profiles")
    vendors = set()
    identifiers = {}
    for kind, template in declared.items():
        source = pathlib.Path(expand(template, {"repo": str(REPO_ROOT)}))
        vendor, identifier = catalog_identity(source)
        vendors.add(vendor)
        identifiers[kind] = identifier
    if len(vendors) != 1:
        raise ValueError("baseline profiles must come from one bundled vendor")
    return vendors.pop(), identifiers


def model_source(case):
    path = pathlib.Path(expand(case["model"], {"repo": str(REPO_ROOT)}))
    if not path.is_file():
        raise ValueError("case {} model does not exist: {}".format(case["name"], path))
    return path


def run_case(client, case, native_run, output_root, timeout_seconds):
    source = model_source(case)
    with source.open("rb") as stream:
        upload = client.post("/api/v1/uploads", files={"file": (source.name, stream)})
    if upload.status_code != 201:
        return {"matches": False, "stage": "upload", "response": upload.json()}

    submission = {"upload_id": upload.json()["upload_id"], **case["profiles"]}
    if case.get("settings"):
        submission["settings"] = dict(case["settings"])
    accepted = client.post(
        "/api/v1/jobs",
        json=submission,
        headers={"X-Correlation-Id": "api-baseline-{}".format(case["name"])},
    )
    if accepted.status_code != 202:
        return {"matches": False, "stage": "submit", "response": accepted.json()}

    job_id = accepted.json()["job_id"]
    deadline = time.monotonic() + timeout_seconds
    job = accepted.json()
    while time.monotonic() < deadline:
        job = client.get("/api/v1/jobs/{}".format(job_id)).json()
        if job["state"] in TERMINAL_STATES:
            break
        time.sleep(0.05)

    expect = case.get("expect")
    if expect is not None:
        # An error-fixture case asserts the API's stable error verbatim
        # instead of comparing G-code; it is expected never to succeed.
        error = job.get("error") or {}
        matches = (
            job["state"] == "failed"
            and error.get("code") == expect["code"]
            and error.get("category") == expect["category"]
        )
        return {
            "matches": matches,
            "stage": "compared",
            "state": job["state"],
            "expected": expect,
            "error": job["error"],
        }

    if job["state"] != "succeeded":
        return {"matches": False, "stage": "slice", "state": job["state"], "error": job["error"]}

    downloaded = client.get("/api/v1/jobs/{}/artifacts/gcode".format(job_id))
    if downloaded.status_code != 200:
        return {"matches": False, "stage": "download", "status": downloaded.status_code}
    actual_path = output_root / "{}.gcode".format(case["name"])
    actual_path.write_bytes(downloaded.content)

    native_files = sorted((native_run / case["name"] / "run-1").glob("*.gcode"))
    if len(native_files) != 1:
        raise ValueError("native case {} must contain exactly one G-code file".format(case["name"]))
    expected = summarize_gcode(native_files[0])
    actual = summarize_gcode(actual_path)
    differences = {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    }
    return {
        "matches": not differences,
        "stage": "compared",
        "correlation_id": job["correlation_id"],
        "differences": differences,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    parser.add_argument("--native-baselines", required=True, type=pathlib.Path)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    native_run = latest_native_run(args.native_baselines)
    vendor, profiles = selected_profiles(manifest)
    cases = [
        case
        for case in manifest["cases"]
        if (args.case is None or case["name"] in args.case) and "api" in case_lanes(case)
    ]
    if not cases:
        raise ValueError("no baseline case matched {}".format(args.case))
    for case in cases:
        case["profiles"] = {"{}_profile".format(kind): value for kind, value in profiles.items()}

    with tempfile.TemporaryDirectory(prefix="orca-api-baseline-") as workspace:
        root = pathlib.Path(workspace)
        config = ApiConfig(
            repo_root=REPO_ROOT,
            state_root=root / "state",
            worker_command=(str(args.worker.resolve()),),
            profile_vendors=(vendor,),
            max_concurrent_jobs=1,
            executor_limits=ExecutorLimits(
                wall_time_ms=int(args.timeout_seconds * 1000),
                cpu_time_ms=int(args.timeout_seconds * 1000),
            ),
            job_limits=JobDirectoryLimits(),
        )
        with TestClient(create_app(config)) as client:
            results = {
                case["name"]: run_case(client, case, native_run, root, args.timeout_seconds)
                for case in cases
            }

    report = {
        "native_run": native_run.name,
        "profiles": profiles,
        "matches": all(result["matches"] for result in results.values()),
        "cases": results,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
