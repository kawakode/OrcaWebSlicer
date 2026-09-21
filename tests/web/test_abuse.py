#!/usr/bin/env python3
"""Abuse controls: the rate limiter on its own, then the guard in front of the API."""

import asyncio
import importlib.util
import io
import json
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

from web.api.abuse import RateLimiter, RateLimits, limits_from_environment  # noqa: E402
from web.api.errors import ApiError  # noqa: E402

HAS_DEPS = (
    importlib.util.find_spec("fastapi") is not None
    and importlib.util.find_spec("httpx") is not None
    and importlib.util.find_spec("jwt") is not None
    and importlib.util.find_spec("cryptography") is not None
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RateLimiterTests(unittest.TestCase):
    def limiter(self, **limits):
        self.clock = Clock()
        return RateLimiter(RateLimits(**limits), clock=self.clock)

    def test_a_burst_is_admitted_then_refused_until_a_token_refills(self):
        limiter = self.limiter(requests_per_minute=60, request_burst=3)
        self.assertEqual([limiter.acquire("a") for _ in range(3)], [0, 0, 0])
        self.assertAlmostEqual(limiter.acquire("a"), 1.0)
        # Another key has its own bucket.
        self.assertEqual(limiter.acquire("b"), 0)
        self.clock.now += 1.0
        self.assertEqual(limiter.acquire("a"), 0)
        self.assertGreater(limiter.acquire("a"), 0)

    def test_an_idle_bucket_refills_only_to_its_burst(self):
        limiter = self.limiter(requests_per_minute=60, request_burst=2)
        limiter.acquire("a")
        limiter.acquire("a")
        self.clock.now += 3600
        self.assertEqual([limiter.acquire("a") for _ in range(2)], [0, 0])
        self.assertGreater(limiter.acquire("a"), 0)

    def test_the_key_table_is_bounded_forgetting_the_least_recent_first(self):
        limiter = self.limiter(requests_per_minute=60, request_burst=1, max_tracked_owners=2)
        limiter.acquire("a")
        limiter.acquire("b")
        limiter.acquire("a")  # refused, and now the most recent
        limiter.acquire("c")  # evicts "b"
        self.assertEqual(len(limiter._buckets), 2)
        self.assertEqual(limiter.acquire("b"), 0)
        self.assertGreater(limiter.acquire("c"), 0)

    def test_limits_must_be_positive_integers_and_are_read_from_the_environment(self):
        for field in ("requests_per_minute", "request_burst", "max_json_body_bytes", "max_tracked_owners"):
            for value in (0, -1, True):
                with self.subTest(field=field, value=value), self.assertRaises(ApiError):
                    RateLimits(**{field: value}).validate()
        with mock.patch.dict(os.environ, {"ORCA_WEB_RATE_REQUEST_BURST": "7"}):
            limits = limits_from_environment()
        self.assertEqual(limits.request_burst, 7)
        self.assertEqual(limits.requests_per_minute, RateLimits().requests_per_minute)
        with mock.patch.dict(os.environ, {"ORCA_WEB_MAX_JSON_BODY_BYTES": "lots"}), self.assertRaises(ApiError):
            limits_from_environment()


def call_asgi(app, method, path, headers=(), chunks=()):
    """Drive the ASGI app directly, recording whether it ever read the body."""
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    reads = []
    sent = []

    async def receive():
        reads.append(True)
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], json.loads(body), bool(reads)


