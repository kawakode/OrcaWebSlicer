#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exercise stable web worker resource-limit failures.")
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_case(worker: Path, model: Path, name: str, limit: str, value: int, expected_code: str,
             environment: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix=f"orca-worker-{name}-") as temporary:
        job_dir = Path(temporary)
        shutil.copyfile(model, job_dir / "model.obj")
        manifest = {
            "protocol_version": 1,
            "job_id": f"{name}-smoke",
            "operation": {
                "name": "slice",
                "version": 1,
                "payload": {
                    "input_model": "model.obj",
                    "output_gcode": "result.gcode",
                    "limits": {limit: value},
                    "settings": {
                        "layer_height": "0.3",
                        "initial_layer_print_height": "0.3",
                        "layer_change_gcode": "G92 E0\n",
                    },
                },
            },
        }
        manifest_path = job_dir / "request.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(environment)
        process = subprocess.run(
            [str(worker), "--slice-manifest", str(manifest_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=process_environment,
            check=False,
            timeout=30,
        )
        diagnostic = f"stdout:\n{process.stdout}stderr:\n{process.stderr}".strip()
        require(process.returncode == 7, f"{name}: expected exit 7, got {process.returncode}. {diagnostic}")
        events = [json.loads(line) for line in process.stdout.splitlines()]
        require(events, f"{name}: worker emitted no events.")
        require(events[-1].get("state") == "failed", f"{name}: final state is not failed.")
        errors = [event["error"] for event in events if event.get("type") == "error"]
        require(errors, f"{name}: no error event was emitted.")
        require(errors[-1].get("category") == "resource_limit", f"{name}: error category is not resource_limit.")
        require(errors[-1].get("code") == expected_code, f"{name}: unexpected error code. {diagnostic}")

        result_path = job_dir / "result.json"
        require(result_path.is_file(), f"{name}: result.json was not published.")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        require(result.get("outcome") == "failed", f"{name}: terminal result is not failed.")
        require(result.get("artifacts") == [], f"{name}: failed result exposes artifacts.")
        require(result.get("error", {}).get("code") == expected_code, f"{name}: result error code changed.")
        require(not (job_dir / "result.gcode").exists(), f"{name}: G-code was published.")
        partials = [path for path in job_dir.rglob("*") if path.name.endswith(".partial")]
        require(not partials, f"{name}: partial artifacts remain: {partials}")


def main() -> int:
    args = parse_args()
    worker = args.worker.resolve()
    model = args.model.resolve()
    require(worker.is_file(), f"Worker does not exist: {worker}")
    require(model.is_file(), f"Model does not exist: {model}")

    run_case(worker, model, "input-limit", "max_input_bytes", 1, "input_size_limit_exceeded")
    run_case(worker, model, "triangle-limit", "max_triangles", 1, "triangle_limit_exceeded")
    run_case(worker, model, "wall-time-limit", "max_wall_time_ms", 1, "wall_time_limit_exceeded")
    run_case(worker, model, "memory-limit", "max_memory_bytes", 1, "memory_limit_exceeded")
    run_case(
        worker,
        model,
        "server-ceiling",
        "max_triangles",
        1_000_000,
        "triangle_limit_exceeded",
        {"ORCA_WEB_MAX_TRIANGLES": "1"},
    )

    print("Worker resource-limit smoke passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"Worker resource-limit smoke failed: {error}") from error
