#!/usr/bin/env python3

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import web_deployment_probe as deployment_probe  # noqa: E402


class DeploymentProbeTests(unittest.TestCase):
    def arguments(self, model: Path) -> argparse.Namespace:
        return argparse.Namespace(
            base_url="http://example.invalid",
            model=model,
            machine="Vendor/machine/Test",
            process="Vendor/process/Test",
            filament="Vendor/filament/Test",
            request_timeout=1,
            ready_timeout=1,
            job_timeout=1,
        )

    def test_waits_through_connection_and_not_ready_responses(self):
        args = self.arguments(Path("probe.stl"))
        with mock.patch.object(
            deployment_probe,
            "_json_request",
            side_effect=[
                RuntimeError("connection refused"),
                {"status": "not_ready"},
                {"status": "ready", "api_version": "v1", "protocol_version": 1},
            ],
        ), mock.patch.object(deployment_probe.time, "sleep"):
            ready = deployment_probe._wait_ready(args)
        self.assertEqual(ready["status"], "ready")

    def test_reports_a_terminal_job_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "probe.stl"
            model.write_bytes(b"solid probe\nendsolid probe\n")
            args = self.arguments(model)
            with mock.patch.object(
                deployment_probe,
                "_wait_ready",
                return_value={"status": "ready", "api_version": "v1", "protocol_version": 1},
            ), mock.patch.object(
                deployment_probe,
                "_request",
                return_value=(json.dumps({"upload_id": "upload-1"}).encode(), {}),
            ), mock.patch.object(
                deployment_probe,
                "_json_request",
                side_effect=[
                    {"job_id": "job-1"},
                    {"job_id": "job-1", "state": "failed", "error": {"code": "slice_failed"}},
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "ended as failed"):
                    deployment_probe.probe(args)

    def test_verifies_downloaded_gcode_against_artifact_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "probe.obj"
            model.write_bytes(b"v 0 0 0\n")
            args = self.arguments(model)
            gcode = b"G1 X1\n"
            digest = hashlib.sha256(gcode).hexdigest()
            job = {
                "job_id": "job-1",
                "state": "succeeded",
                "artifacts": [
                    {"name": "gcode", "size_bytes": len(gcode), "sha256": digest}
                ],
            }
            result = json.dumps({"job_id": "job-1", "outcome": "succeeded"}).encode()
            with mock.patch.object(
                deployment_probe,
                "_wait_ready",
                return_value={"status": "ready", "api_version": "v1", "protocol_version": 1},
            ), mock.patch.object(
                deployment_probe,
                "_request",
                side_effect=[
                    (json.dumps({"upload_id": "upload-1"}).encode(), {}),
                    (gcode, {}),
                    (result, {}),
                ],
            ), mock.patch.object(
                deployment_probe,
                "_json_request",
                side_effect=[{"job_id": "job-1"}, job],
            ):
                reported = deployment_probe.probe(args)
            self.assertEqual(reported["gcode_sha256"], digest)

            job["artifacts"][0]["sha256"] = "0" * 64
            with mock.patch.object(
                deployment_probe,
                "_wait_ready",
                return_value={"status": "ready", "api_version": "v1", "protocol_version": 1},
            ), mock.patch.object(
                deployment_probe,
                "_request",
                side_effect=[
                    (json.dumps({"upload_id": "upload-1"}).encode(), {}),
                    (gcode, {}),
                    (result, {}),
                ],
            ), mock.patch.object(
                deployment_probe,
                "_json_request",
                side_effect=[{"job_id": "job-1"}, job],
            ):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    deployment_probe.probe(args)


if __name__ == "__main__":
    unittest.main()
