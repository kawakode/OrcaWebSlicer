#!/usr/bin/env python3
"""Two-user ownership isolation over HTTP, and the legacy-upload transition.

Verifier behavior and config defaults are covered by test_auth.py; this file
drives the real FastAPI app in `required` mode with two distinct minted
principals and checks that neither can observe or act on the other's uploads,
jobs, artifacts, previews, or scenes.
"""

import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

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
from test_job_service import TEST_LIMITS  # noqa: E402
from web_job_directory import JobDirectoryLimits  # noqa: E402

HAS_DEPS = (
    importlib.util.find_spec("fastapi") is not None
    and importlib.util.find_spec("httpx") is not None
    and importlib.util.find_spec("jwt") is not None
    and importlib.util.find_spec("cryptography") is not None
)


@unittest.skipUnless(HAS_DEPS, "fastapi, httpx, PyJWT, and cryptography are required")
@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
class OwnershipTests(unittest.TestCase):
    ISSUER = "https://edge.example.test"
    AUDIENCE = "orca-web-api"

    def setUp(self):
        from fastapi.testclient import TestClient

        from web.api.app import create_app
        from web.api.config import ApiConfig

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.key = generate_rsa_keypair()
        self.jwks_path = write_jwks(self.root / "jwks.json", [jwk_from_private_key(self.key, "key-1")])
        self.create_app = create_app
        self.ApiConfig = ApiConfig
        self.client = TestClient(create_app(self.config()))
        self.client.__enter__()
        self.token_a = mint_assertion(self.key, "key-1", self.ISSUER, self.AUDIENCE, "user-a")
        self.token_b = mint_assertion(self.key, "key-1", self.ISSUER, self.AUDIENCE, "user-b")
        self.headers_a = {"Authorization": f"Bearer {self.token_a}"}
        self.headers_b = {"Authorization": f"Bearer {self.token_b}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def config(self, mode="success", auth_mode="required", state_root=None):
        return self.ApiConfig(
            repo_root=self.root,
            state_root=state_root or (self.root / "state"),
            worker_command=(sys.executable, str(self.worker), mode),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=2,
            executor_limits=TEST_LIMITS,
            job_limits=JobDirectoryLimits(retention_seconds=3600),
            auth_mode=auth_mode,
            auth_issuer=self.ISSUER,
            auth_audience=self.AUDIENCE,
            auth_jwks_path=self.jwks_path,
        )

    def upload(self, headers, name="cube.stl", contents=b"solid cube\nendsolid cube\n"):
        return self.client.post(
            "/api/v1/uploads",
            files={"file": (name, io.BytesIO(contents), "application/octet-stream")},
            headers=headers,
        )

    def submit(self, headers, upload_id, **overrides):
        body = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        body.update(overrides)
        return self.client.post("/api/v1/jobs", json=body, headers=headers)

    def submit_scene(self, headers, upload_id, **overrides):
        body = {
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        body.update(overrides)
        return self.client.post(f"/api/v1/uploads/{upload_id}/scene", json=body, headers=headers)

    def wait(self, job_id, headers, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v1/jobs/{job_id}", headers=headers)
            self.assertEqual(response.status_code, 200)
            if response.json()["state"] in {"succeeded", "failed", "canceled"}:
                return response.json()
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish within {timeout}s")

    # --- authentication --------------------------------------------------

    def test_protected_get_routes_refuse_a_missing_bearer_assertion(self):
        for path in ("/api/v1/profiles", "/api/v1/settings", "/api/v1/jobs"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "authentication_required")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertRegex(response.headers["x-correlation-id"], r"^[0-9a-f]{32}$")

    def test_protected_post_route_refuses_a_missing_bearer_assertion(self):
        # A structurally valid body, so the only possible reason for failure
        # is the missing assertion, not request validation.
        response = self.client.post(
            "/api/v1/jobs",
            json={
                "upload_id": "0" * 32,
                "machine_profile": MACHINE_ID,
                "process_profile": PROCESS_ID,
                "filament_profile": FILAMENT_ID,
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    def test_protected_routes_refuse_a_malformed_or_wrong_scheme_assertion(self):
        for header in ("Bearer not-a-jwt", f"Basic {self.token_a}", "Bearer"):
            with self.subTest(header=header):
                response = self.client.get("/api/v1/jobs", headers={"Authorization": header})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "authentication_required")

    def test_health_endpoints_stay_public_and_readiness_reports_the_auth_mode(self):
        for path in ("/api/v1/health", "/api/v1/health/live", "/api/v1/health/ready"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (200, 503))
        self.assertEqual(self.client.get("/api/v1/health/ready").json()["auth_mode"], "required")

    def test_docs_are_absent_in_required_mode(self):
        for path in ("/openapi.json", "/docs", "/redoc"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

        from fastapi.testclient import TestClient

        frontend = self.root / "frontend-dist"
        frontend.mkdir()
        (frontend / "index.html").write_text("browser shell", encoding="utf-8")
        mounted = dataclasses.replace(
            self.config(state_root=self.root / "mounted-state"), frontend_dist=frontend
        )
        with TestClient(self.create_app(mounted)) as client:
            self.assertEqual(client.get("/").status_code, 200)
            for path in ("/openapi.json", "/docs", "/redoc"):
                with self.subTest(mounted_path=path):
                    self.assertEqual(client.get(path).status_code, 404)

    # --- two-user isolation -----------------------------------------------

    def test_owner_can_only_list_and_read_their_own_jobs(self):
        upload_a = self.upload(self.headers_a).json()["upload_id"]
        job_a = self.submit(self.headers_a, upload_a).json()["job_id"]
        self.wait(job_a, self.headers_a)

        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers_b).json()["jobs"], [])
        listed_a = [job["job_id"] for job in self.client.get("/api/v1/jobs", headers=self.headers_a).json()["jobs"]]
        self.assertEqual(listed_a, [job_a])

        cross = self.client.get(f"/api/v1/jobs/{job_a}", headers=self.headers_b)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["error"]["code"], "unknown_job")
        self.assertEqual(self.client.get(f"/api/v1/jobs/{job_a}", headers=self.headers_a).status_code, 200)

    def test_owner_cannot_use_another_owners_upload(self):
        upload_a = self.upload(self.headers_a).json()["upload_id"]
        cross = self.submit(self.headers_b, upload_a)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["error"]["code"], "unknown_upload")

    def test_owner_cannot_cancel_or_retry_another_owners_job(self):
        upload_a = self.upload(self.headers_a).json()["upload_id"]
        job_a = self.submit(self.headers_a, upload_a).json()["job_id"]
        self.wait(job_a, self.headers_a)

        self.assertEqual(self.client.post(f"/api/v1/jobs/{job_a}/cancel", headers=self.headers_b).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/jobs/{job_a}/retry", headers=self.headers_b).status_code, 404)
        # The owner can still retry their own job: cross-owner failures above
        # left it untouched.
        self.assertEqual(self.client.post(f"/api/v1/jobs/{job_a}/retry", headers=self.headers_a).status_code, 202)

    def test_owner_cannot_download_artifacts_preview_or_scene_of_another_owners_job(self):
        upload_a = self.upload(self.headers_a).json()["upload_id"]
        job_a = self.submit(self.headers_a, upload_a).json()["job_id"]
        self.wait(job_a, self.headers_a)

        for path in (
            f"/api/v1/jobs/{job_a}/artifacts/gcode",
            f"/api/v1/jobs/{job_a}/preview",
            f"/api/v1/jobs/{job_a}/preview/layers/0",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers_b).status_code, 404)
        # And the owner can, proving the 404s above are ownership, not breakage.
        self.assertEqual(
            self.client.get(f"/api/v1/jobs/{job_a}/artifacts/gcode", headers=self.headers_a).status_code, 200
        )

        scene_upload = self.upload(self.headers_a, name="scene.stl").json()["upload_id"]
        scene_job = self.submit_scene(self.headers_a, scene_upload).json()["job_id"]
        self.wait(scene_job, self.headers_a)
        for path in (f"/api/v1/scenes/{scene_job}", f"/api/v1/scenes/{scene_job}/objects/0"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers_b).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/scenes/{scene_job}", headers=self.headers_a).status_code, 200)

    def test_scene_deduplication_never_crosses_owners(self):
        # Same upload id is impossible across owners (uploads are themselves
        # owned), so this proves the dedup key by using each owner's own
        # upload of identical bytes and the same profile chain: two distinct
        # jobs, not one shared one.
        contents = b"solid dup\nendsolid dup\n"
        upload_a = self.upload(self.headers_a, name="dup.stl", contents=contents).json()["upload_id"]
        upload_b = self.upload(self.headers_b, name="dup.stl", contents=contents).json()["upload_id"]
        job_a = self.submit_scene(self.headers_a, upload_a).json()["job_id"]
        job_b = self.submit_scene(self.headers_b, upload_b).json()["job_id"]
        self.assertNotEqual(job_a, job_b)

    def test_job_description_never_carries_owner_id(self):
        upload_a = self.upload(self.headers_a).json()["upload_id"]
        accepted = self.submit(self.headers_a, upload_a).json()
        self.assertNotIn("owner_id", accepted)
        job = self.wait(accepted["job_id"], self.headers_a)
        self.assertNotIn("owner_id", job)

    def test_upload_description_never_carries_owner_id(self):
        upload = self.upload(self.headers_a).json()
        self.assertNotIn("owner_id", upload)
        metadata = json.loads(
            (self.root / "state" / "uploads" / upload["upload_id"] / "upload.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata["metadata_version"], 2)
        self.assertRegex(metadata["owner_id"], r"^[0-9a-f]{64}$")

    def test_unknown_upload_metadata_version_fails_closed(self):
        upload = self.upload(self.headers_a).json()
        metadata_path = self.root / "state" / "uploads" / upload["upload_id"] / "upload.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["metadata_version"] = 999
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        response = self.submit(self.headers_a, upload["upload_id"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_upload")

    # --- legacy upload transition ------------------------------------------

    def test_required_mode_treats_a_pre_auth_upload_as_unknown(self):
        legacy_id = self._write_legacy_upload()
        response = self.submit(self.headers_a, legacy_id)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_upload")

    def test_disabled_mode_maps_a_pre_auth_upload_to_the_local_principal(self):
        from fastapi.testclient import TestClient

        state_root = self.root / "state"
        legacy_id = self._write_legacy_upload(state_root)
        disabled_config = self.config(auth_mode="disabled", state_root=state_root)
        with TestClient(self.create_app(disabled_config)) as client:
            accepted = client.post(
                "/api/v1/jobs",
                json={
                    "upload_id": legacy_id,
                    "machine_profile": MACHINE_ID,
                    "process_profile": PROCESS_ID,
                    "filament_profile": FILAMENT_ID,
                },
            )
            self.assertEqual(accepted.status_code, 202)

    def _write_legacy_upload(self, state_root=None) -> str:
        """Write an upload directory the way a pre-auth deployment would: no `owner_id`."""
        root = (state_root or (self.root / "state")) / "uploads"
        upload_id = uuid.uuid4().hex
        directory = root / upload_id
        directory.mkdir(parents=True, exist_ok=True)
        contents = b"solid legacy\nendsolid legacy\n"
        (directory / "source.stl").write_bytes(contents)
        metadata = {
            "upload_id": upload_id,
            "filename": "legacy.stl",
            "format": "stl",
            "size_bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
            "created_at": time.time(),
        }
        (directory / "upload.json").write_text(json.dumps(metadata), encoding="utf-8")
        return upload_id


if __name__ == "__main__":
    unittest.main()
