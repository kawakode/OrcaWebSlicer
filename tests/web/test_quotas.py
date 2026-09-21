#!/usr/bin/env python3
"""Per-owner quotas: the ledger on its own, then enforced through the HTTP API."""

import importlib.util
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    FAKE_WORKER,
    FILAMENT_ID,
    MACHINE_ID,
    PROCESS_ID,
    VENDOR,
    generate_rsa_keypair,
    jwk_from_private_key,
    mint_assertion,
    write_jwks,
    write_profile_tree,
)
from web_job_directory import JobDirectoryLimits  # noqa: E402

from web.api.errors import ApiError  # noqa: E402
from web.api.quotas import QuotaLedger, QuotaLimits, limits_from_environment  # noqa: E402

HAS_DEPS = (
    importlib.util.find_spec("fastapi") is not None
    and importlib.util.find_spec("httpx") is not None
    and importlib.util.find_spec("jwt") is not None
    and importlib.util.find_spec("cryptography") is not None
)

OWNER = "a" * 64
OTHER = "b" * 64


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class QuotaLedgerTests(unittest.TestCase):
    def ledger(self, **limits):
        self.clock = Clock()
        return QuotaLedger(QuotaLimits(**limits), clock=self.clock)

    def assertQuotaError(self, code, call, *args):
        with self.assertRaises(ApiError) as raised:
            call(*args)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.status, 429)
        return raised.exception

    def test_active_jobs_are_bounded_per_owner(self):
        ledger = self.ledger(max_active_jobs=2)
        ledger.check_submission(OWNER, 1)
        error = self.assertQuotaError("concurrent_job_quota_exceeded", ledger.check_submission, OWNER, 2)
        # Freed by a job finishing, not by time, so there is nothing to wait for.
        self.assertIsNone(error.retry_after)

    def test_submissions_expire_out_of_the_rolling_window(self):
        ledger = self.ledger(max_submissions=2, window_seconds=60)
        ledger.record_submission(OWNER)
        self.clock.now += 10
        ledger.record_submission(OWNER)
        error = self.assertQuotaError("job_submission_quota_exceeded", ledger.check_submission, OWNER, 0)
        self.assertEqual(error.retry_after, 50)
        ledger.check_submission(OTHER, 0)
        self.clock.now += 50
        ledger.check_submission(OWNER, 0)
        self.assertEqual(ledger.usage(OWNER), (0, 1, 0))

    def test_cpu_budget_waits_for_the_charges_that_overspend_it(self):
        ledger = self.ledger(cpu_time_ms=1000, window_seconds=100)
        ledger.charge_cpu(OWNER, 600)
        self.clock.now += 20
        ledger.charge_cpu(OWNER, 600)
        ledger.charge_cpu(OWNER, 0)
        # 1200 ms spent; aging out the first charge is enough to go under.
        error = self.assertQuotaError("cpu_time_quota_exceeded", ledger.check_submission, OWNER, 0)
        self.assertEqual(error.retry_after, 80)
        self.clock.now += 80
        ledger.check_submission(OWNER, 0)
        self.assertEqual(ledger.usage(OWNER)[0], 600)

    def test_storage_reservations_share_one_budget_until_released(self):
        ledger = self.ledger(max_storage_bytes=1000)
        first = ledger.reserve_storage(OWNER, 200, 500)
        second = ledger.reserve_storage(OWNER, 200, 500)
        self.assertEqual((first, second), (500, 300))
        error = self.assertQuotaError("storage_quota_exceeded", ledger.reserve_storage, OWNER, 200, 1)
        self.assertIsNone(error.retry_after)
        self.assertEqual(ledger.reserve_storage(OTHER, 0, 500), 500)
        ledger.release_storage(OWNER, first)
        self.assertEqual(ledger.usage(OWNER)[2], 300)
        ledger.release_storage(OWNER, second)
        self.assertEqual(ledger.usage(OWNER)[2], 0)

    def test_limits_must_be_positive_integers(self):
        for field in ("max_active_jobs", "max_storage_bytes", "cpu_time_ms", "max_submissions", "window_seconds"):
            for value in (0, -1, True):
                with self.subTest(field=field, value=value), self.assertRaises(ApiError):
                    QuotaLimits(**{field: value}).validate()

    def test_limits_are_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ORCA_WEB_QUOTA_ACTIVE_JOBS": "3", "ORCA_WEB_QUOTA_WINDOW_SECONDS": "60"}):
            limits = limits_from_environment()
        self.assertEqual((limits.max_active_jobs, limits.window_seconds), (3, 60))
        self.assertEqual(limits.max_submissions, QuotaLimits().max_submissions)
        for value in ("many", "0"):
            with mock.patch.dict(os.environ, {"ORCA_WEB_QUOTA_SUBMISSIONS": value}), self.assertRaises(ApiError):
                limits_from_environment()


