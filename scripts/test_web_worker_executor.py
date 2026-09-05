#!/usr/bin/env python3
"""Exercise the isolated executor against the real web worker."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from web_worker_executor import ExecutorError, execute_worker, limits_from_environment


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument(
        "--manifest-template",
        default=Path("tests/data/web-worker/slice-envelope.json"),
        type=Path,
    )
    args = parser.parse_args()

    worker = args.worker.resolve()
    model = args.model.resolve()
    manifest_template = args.manifest_template.resolve()
    with tempfile.TemporaryDirectory(prefix="orca-web-executor-") as temporary:
        job_root = Path(temporary)
        model_sentinel = "MODEL-CONTENT-SENTINEL-6fbd4868"
        gcode_sentinel = "GCODE-CONTENT-SENTINEL-2f39874e"
        credential_sentinel = "CREDENTIAL-SENTINEL-3e1a275c"
        copied_model = job_root / "model.obj"
        copied_model.write_bytes((f"# {model_sentinel}\n").encode("utf-8") + model.read_bytes())
        manifest = job_root / "request.json"
        request = json.loads(manifest_template.read_text(encoding="utf-8"))
        request["executor_test_credential"] = credential_sentinel
        request["operation"]["payload"].setdefault("settings", {})["machine_start_gcode"] = (
            f"G28 ; {gcode_sentinel}"
        )
        manifest.write_text(json.dumps(request), encoding="utf-8")
        execution = execute_worker([str(worker)], manifest, limits_from_environment())

        require(execution.status == "completed", execution.summary())
        require(execution.succeeded, execution.summary())
        require(execution.worker_exit_code == 0, execution.summary())
        require(not execution.forced_termination, execution.summary())
        require(execution.events[-1].get("state") == "succeeded", execution.summary())
        require((job_root / "result.gcode").is_file(), "Executor run published no G-code.")
        require((job_root / "result.json").is_file(), "Executor run published no result.json.")
        gcode = (job_root / "result.gcode").read_text(encoding="utf-8", errors="replace")
        require(gcode_sentinel in gcode, "Sensitive-output canary was not present in G-code.")
        captured = execution.stderr + json.dumps(execution.events, separators=(",", ":"))
        for sentinel in (model_sentinel, gcode_sentinel, credential_sentinel):
            require(sentinel not in captured, "Worker diagnostics exposed job content.")

        print(json.dumps(execution.summary(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExecutorError, OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"executor integration check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
