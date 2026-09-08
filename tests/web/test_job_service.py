#!/usr/bin/env python3

import json
import os
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
    OTHER_PROCESS_ID,
    PROCESS_ID,
    VENDOR,
    write_profile_tree,
)
from web_job_directory import JobDirectoryLimits  # noqa: E402
from web_profile_catalog import load_catalog  # noqa: E402
from web_worker_executor import ExecutorLimits  # noqa: E402

from web.api.config import ApiConfig  # noqa: E402
from web.api.errors import ApiError  # noqa: E402
from web.api.jobs import TERMINAL_STATES, JobService, SceneRequest, SliceRequest  # noqa: E402
from web.api.uploads import UploadStore  # noqa: E402


TEST_LIMITS = ExecutorLimits(
    wall_time_ms=20_000,
    cpu_time_ms=20_000,
    memory_bytes=512 * 1024 * 1024,
    output_bytes=8 * 1024 * 1024,
    process_count=32,
    open_files=64,
    termination_grace_ms=200,
    event_bytes=64 * 1024,
    event_line_bytes=8 * 1024,
    event_count=128,
    log_bytes=32 * 1024,
)


@unittest.skipUnless(sys.platform == "linux", "the executor requires Linux")
class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_profile_tree(self.root)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
        self.temporary.cleanup()

    def service(self, mode="success", max_concurrent_jobs=2, retention_seconds=3600):
        config = ApiConfig(
            repo_root=self.root,
            state_root=self.root / "state",
            worker_command=(sys.executable, str(self.worker), mode),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=max_concurrent_jobs,
            executor_limits=TEST_LIMITS,
            job_limits=JobDirectoryLimits(retention_seconds=retention_seconds),
        )
        config.validate()
        self.config = config
        uploads = UploadStore(config.uploads_root, config.job_limits.max_input_bytes)
        built = JobService(config, load_catalog(self.root, [VENDOR]), uploads)
        self.services.append(built)
        self.uploads = uploads
        return built

    def upload(self, contents=b"solid cube\nendsolid cube\n", filename="cube.stl"):
        source = self.root / filename
        source.write_bytes(contents)
        with source.open("rb") as reader:
            return self.uploads.create(filename, reader)

    def request(self, upload_id, **overrides):
        fields = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        fields.update(overrides)
        return SliceRequest(**fields)

    def scene_request(self, upload_id, **overrides):
        fields = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profile": FILAMENT_ID,
        }
        fields.update(overrides)
        return SceneRequest(**fields)

    def wait(self, service, job_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = service.get(job_id)
            if job["state"] in TERMINAL_STATES:
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish within {timeout}s: {service.get(job_id)['state']}")

    def submit(self, service, **overrides):
        return service.submit(self.request(self.upload().upload_id, **overrides), "correlation-1")

    def test_slices_an_upload_into_a_downloadable_artifact(self):
        service = self.service()
        accepted = self.submit(service, settings={"layer_height": "0.28"})
        self.assertEqual(accepted["state"], "queued")
        self.assertEqual(accepted["correlation_id"], "correlation-1")

        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["progress"], {"stage": "finalize", "percent": 100, "message": ""})
        self.assertEqual([warning["code"] for warning in job["warnings"]], ["thin_wall"])
        # The preview's binary companion is not downloadable by name; it is
        # reached one layer at a time instead.
        self.assertEqual(
            sorted(item["name"] for item in job["artifacts"]), ["gcode", "preview", "result"]
        )
        self.assertEqual(job["timing"], {"duration_ms": 3, "cpu_time_ms": 2})

        path, media_type, filename = service.artifact(accepted["job_id"], "gcode")
        self.assertEqual(media_type, "text/x.gcode")
        self.assertEqual(filename, f"{accepted['job_id']}.gcode")
        # The curated override reached the worker through the manifest.
        self.assertIn("layer_height = 0.28", path.read_text(encoding="utf-8"))
        self.assertTrue(service.artifact(accepted["job_id"], "result")[0].is_file())

    def test_keeps_a_failed_result_but_publishes_no_gcode(self):
        service = self.service("fail")
        job = self.wait(service, self.submit(service)["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "slicing_failed")
        self.assertEqual([item["name"] for item in job["artifacts"]], ["result"])
        self.assertTrue(service.artifact(job["job_id"], "result")[0].is_file())
        with self.assertRaises(ApiError) as raised:
            service.artifact(job["job_id"], "gcode")
        self.assertEqual(raised.exception.status, 409)

    def test_isolates_a_worker_crash(self):
        service = self.service("crash")
        job = self.wait(service, self.submit(service)["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "missing_terminal_event")
        self.assertEqual(job["artifacts"], [])
        # A crash leaves nothing the executor validated, so the job is reclaimed.
        self.assertFalse((self.config.jobs_root / job["job_id"]).exists())
        # The service keeps serving after the failure.
        self.assertEqual(self.wait(service, self.submit(service)["job_id"])["state"], "failed")

    def test_reports_worker_progress_and_warnings_while_the_job_runs(self):
        service = self.service("progress")
        accepted = self.submit(service)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = service.get(accepted["job_id"])
            if job["progress"]["percent"] == 42:
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["progress"], {"stage": "slicing", "percent": 42, "message": "Slicing"})
        self.assertEqual([warning["code"] for warning in job["warnings"]], ["slow_start"])
        service.cancel(accepted["job_id"])
        self.assertEqual(self.wait(service, accepted["job_id"])["state"], "canceled")

    def test_cancels_a_running_job_and_removes_its_directory(self):
        service = self.service("slow")
        accepted = self.submit(service)
        deadline = time.monotonic() + 10
        while service.get(accepted["job_id"])["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(service.get(accepted["job_id"])["state"], "running")

        service.cancel(accepted["job_id"])
        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "canceled")
        self.assertEqual(job["error"]["category"], "cancellation")
        self.assertFalse((self.config.jobs_root / job["job_id"]).exists())
        with self.assertRaises(ApiError):
            service.artifact(job["job_id"], "result")

    def test_cancels_a_queued_job_before_a_worker_starts(self):
        service = self.service("slow", max_concurrent_jobs=1)
        running = self.submit(service)
        queued = self.submit(service)
        self.assertEqual(service.get(queued["job_id"])["state"], "queued")

        canceled = service.cancel(queued["job_id"])
        self.assertEqual(canceled["state"], "canceled")
        self.assertIsNone(canceled["started_at"])
        service.cancel(running["job_id"])
        self.assertEqual(self.wait(service, running["job_id"])["state"], "canceled")

    def test_refuses_to_cancel_a_finished_job(self):
        service = self.service()
        job = self.wait(service, self.submit(service)["job_id"])
        with self.assertRaises(ApiError) as raised:
            service.cancel(job["job_id"])
        self.assertEqual(raised.exception.code, "job_not_cancelable")

    def test_retries_a_finished_job_with_the_same_inputs(self):
        service = self.service("fail")
        first = self.wait(service, self.submit(service, settings={"layer_height": "0.3"})["job_id"])
        retried = service.retry(first["job_id"], "correlation-2")
        self.assertNotEqual(retried["job_id"], first["job_id"])
        self.assertEqual(retried["retry_of"], first["job_id"])
        self.assertEqual(retried["correlation_id"], "correlation-2")
        self.assertEqual(retried["request"], first["request"])
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "failed")

    def test_refuses_to_retry_a_job_that_is_still_running(self):
        service = self.service("slow")
        accepted = self.submit(service)
        with self.assertRaises(ApiError) as raised:
            service.retry(accepted["job_id"], "correlation-3")
        self.assertEqual(raised.exception.code, "job_not_retryable")
        service.cancel(accepted["job_id"])

    def test_rejects_requests_that_cannot_produce_a_job(self):
        service = self.service()
        upload = self.upload()
        valid_transform = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        for overrides, code, status in (
            ({"upload_id": "0" * 32}, "unknown_upload", 404),
            ({"process_profile": f"{VENDOR}/process/Nonexistent"}, "unknown_profile", 404),
            ({"process_profile": OTHER_PROCESS_ID}, "incompatible_profile", 409),
            ({"machine_profile": PROCESS_ID}, "profile_kind_mismatch", 400),
            ({"settings": {"layer height": "0.2"}}, "invalid_settings", 400),
            ({"settings": {"layer_height": "x" * 5000}}, "invalid_settings", 400),
            ({"plate_index": 0}, "invalid_plate_index", 400),
            (
                {"objects": [{"source_object": 0, "transform": valid_transform}] * 65},
                "invalid_objects",
                400,
            ),
            (
                {"objects": [{"source_object": -1, "transform": valid_transform}]},
                "invalid_objects",
                400,
            ),
            (
                {"objects": [{"source_object": 0, "transform": valid_transform[:-1]}]},
                "invalid_objects",
                400,
            ),
            (
                {"objects": [{"source_object": 0, "transform": [float("inf")] + valid_transform[1:]}]},
                "invalid_objects",
                400,
            ),
            (
                {"objects": [{"source_object": 0, "transform": [float("nan")] + valid_transform[1:]}]},
                "invalid_objects",
                400,
            ),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ApiError) as raised:
                    fields = {"upload_id": upload.upload_id, **overrides}
                    service.submit(self.request(**fields), "correlation-4")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status, status)
        self.assertEqual(service.list(), [])

    def test_places_explicit_object_transforms_and_preserves_them_on_retry(self):
        service = self.service()
        transform = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 5.0, 2.0, 0.0, 1.0]
        # A source_object may repeat: this places the same source twice, once
        # as a duplicate.
        objects = [{"source_object": 0, "transform": transform}, {"source_object": 0, "transform": transform}]
        accepted = self.submit(service, objects=objects)
        self.assertEqual(accepted["request"]["objects"], objects)

        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["request"]["objects"], objects)
        path, _, _ = service.artifact(job["job_id"], "gcode")
        # The manifest carried the placement through to the worker intact.
        self.assertIn(json.dumps(objects, separators=(",", ":")), path.read_text(encoding="utf-8"))

        retried = service.retry(job["job_id"], "correlation-objects-retry")
        self.assertEqual(retried["request"]["objects"], objects)
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "succeeded")

    def test_produces_a_scene_for_an_upload_and_serves_its_objects(self):
        service = self.service()
        upload = self.upload()
        accepted = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-1")
        self.assertEqual(accepted["state"], "queued")

        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "succeeded")

        index = service.scene(job["job_id"])
        self.assertEqual(index["scene_version"], 1)
        self.assertEqual(len(index["objects"]), 2)

        for described in index["objects"]:
            with self.subTest(index=described["index"]):
                path, offset, length = service.scene_object(job["job_id"], described["index"])
                self.assertEqual(length, described["length"])
                # Never the whole blob: each object is a strict slice of it.
                self.assertLess(length, index["data_bytes"])
                with open(path, "rb") as stream:
                    stream.seek(offset)
                    self.assertEqual(len(stream.read(length)), length)

        with self.assertRaises(ApiError) as raised:
            service.scene_object(job["job_id"], 99)
        self.assertEqual(raised.exception.code, "unknown_scene_object")
        self.assertEqual(raised.exception.status, 404)

    def test_returns_the_existing_scene_job_for_a_repeated_request(self):
        service = self.service()
        upload = self.upload()
        first = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-1")
        second = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-2")
        self.assertEqual(second["job_id"], first["job_id"])
        # The correlation id recorded is the one that actually created the job.
        self.assertEqual(second["correlation_id"], "correlation-scene-1")
        self.assertEqual(self.wait(service, first["job_id"])["state"], "succeeded")

    def test_retries_a_failed_scene_job_as_a_fresh_one(self):
        service = self.service("fail")
        first = self.wait(service, service.submit_scene(self.scene_request(self.upload().upload_id), "c-1")["job_id"])
        self.assertEqual(first["state"], "failed")
        retried = service.retry(first["job_id"], "correlation-scene-retry")
        self.assertNotEqual(retried["job_id"], first["job_id"])
        self.assertEqual(retried["retry_of"], first["job_id"])
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "failed")

    def test_reclaims_expired_jobs_and_uploads(self):
        service = self.service(retention_seconds=1)
        job = self.wait(service, self.submit(service)["job_id"])
        directory = self.config.jobs_root / job["job_id"]
        self.assertTrue(directory.is_dir())

        expired = time.time() - 3600
        os.utime(directory, (expired, expired))
        for upload in self.config.uploads_root.iterdir():
            os.utime(upload, (expired, expired))

        report = service.sweep()
        self.assertEqual(report["jobs_removed"], [job["job_id"]])
        self.assertEqual(len(report["uploads_removed"]), 1)
        self.assertFalse(directory.exists())
        with self.assertRaises(ApiError) as raised:
            service.get(job["job_id"])
        self.assertEqual(raised.exception.code, "unknown_job")

    def test_never_sweeps_a_job_that_is_still_running(self):
        service = self.service("slow", retention_seconds=1)
        accepted = self.submit(service)
        directory = self.config.jobs_root / accepted["job_id"]
        expired = time.time() - 3600
        os.utime(directory, (expired, expired))

        report = service.sweep()
        self.assertEqual(report["jobs_removed"], [])
        self.assertEqual(report["jobs_retained"], [accepted["job_id"]])
        self.assertTrue(directory.is_dir())
        service.cancel(accepted["job_id"])


if __name__ == "__main__":
    unittest.main()