@unittest.skipUnless(HAS_DEPS, "fastapi, httpx, PyJWT, and cryptography are required")
@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
class QuotaApiTests(unittest.TestCase):
    ISSUER = "https://edge.example.test"
    AUDIENCE = "orca-web-api"

    def setUp(self):
        from web.api.config import ApiConfig

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        key = generate_rsa_keypair()
        self.jwks_path = write_jwks(self.root / "jwks.json", [jwk_from_private_key(key, "key-1")])
        self.ApiConfig = ApiConfig
        self.headers_a = {"Authorization": "Bearer " + mint_assertion(key, "key-1", self.ISSUER, self.AUDIENCE, "a")}
        self.headers_b = {"Authorization": "Bearer " + mint_assertion(key, "key-1", self.ISSUER, self.AUDIENCE, "b")}
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.__exit__(None, None, None)
        self.temporary.cleanup()

    def start(self, mode="success", **quotas):
        from fastapi.testclient import TestClient

        from test_job_service import TEST_LIMITS
        from web.api.app import create_app

        config = self.ApiConfig(
            repo_root=self.root,
            state_root=self.root / "state",
            worker_command=(sys.executable, str(self.worker), mode),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=2,
            executor_limits=TEST_LIMITS,
            job_limits=JobDirectoryLimits(retention_seconds=3600, max_input_bytes=4096),
            quotas=QuotaLimits(**quotas),
            auth_mode="required",
            auth_issuer=self.ISSUER,
            auth_audience=self.AUDIENCE,
            auth_jwks_path=self.jwks_path,
        )
        client = TestClient(create_app(config))
        client.__enter__()
        self.clients.append(client)
        self.client = client
        return client

    def upload(self, headers, contents=b"solid cube\nendsolid cube\n"):
        return self.client.post(
            "/api/v1/uploads",
            files={"file": ("cube.stl", io.BytesIO(contents), "application/octet-stream")},
            headers=headers,
        )

    def upload_id(self, headers):
        response = self.upload(headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["upload_id"]

    def submit(self, headers, upload_id):
        body = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        return self.client.post("/api/v1/jobs", json=body, headers=headers)

    def submit_scene(self, headers, upload_id):
        body = {"machine_profile": MACHINE_ID, "process_profile": PROCESS_ID, "filament_profile": FILAMENT_ID}
        return self.client.post(f"/api/v1/uploads/{upload_id}/scene", json=body, headers=headers)

    def wait(self, job_id, headers, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            described = self.client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            if described["state"] in {"succeeded", "failed", "canceled"}:
                return described
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish within {timeout}s")

    def assertQuotaRefusal(self, response, code):
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        return response

    def job_directories(self):
        jobs = self.root / "state" / "jobs"
        return sorted(path.name for path in jobs.iterdir()) if jobs.is_dir() else []

    def test_quota_endpoint_reports_limits_and_the_callers_own_usage(self):
        self.start(max_active_jobs=3, max_storage_bytes=100_000)
        upload_id = self.upload_id(self.headers_a)
        job = self.submit(self.headers_a, upload_id).json()
        self.wait(job["job_id"], self.headers_a)

        quota = self.client.get("/api/v1/quota", headers=self.headers_a).json()
        self.assertEqual(quota["limits"]["max_active_jobs"], 3)
        self.assertEqual(quota["limits"]["max_storage_bytes"], 100_000)
        usage = quota["usage"]
        self.assertEqual((usage["active_jobs"], usage["submissions"]), (0, 1))
        # The upload, plus the job directory with its staged copy and output.
        self.assertGreater(usage["storage_bytes"], 2 * len(b"solid cube\nendsolid cube\n"))
        self.assertGreater(usage["cpu_time_ms"], 0)

        other = self.client.get("/api/v1/quota", headers=self.headers_b).json()["usage"]
        self.assertEqual(other, {"active_jobs": 0, "storage_bytes": 0, "cpu_time_ms": 0, "submissions": 0})

    def test_upload_beyond_the_storage_quota_is_refused_and_discarded(self):
        self.start(max_storage_bytes=4096)
        self.assertEqual(self.upload(self.headers_a, b"x" * 3000).status_code, 201)
        response = self.assertQuotaRefusal(self.upload(self.headers_a, b"y" * 2000), "storage_quota_exceeded")
        self.assertNotIn("Retry-After", response.headers)
        self.assertEqual(len(list((self.root / "state" / "uploads").iterdir())), 1)
        # One owner's usage never spends another's budget.
        self.assertEqual(self.upload(self.headers_b, b"z" * 3000).status_code, 201)
        # The service-wide size limit is still its own, distinct failure.
        self.assertEqual(self.upload(self.headers_b, b"z" * 5000).json()["error"]["code"], "upload_size_limit_exceeded")

    def test_a_job_that_would_exceed_the_storage_quota_is_never_staged(self):
        self.start(max_storage_bytes=4096)
        upload_id = self.upload_id(self.headers_a)
        self.assertEqual(self.upload(self.headers_a, b"x" * 4060).status_code, 201)
        self.assertQuotaRefusal(self.submit(self.headers_a, upload_id), "storage_quota_exceeded")
        self.assertEqual(self.job_directories(), [])

    def test_concurrent_jobs_are_bounded_per_owner(self):
        self.start(mode="slow", max_active_jobs=1)
        upload_a = self.upload_id(self.headers_a)
        running = self.submit(self.headers_a, upload_a)
        self.assertEqual(running.status_code, 202)
        refused = self.assertQuotaRefusal(self.submit(self.headers_a, upload_a), "concurrent_job_quota_exceeded")
        self.assertNotIn("Retry-After", refused.headers)
        self.assertEqual(len(self.job_directories()), 1)

        upload_b = self.upload_id(self.headers_b)
        other = self.submit(self.headers_b, upload_b)
        self.assertEqual(other.status_code, 202)

        for job, headers in ((running.json(), self.headers_a), (other.json(), self.headers_b)):
            self.client.post(f"/api/v1/jobs/{job['job_id']}/cancel", headers=headers)
            self.wait(job["job_id"], headers)
        self.assertEqual(self.submit(self.headers_a, upload_a).status_code, 202)

    def test_submissions_are_bounded_per_window_including_retries(self):
        self.start(max_submissions=2, max_active_jobs=5)
        upload_id = self.upload_id(self.headers_a)
        first = self.submit(self.headers_a, upload_id).json()
        self.wait(first["job_id"], self.headers_a)
        retried = self.client.post(f"/api/v1/jobs/{first['job_id']}/retry", headers=self.headers_a)
        self.assertEqual(retried.status_code, 202)
        response = self.assertQuotaRefusal(self.submit(self.headers_a, upload_id), "job_submission_quota_exceeded")
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        self.assertIn("X-Correlation-Id", response.headers)
        self.assertEqual(self.submit(self.headers_b, self.upload_id(self.headers_b)).status_code, 202)

    def test_a_repeated_scene_request_is_not_charged_again(self):
        self.start(max_submissions=1)
        upload_id = self.upload_id(self.headers_a)
        scene = self.submit_scene(self.headers_a, upload_id)
        self.assertEqual(scene.status_code, 202)
        self.wait(scene.json()["job_id"], self.headers_a)
        again = self.submit_scene(self.headers_a, upload_id)
        self.assertEqual(again.status_code, 202)
        self.assertEqual(again.json()["job_id"], scene.json()["job_id"])
        self.assertQuotaRefusal(self.submit(self.headers_a, upload_id), "job_submission_quota_exceeded")

    def test_spent_cpu_budget_refuses_the_next_job(self):
        # The fake worker reports 2 ms; the charge is at least the wall time
        # the executor measured, so a 1 ms budget is spent by one job.
        self.start(cpu_time_ms=1)
        upload_id = self.upload_id(self.headers_a)
        self.wait(self.submit(self.headers_a, upload_id).json()["job_id"], self.headers_a)
        response = self.assertQuotaRefusal(self.submit(self.headers_a, upload_id), "cpu_time_quota_exceeded")
        self.assertIn("Retry-After", response.headers)

    def test_storage_quota_smaller_than_the_input_limit_is_a_configuration_error(self):
        with self.assertRaises(ApiError) as raised:
            self.start(max_storage_bytes=1024)
        self.assertEqual(raised.exception.code, "invalid_api_configuration")


if __name__ == "__main__":
    unittest.main()
