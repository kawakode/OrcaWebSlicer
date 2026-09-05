#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from web_worker_executor import ExecutorError, ExecutorLimits, execute_worker  # noqa: E402


FAKE_WORKER = r'''#!/usr/bin/env python3
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

manifest = Path(sys.argv[2])
request = json.loads(manifest.read_text(encoding="utf-8"))
job_id = request["job_id"]
mode = request["mode"]

def event(sequence, kind, **fields):
    value = {"protocol_version": 1, "job_id": job_id, "sequence": sequence, "type": kind}
    value.update(fields)
    print(json.dumps(value, separators=(",", ":")), flush=True)

def result(outcome, artifacts=None):
    value = {
        "protocol_version": 1,
        "job_id": job_id,
        "outcome": outcome,
        "warnings": [],
        "timing": {"duration_ms": 1, "cpu_time_ms": 1},
        "resource_usage": {"peak_memory_bytes": 1},
        "artifacts": artifacts or [],
        "error": None,
    }
    (manifest.parent / "result.json").write_text(json.dumps(value), encoding="utf-8")

if mode == "success":
    event(0, "state", state="accepted")
    artifact = manifest.parent / "output.gcode"
    artifact.write_text("G1 X1\n", encoding="utf-8")
    metadata = [{
        "kind": "gcode",
        "path": "output.gcode",
        "size_bytes": artifact.stat().st_size,
        "sha256": "unused",
    }]
    result("succeeded", metadata)
    event(1, "artifact", artifact=metadata[0])
    event(2, "state", state="succeeded")
elif mode == "missing-artifact":
    event(0, "state", state="accepted")
    metadata = [{"kind": "gcode", "path": "missing.gcode", "size_bytes": 1, "sha256": "unused"}]
    result("succeeded", metadata)
    event(1, "artifact", artifact=metadata[0])
    event(2, "state", state="succeeded")
elif mode == "malformed":
    print("not-json", flush=True)
elif mode == "crash":
    event(0, "state", state="accepted")
    raise SystemExit(9)
elif mode == "event-spam":
    event(0, "state", state="accepted")
    for index in range(1, 10000):
        event(index, "progress", progress={"stage": "slicing", "percent": 1, "message": "x" * 128})
elif mode == "log-spam":
    event(0, "state", state="accepted")
    while True:
        print("secretless diagnostic " * 100, file=sys.stderr, flush=True)
elif mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    event(0, "state", state="accepted")
    while True:
        time.sleep(1)
elif mode == "memory-spam":
    event(0, "state", state="accepted")
    allocations = []
    while True:
        allocations.append(bytearray(8 * 1024 * 1024))
elif mode == "cpu-spam":
    event(0, "state", state="accepted")
    while True:
        pass
elif mode == "file-spam":
    event(0, "state", state="accepted")
    (manifest.parent / "oversized.bin").write_bytes(b"x" * (2 * 1024 * 1024))
elif mode == "process-spam":
    event(0, "state", state="accepted")
    children = []
    try:
        for unused in range(64):
            children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]))
    except OSError:
        print("PROCESS-LIMIT-REACHED", file=sys.stderr, flush=True)
        raise SystemExit(9)
    print("PROCESS-LIMIT-NOT-ENFORCED", file=sys.stderr, flush=True)
    raise SystemExit(10)
'''


@unittest.skipUnless(sys.platform == "linux", "executor requires Linux")
class WorkerExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_mode(self, mode, **limit_overrides):
        manifest = self.root / "request.json"
        manifest.write_text(json.dumps({"job_id": "executor-test", "mode": mode}), encoding="utf-8")
        defaults = {
            "wall_time_ms": 5_000,
            "cpu_time_ms": 5_000,
            "memory_bytes": 512 * 1024 * 1024,
            "output_bytes": 1024 * 1024,
            "process_count": 32,
            "open_files": 64,
            "termination_grace_ms": 100,
            "event_bytes": 64 * 1024,
            "event_line_bytes": 8 * 1024,
            "event_count": 128,
            "log_bytes": 32 * 1024,
        }
        defaults.update(limit_overrides)
        return execute_worker(
            [sys.executable, str(self.worker)], manifest, ExecutorLimits(**defaults)
        )

    def test_accepts_a_complete_worker_contract(self):
        execution = self.run_mode("success")
        self.assertTrue(execution.succeeded)
        self.assertEqual(execution.worker_exit_code, 0)
        self.assertEqual([event["sequence"] for event in execution.events], [0, 1, 2])

    def test_rejects_a_reused_job_directory(self):
        self.run_mode("success")
        with self.assertRaisesRegex(ExecutorError, "already contains result.json"):
            self.run_mode("success")

    def test_rejects_malformed_worker_output(self):
        execution = self.run_mode("malformed")
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "malformed_worker_output")

    def test_isolates_a_worker_crash(self):
        execution = self.run_mode("crash")
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "missing_terminal_event")
        self.assertEqual(execution.worker_exit_code, 9)

    def test_rejects_a_missing_declared_artifact(self):
        execution = self.run_mode("missing-artifact")
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "missing_worker_artifact")

    def test_stops_excessive_event_output(self):
        execution = self.run_mode("event-spam", event_count=8)
        self.assertEqual(execution.status, "output_limit")
        self.assertEqual(execution.error_code, "event_count_limit_exceeded")
        self.assertLessEqual(len(execution.events), 8)

    def test_stops_and_bounds_excessive_logs(self):
        execution = self.run_mode("log-spam", log_bytes=4096)
        self.assertEqual(execution.status, "output_limit")
        self.assertEqual(execution.error_code, "log_volume_limit_exceeded")
        self.assertLessEqual(len(execution.stderr.encode("utf-8")), 4096)
        self.assertGreater(execution.log_bytes, 4096)

    def test_forces_a_worker_that_ignores_the_grace_period(self):
        execution = self.run_mode("ignore-term", wall_time_ms=500)
        self.assertEqual(execution.status, "timed_out")
        self.assertEqual(execution.error_code, "executor_wall_time_exceeded")
        self.assertTrue(execution.forced_termination)

    def test_hard_memory_limit_stops_allocation_growth(self):
        execution = self.run_mode("memory-spam", memory_bytes=64 * 1024 * 1024)
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "missing_terminal_event")
        self.assertNotEqual(execution.worker_exit_code, 0)

    def test_hard_cpu_limit_stops_a_busy_worker(self):
        execution = self.run_mode("cpu-spam", cpu_time_ms=100, wall_time_ms=5_000)
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "missing_terminal_event")
        self.assertNotEqual(execution.worker_exit_code, 0)

    def test_hard_file_limit_stops_unchecked_output(self):
        execution = self.run_mode("file-spam", output_bytes=64 * 1024)
        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.error_code, "missing_terminal_event")
        self.assertNotEqual(execution.worker_exit_code, 0)
        self.assertLessEqual((self.root / "oversized.bin").stat().st_size, 64 * 1024)

    @unittest.skipIf(
        getattr(os, "geteuid", lambda: 0)() == 0,
        "RLIMIT_NPROC is not enforced for root",
    )
    def test_hard_process_limit_stops_child_process_growth(self):
        execution = self.run_mode("process-spam", process_count=8)
        self.assertEqual(execution.status, "failed")
        self.assertIn(execution.error_code, {"missing_terminal_event", "worker_left_descendants"})
        self.assertIn("PROCESS-LIMIT-REACHED", execution.stderr)


if __name__ == "__main__":
    unittest.main()
