#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FAKE_WORKER, FILAMENT_ID, MACHINE_ID, PROCESS_ID, VENDOR, write_profile_tree  # noqa: E402
from web_deployment_probe import _json_request, _request, _upload_body, _wait_ready  # noqa: E402
from web_worker_executor import ExecutorLimits  # noqa: E402

from web.api.app import create_app  # noqa: E402
from web.api.config import ApiConfig  # noqa: E402


HAS_SERVER = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("uvicorn") is not None


def create_lifecycle_app():
    """Uvicorn factory with the test worker expressed as an argv tuple."""
    root = Path(os.environ["ORCA_LIFECYCLE_ROOT"])
    return create_app(
        ApiConfig(
            repo_root=root,
            state_root=root / "state",
            worker_command=(sys.executable, os.environ["ORCA_LIFECYCLE_WORKER"], "slow"),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=1,
            executor_limits=ExecutorLimits(termination_grace_ms=200),
            # This suite exercises shutdown timing, not identity.
            auth_mode="disabled",
            # The production shape: every line the process writes is JSON.
            log_format="json",
        )
    )


@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
@unittest.skipUnless(HAS_SERVER, "fastapi and uvicorn are required for the lifecycle test")
class ApiLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        worker = self.root / "fake_worker.py"
        worker.write_text(
            FAKE_WORKER.replace("import hashlib\n", "import hashlib\nimport os\n").replace(
                "job_root = manifest.parent\n",
                "job_root = manifest.parent\n(job_root / 'worker.pid').write_text(str(os.getpid()))\n",
            ),
            encoding="utf-8",
        )
        self.state_root = self.root / "state"
        self.port = self._unused_port()
        environment = os.environ.copy()
        environment.update(
            {
                "ORCA_LIFECYCLE_ROOT": str(self.root),
                "ORCA_LIFECYCLE_WORKER": str(worker),
            }
        )
        self.log = (self.root / "uvicorn.log").open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.web.test_api_lifecycle:create_lifecycle_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--timeout-graceful-shutdown",
                "2",
            ],
            cwd=REPO_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self.base_url = f"http://127.0.0.1:{self.port}"
        _wait_ready(
            argparse.Namespace(
                base_url=self.base_url,
                request_timeout=1,
                ready_timeout=20,
            )
        )

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.temporary.cleanup()

    @staticmethod
    def _unused_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _upload(self, model: Path) -> str:
        body, boundary = _upload_body(model)
        raw, _headers = _request(
            self.base_url,
            "/api/v1/uploads",
            method="POST",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return str(json.loads(raw.decode("utf-8"))["upload_id"])

    def _submit(self, upload_id: str) -> str:
        accepted = _json_request(
            self.base_url,
            "/api/v1/jobs",
            method="POST",
            value={
                "upload_id": upload_id,
                "machine_profile": MACHINE_ID,
                "process_profile": PROCESS_ID,
                "filament_profile": FILAMENT_ID,
            },
        )
        return str(accepted["job_id"])

    def _wait_state(self, job_id: str, state: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = _json_request(self.base_url, f"/api/v1/jobs/{job_id}")
            if job.get("state") == state:
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not reach {state}")

    def test_sigterm_cancels_running_and_queued_jobs_before_exit(self):
        model = self.root / "probe.stl"
        model.write_bytes(b"solid probe\nendsolid probe\n")
        upload_id = self._upload(model)
        running = self._submit(upload_id)
        self._wait_state(running, "running")
        queued = self._submit(upload_id)
        self._wait_state(queued, "queued")

        pid_path = self.state_root / "jobs" / running / "worker.pid"
        deadline = time.monotonic() + 5
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_path.is_file())
        worker_pid = int(pid_path.read_text(encoding="utf-8"))

        started = time.monotonic()
        self.process.terminate()
        self.process.wait(timeout=10)
        self.assertLess(time.monotonic() - started, 10)
        # A direct Uvicorn process exits 0; the same graceful lifecycle behind
        # an init shim may preserve SIGTERM's conventional 143/-15 status.
        self.assertIn(self.process.returncode, (0, -15))
        log = (self.root / "uvicorn.log").read_text(encoding="utf-8")
        records = [json.loads(line) for line in log.splitlines()]
        events = [record["event"] for record in records]
        self.assertIn("Application shutdown complete.", events)
        self.assertIn("api.stopped", events)
        finished = {record["job_id"]: record["state"] for record in records if record["event"] == "job.finished"}
        self.assertEqual(finished, {running: "canceled", queued: "canceled"})
        self.assertFalse((self.state_root / "jobs" / running).exists())
        self.assertFalse((self.state_root / "jobs" / queued).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker_pid, 0)


if __name__ == "__main__":
    unittest.main()