@unittest.skipUnless(HAS_DEPS, "fastapi, httpx, PyJWT, and cryptography are required")
@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
class AbuseGuardApiTests(unittest.TestCase):
    ISSUER = "https://edge.example.test"
    AUDIENCE = "orca-web-api"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        key = generate_rsa_keypair()
        self.jwks_path = write_jwks(self.root / "jwks.json", [jwk_from_private_key(key, "key-1")])
        self.headers_a = {"Authorization": "Bearer " + mint_assertion(key, "key-1", self.ISSUER, self.AUDIENCE, "a")}
        self.headers_b = {"Authorization": "Bearer " + mint_assertion(key, "key-1", self.ISSUER, self.AUDIENCE, "b")}
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.__exit__(None, None, None)
        self.temporary.cleanup()

    def app(self, **rate_limits):
        from test_job_service import TEST_LIMITS
        from web.api.app import create_app
        from web.api.config import ApiConfig

        return create_app(
            ApiConfig(
                repo_root=self.root,
                state_root=self.root / "state",
                worker_command=(sys.executable, str(self.worker), "success"),
                profile_vendors=(VENDOR,),
                executor_limits=TEST_LIMITS,
                job_limits=JobDirectoryLimits(retention_seconds=3600, max_input_bytes=4096),
                rate_limits=RateLimits(**rate_limits),
                auth_mode="required",
                auth_issuer=self.ISSUER,
                auth_audience=self.AUDIENCE,
                auth_jwks_path=self.jwks_path,
            )
        )

    def start(self, **rate_limits):
        from fastapi.testclient import TestClient

        client = TestClient(self.app(**rate_limits))
        client.__enter__()
        self.clients.append(client)
        self.client = client
        return client

    def upload(self, headers):
        return self.client.post(
            "/api/v1/uploads",
            files={"file": ("cube.stl", io.BytesIO(b"solid cube\nendsolid cube\n"), "application/octet-stream")},
            headers=headers,
        )

    def test_an_owner_past_its_burst_is_refused_with_a_stable_retryable_error(self):
        self.start(requests_per_minute=1, request_burst=2)
        self.assertEqual(self.client.get("/api/v1/quota", headers=self.headers_a).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers_a).status_code, 200)
        refused = self.client.get("/api/v1/jobs", headers={**self.headers_a, "X-Correlation-Id": "abuse-1"})
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json()["error"]["code"], "request_rate_limited")
        self.assertEqual(refused.json()["correlation_id"], "abuse-1")
        self.assertEqual(refused.headers["X-Correlation-Id"], "abuse-1")
        self.assertEqual(refused.headers["Retry-After"], "60")
        # Refused before the route ran: nothing was stored.
        self.assertEqual(self.upload(self.headers_a).status_code, 429)
        self.assertFalse((self.root / "state" / "uploads").is_dir() and any((self.root / "state" / "uploads").iterdir()))
        # Another owner, and the public probes, are unaffected.
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers_b).status_code, 200)
        for _ in range(3):
            self.assertEqual(self.client.get("/api/v1/health/ready").status_code, 200)

    def test_refused_reads_leave_a_running_job_and_its_outcome_untouched(self):
        self.start(requests_per_minute=1, request_burst=2)
        upload_id = self.upload(self.headers_a).json()["upload_id"]
        body = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        job = self.client.post("/api/v1/jobs", json=body, headers=self.headers_a)
        self.assertEqual(job.status_code, 202, job.text)
        job_id = job.json()["job_id"]
        from web.api.auth import derive_owner_id

        service = self.client.app.state.jobs
        owner_a = derive_owner_id(self.ISSUER, "a")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            refused = self.client.get(f"/api/v1/jobs/{job_id}", headers=self.headers_a)
            self.assertEqual(refused.json()["error"]["code"], "request_rate_limited")
            described = service.get(job_id, owner_a)
            if described["state"] == "succeeded":
                break
            time.sleep(0.05)
        self.assertEqual(described["state"], "succeeded")
        self.assertIsNone(described.get("error"))

    def test_an_unauthenticated_upload_is_refused_before_its_body_is_read(self):
        app = self.app()
        status, body, read = call_asgi(
            app, "POST", "/api/v1/uploads", [("content-type", "multipart/form-data; boundary=x")], [b"x" * 2048] * 4
        )
        self.assertEqual((status, body["error"]["code"]), (401, "authentication_required"))
        self.assertFalse(read)

    def test_a_declared_oversized_body_is_refused_before_authentication(self):
        app = self.app(max_json_body_bytes=1024)
        status, body, read = call_asgi(app, "POST", "/api/v1/jobs", [("content-length", "1025")], [b"{" * 1025])
        self.assertEqual((status, body["error"]["code"]), (413, "request_body_too_large"))
        self.assertFalse(read)
        # An upload is allowed its input limit plus multipart framing.
        status, body, _ = call_asgi(app, "POST", "/api/v1/uploads", [("content-length", str(4096 + 1024 * 1024 + 1))])
        self.assertEqual((status, body["error"]["code"]), (413, "request_body_too_large"))

    def test_a_chunked_body_is_cut_off_once_it_streams_past_its_cap(self):
        app = self.app(max_json_body_bytes=1024)
        headers = [("content-type", "application/json"), *self.headers_a.items()]
        status, body, read = call_asgi(app, "POST", "/api/v1/jobs", headers, [b" " * 600, b" " * 600, b"{}"])
        self.assertEqual((status, body["error"]["code"]), (413, "request_body_too_large"))
        self.assertTrue(read)

    def test_the_service_wide_upload_limit_keeps_its_own_code(self):
        self.start()
        response = self.client.post(
            "/api/v1/uploads",
            files={"file": ("cube.stl", io.BytesIO(b"x" * 5000), "application/octet-stream")},
            headers=self.headers_a,
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "upload_size_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
