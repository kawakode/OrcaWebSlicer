#!/usr/bin/env python3

import importlib.util
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    FAKE_WORKER,
    FILAMENT_ID,
    MACHINE_ID,
    OTHER_MACHINE_ID,
    OTHER_PROCESS_ID,
    PROCESS_ID,
    VENDOR,
    write_profile_tree,
)
from test_job_service import TEST_LIMITS  # noqa: E402
from web_job_directory import JobDirectoryLimits  # noqa: E402

from web.api.config import ApiConfig  # noqa: E402

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("httpx") is not None


@unittest.skipUnless(HAS_FASTAPI, "fastapi and httpx are required for the API tests")
@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from web.api.app import create_app

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        config = ApiConfig(
            repo_root=self.root,
            state_root=self.root / "state",
            # The mode is bound late so a test can pick the worker outcome.
            worker_command=(sys.executable, str(self.worker), "success"),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=2,
            executor_limits=TEST_LIMITS,
            job_limits=JobDirectoryLimits(retention_seconds=3600),
        )
        self.client = TestClient(create_app(config))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def upload(self, name="cube.stl", contents=b"solid cube\nendsolid cube\n"):
        return self.client.post(
            "/api/v1/uploads", files={"file": (name, io.BytesIO(contents), "application/octet-stream")}
        )

    def submit(self, **overrides):
        upload = self.upload()
        self.assertEqual(upload.status_code, 201)
        body = {
            "upload_id": upload.json()["upload_id"],
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        body.update(overrides)
        return self.client.post("/api/v1/jobs", json=body)

    def wait(self, job_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            if response.json()["state"] in {"succeeded", "failed", "canceled"}:
                return response.json()
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish within {timeout}s")

    def test_reports_health_and_publishes_a_schema(self):
        health = self.client.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["protocol_version"], 1)

        schema = self.client.get("/openapi.json")
        self.assertEqual(schema.status_code, 200)
        document = schema.json()
        self.assertIn("/api/v1/jobs/{job_id}/artifacts/{name}", document["paths"])
        submission = document["components"]["schemas"]["SliceSubmission"]
        self.assertEqual(
            sorted(submission["required"]),
            ["filament_profile", "machine_profile", "process_profile", "upload_id"],
        )

    def test_lists_bundled_profiles_and_narrows_them_to_a_printer(self):
        response = self.client.get("/api/v1/profiles")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(
            [entry["profile_id"] for entry in catalog["machine"]], [OTHER_MACHINE_ID, MACHINE_ID]
        )
        self.assertEqual(len(catalog["process"]), 2)

        narrowed = self.client.get("/api/v1/profiles", params={"printer": MACHINE_ID}).json()
        self.assertEqual([entry["profile_id"] for entry in narrowed["process"]], [PROCESS_ID])
        self.assertEqual([entry["profile_id"] for entry in narrowed["filament"]], [FILAMENT_ID])

        unknown = self.client.get("/api/v1/profiles", params={"printer": "Testing/machine/Nope"})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_profile")

    def test_accepts_supported_models_and_refuses_the_rest(self):
        accepted = self.upload("cube.STL")
        self.assertEqual(accepted.status_code, 201)
        body = accepted.json()
        self.assertEqual(body["format"], "stl")
        self.assertEqual(body["size_bytes"], len(b"solid cube\nendsolid cube\n"))
        self.assertEqual(len(body["sha256"]), 64)

        rejected = self.upload("payload.gcode")
        self.assertEqual(rejected.status_code, 415)
        self.assertEqual(rejected.json()["error"]["code"], "unsupported_model_format")
        self.assertEqual(self.upload("empty.stl", b"").status_code, 400)
        # A traversing filename is reduced to a display name, never a path.
        traversing = self.upload("../../etc/passwd.obj")
        self.assertEqual(traversing.json()["filename"], "passwd.obj")

    def test_slices_an_upload_and_serves_both_artifacts(self):
        accepted = self.submit(settings={"layer_height": "0.28"})
        self.assertEqual(accepted.status_code, 202)
        job_id = accepted.json()["job_id"]

        job = self.wait(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual([warning["code"] for warning in job["warnings"]], ["thin_wall"])

        gcode = self.client.get(f"/api/v1/jobs/{job_id}/artifacts/gcode")
        self.assertEqual(gcode.status_code, 200)
        self.assertIn("layer_height = 0.28", gcode.text)
        self.assertIn(f"{job_id}.gcode", gcode.headers["content-disposition"])

        result = self.client.get(f"/api/v1/jobs/{job_id}/artifacts/result")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["outcome"], "succeeded")

    def test_refuses_an_artifact_before_it_is_published(self):
        accepted = self.submit()
        job_id = accepted.json()["job_id"]
        early = self.client.get(f"/api/v1/jobs/{job_id}/artifacts/gcode")
        if early.status_code != 200:
            self.assertEqual(early.status_code, 409)
            self.assertEqual(early.json()["error"]["code"], "artifact_unavailable")
        self.wait(job_id)
        self.assertEqual(
            self.client.get(f"/api/v1/jobs/{job_id}/artifacts/manifest").status_code, 422
        )

    def test_cancels_and_retries_a_job(self):
        accepted = self.submit()
        job_id = accepted.json()["job_id"]
        self.wait(job_id)

        finished = self.client.post(f"/api/v1/jobs/{job_id}/cancel")
        self.assertEqual(finished.status_code, 409)
        self.assertEqual(finished.json()["error"]["code"], "job_not_cancelable")

        retried = self.client.post(f"/api/v1/jobs/{job_id}/retry")
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()["retry_of"], job_id)
        self.assertEqual(retried.json()["request"], accepted.json()["request"])
        self.assertEqual(self.wait(retried.json()["job_id"])["state"], "succeeded")
        self.assertEqual(len(self.client.get("/api/v1/jobs").json()["jobs"]), 2)

    def test_validates_the_slice_request_before_it_reaches_a_worker(self):
        upload_id = self.upload().json()["upload_id"]
        valid = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        for overrides, status in (
            ({"plate_index": 0}, 422),
            ({"settings": {"layer_height": 0.2}}, 422),
            ({"unexpected": "field"}, 422),
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.client.post("/api/v1/jobs", json={**valid, **overrides}).status_code, status)
        self.assertEqual(
            self.client.post("/api/v1/jobs", json={k: v for k, v in valid.items() if k != "upload_id"}).status_code,
            422,
        )

        incompatible = self.client.post(
            "/api/v1/jobs", json={**valid, "process_profile": OTHER_PROCESS_ID}
        )
        self.assertEqual(incompatible.status_code, 409)
        self.assertEqual(incompatible.json()["error"]["code"], "incompatible_profile")
        self.assertEqual(self.client.get("/api/v1/jobs").json()["jobs"], [])

    def test_reports_an_unknown_job_without_touching_the_filesystem(self):
        for job_id in ("job-missing", "../../etc", "job-" + "x" * 200):
            with self.subTest(job_id=job_id):
                response = self.client.get(f"/api/v1/jobs/{job_id}")
                self.assertIn(response.status_code, {404, 422})

    def test_carries_a_correlation_id_through_every_response(self):
        generated = self.client.get("/api/v1/health")
        self.assertRegex(generated.headers["x-correlation-id"], r"^[0-9a-f]{32}$")

        supplied = self.client.post(
            "/api/v1/jobs",
            json={"upload_id": "0" * 32, "machine_profile": MACHINE_ID, "process_profile": PROCESS_ID,
                  "filament_profile": FILAMENT_ID},
            headers={"X-Correlation-Id": "trace-42"},
        )
        self.assertEqual(supplied.status_code, 404)
        self.assertEqual(supplied.headers["x-correlation-id"], "trace-42")
        self.assertEqual(supplied.json()["correlation_id"], "trace-42")

        # A submitted job keeps the correlation ID of the request that created it.
        accepted = self.client.post(
            "/api/v1/jobs",
            json={"upload_id": self.upload().json()["upload_id"], "machine_profile": MACHINE_ID,
                  "process_profile": PROCESS_ID, "filament_profile": FILAMENT_ID},
            headers={"X-Correlation-Id": "trace-99"},
        )
        self.assertEqual(accepted.json()["correlation_id"], "trace-99")
        self.assertEqual(self.wait(accepted.json()["job_id"])["correlation_id"], "trace-99")

        rejected = self.client.get("/api/v1/health", headers={"X-Correlation-Id": "not a valid id"})
        self.assertRegex(rejected.headers["x-correlation-id"], r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
