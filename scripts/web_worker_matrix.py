#!/usr/bin/env python3
"""Run the worker-only behaviour matrix: each case twice, outcome and determinism checked.

The native baseline (web_baseline.py) compares the worker against the desktop CLI
for cases both can run. This matrix covers worker behaviours the desktop CLI has
no equivalent for, such as setting overrides, stable error codes, and output
limits, and proves that consecutive runs produce the same semantic G-code.
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

from web_baseline import expand, summarize_gcode
from web_worker_baseline import materialize_profile_set, profile_sources

REPO = pathlib.Path(__file__).resolve().parent.parent


def run_once(worker, case, sources, job, timeout):
    job.mkdir(parents=True)
    model = pathlib.Path(expand(case["model"], {"repo": str(REPO)}))
    input_model = "model{}".format(model.suffix.lower())
    shutil.copy2(model, job / input_model)
    payload = {"input_model": input_model, "output_gcode": "result.gcode"}
    if case.get("profiles"):
        materialize_profile_set(sources, job / "profiles")
        payload["profiles"] = {label: "profiles/{}.json".format(label) for label in sources}
    for key in ("plate_index", "settings", "limits"):
        if key in case:
            payload[key] = case[key]
    request = {
        "protocol_version": 1,
        "job_id": "matrix-{}".format(case["name"]),
        "operation": {"name": "slice", "version": 1, "payload": payload},
    }
    (job / "request.json").write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    completed = subprocess.run(
        [str(worker), "--slice-manifest", str(job / "request.json")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )
    result_path = job / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    gcode = job / "result.gcode"
    return {
        "exit_code": completed.returncode,
        "result": result,
        "summary": summarize_gcode(gcode) if gcode.is_file() else None,
        "partials": sorted(str(path.relative_to(job)) for path in job.rglob("*.partial")),
        "stderr": completed.stderr[-2000:],
    }


def check(case, runs):
    expect = case["expect"]
    problems = []
    for number, run in enumerate(runs, start=1):
        result = run["result"]
        prefix = "run {}: ".format(number)
        if result.get("outcome") != expect["outcome"]:
            problems.append(prefix + "outcome {!r}, expected {!r}; stderr: {}".format(
                result.get("outcome"), expect["outcome"], run["stderr"]))
            continue
        if run["partials"]:
            problems.append(prefix + "partial artifacts remain: {}".format(run["partials"]))
        error = result.get("error", {})
        for expected_key, key in (("error_code", "code"), ("setting", "setting")):
            if expected_key in expect and error.get(key) != expect[expected_key]:
                problems.append(prefix + "{} {!r}, expected {!r}".format(key, error.get(key), expect[expected_key]))
        if expect["outcome"] != "succeeded":
            if run["summary"] is not None or result.get("artifacts"):
                problems.append(prefix + "a failed job published artifacts")
            continue
        summary = run["summary"] or {}
        roles = set(summary.get("role_counts", {}))
        missing = set(expect.get("roles_include", [])) - roles
        unexpected = set(expect.get("roles_exclude", [])) & roles
        if missing:
            problems.append(prefix + "missing roles {}".format(sorted(missing)))
        if unexpected:
            problems.append(prefix + "unexpected roles {}".format(sorted(unexpected)))
        # A single-tool job emits no tool change, so an empty sequence means tool 0.
        tools = sorted(set(summary.get("tool_sequence") or [0]))
        if "tools" in expect and tools != expect["tools"]:
            problems.append(prefix + "tools {}, expected {}".format(tools, expect["tools"]))
    summaries = [run["summary"] for run in runs]
    if any(summary != summaries[0] for summary in summaries[1:]):
        problems.append("consecutive runs produced different semantic G-code")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, default=REPO / "docs/web/worker-matrix-cases.json")
    parser.add_argument("--profiles", type=pathlib.Path, default=REPO / "docs/web/baseline-cases.json",
                        help="Manifest whose machine, process, and filament profiles the cases use.")
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    parser.add_argument("--case", action="append", dest="cases", help="Run only the named case.")
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sources = profile_sources(json.loads(args.profiles.read_text(encoding="utf-8")), REPO)
    cases = [case for case in manifest["cases"] if not args.cases or case["name"] in args.cases]
    report = {}
    with tempfile.TemporaryDirectory(prefix="orca-worker-matrix-") as temporary:
        root = pathlib.Path(temporary)
        for case in cases:
            runs = [
                run_once(args.worker, case, sources, root / case["name"] / "run-{}".format(number),
                         manifest.get("timeout_seconds", 300))
                for number in range(1, manifest.get("repetitions", 2) + 1)
            ]
            problems = check(case, runs)
            report[case["name"]] = {"passed": not problems, "problems": problems}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(entry["passed"] for entry in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
