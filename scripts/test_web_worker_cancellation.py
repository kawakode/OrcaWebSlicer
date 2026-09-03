#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cancel a running web worker slice and validate its terminal output.")
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--timeout", default=30.0, type=float)
    return parser.parse_args()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_lines(stream, output, messages) -> None:
    for line in stream:
        output.append(line)
        messages.put(line)
    messages.put(None)


def main() -> int:
    args = parse_args()
    worker = args.worker.resolve()
    model = args.model.resolve()
    require(worker.is_file(), f"Worker does not exist: {worker}")
    require(model.is_file(), f"Model does not exist: {model}")

    with tempfile.TemporaryDirectory(prefix="orca-worker-cancel-") as temporary:
        job_dir = Path(temporary)
        shutil.copyfile(model, job_dir / "model.obj")
        manifest = {
            "protocol_version": 1,
            "job_id": "cancellation-smoke",
            "operation": {
                "name": "slice",
                "version": 1,
                "payload": {
                    "input_model": "model.obj",
                    "output_gcode": "result.gcode",
                    "settings": {
                        "layer_height": "0.1",
                        "initial_layer_print_height": "0.1",
                        "layer_change_gcode": "G92 E0\n",
                    },
                },
            },
        }
        manifest_path = job_dir / "request.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(worker), "--slice-manifest", str(manifest_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=creation_flags,
        )
        require(process.stdout is not None, "Worker stdout pipe was not created.")
        require(process.stderr is not None, "Worker stderr pipe was not created.")

        stdout_lines = []
        stderr_lines = []
        messages = queue.Queue()
        stdout_thread = threading.Thread(target=read_lines, args=(process.stdout, stdout_lines, messages), daemon=True)
        stderr_thread = threading.Thread(
            target=lambda: stderr_lines.extend(process.stderr.readlines()), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        events = []
        cancellation_sent = False
        deadline = time.monotonic() + args.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "Timed out waiting for the worker cancellation result.")
                try:
                    line = messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise RuntimeError("Timed out waiting for worker output.") from error
                if line is None:
                    break
                event = json.loads(line)
                events.append(event)
                progress = event.get("progress", {})
                if not cancellation_sent and event.get("type") == "progress" and progress.get("stage") == "slicing":
                    cancellation_sent = True
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)

            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

        diagnostic = ("stdout:\n" + "".join(stdout_lines) + "stderr:\n" + "".join(stderr_lines)).strip()
        require(cancellation_sent, f"Worker exited before reaching a real slice. {diagnostic}")
        require(process.returncode == 6, f"Expected cancellation exit 6, got {process.returncode}. {diagnostic}")
        require(events, "Worker emitted no protocol events.")
        require([event["sequence"] for event in events] == list(range(len(events))), "Event sequence is not contiguous.")
        require(events[-1].get("state") == "canceled", "The final worker state is not canceled.")
        errors = [event["error"] for event in events if event.get("type") == "error"]
        require(errors and errors[-1].get("category") == "cancellation", "No cancellation error event was emitted.")
        require(errors[-1].get("code") == "job_canceled", "The cancellation error code is not stable.")

        result_path = job_dir / "result.json"
        require(result_path.is_file(), "Cancellation did not publish result.json.")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        require(result.get("outcome") == "canceled", "The terminal result is not canceled.")
        require(result.get("artifacts") == [], "A canceled result exposes downloadable artifacts.")
        require(not (job_dir / "result.gcode").exists(), "Canceled G-code was published.")
        partials = [path for path in job_dir.rglob("*") if path.name.endswith(".partial")]
        require(not partials, f"Cancellation left partial artifacts: {partials}")

    print("Worker cancellation smoke passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Worker cancellation smoke failed: {error}") from error
