#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import threading
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
from web_job_directory import MANIFEST_NAME, JobDirectoryLimits  # noqa: E402
from web_profile_catalog import load_catalog  # noqa: E402
from web_worker_executor import ExecutorLimits  # noqa: E402

from web.api.config import ApiConfig  # noqa: E402
from web.api.errors import ApiError  # noqa: E402
from web.api.jobs import TERMINAL_STATES, JobService, SceneRequest, SliceRequest  # noqa: E402
from web.api.quotas import QuotaLimits  # noqa: E402
from web.api.store import JobStore  # noqa: E402
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

# This suite exercises JobService directly, against one fixed caller; ownership
# enforcement across two distinct owners is covered end to end in
# test_ownership.py, so a single opaque constant stands in for a real
# `derive_owner_id()` result here.
OWNER_ID = "1" * 64


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

    def service(self, mode="success", max_concurrent_jobs=2, retention_seconds=3600, quotas=None, **overrides):
        """A service on this test's state root. Calling it again is a restart:
        the new service recovers whatever the earlier one persisted."""
        config = ApiConfig(
            repo_root=self.root,
            state_root=self.root / "state",
            worker_command=(sys.executable, str(self.worker), mode),
            profile_vendors=(VENDOR,),
            max_concurrent_jobs=max_concurrent_jobs,
            executor_limits=TEST_LIMITS,
            job_limits=JobDirectoryLimits(retention_seconds=retention_seconds),
            quotas=quotas or QuotaLimits(),
            # This suite exercises job execution, not identity.
            auth_mode="disabled",
            **overrides,
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
            return self.uploads.create(filename, reader, OWNER_ID)

    def request(self, upload_id, **overrides):
        fields = {
            "upload_id": upload_id,
            "machine_profile": MACHINE_ID,
            "process_profile": PROCESS_ID,
            "filament_profiles": (FILAMENT_ID,),
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
            job = service.get(job_id, OWNER_ID)
            if job["state"] in TERMINAL_STATES:
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not finish within {timeout}s: {service.get(job_id, OWNER_ID)['state']}")

    def submit(self, service, **overrides):
        return service.submit(self.request(self.upload().upload_id, **overrides), "correlation-1", OWNER_ID)

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

        path, media_type, filename = service.artifact(accepted["job_id"], "gcode", OWNER_ID)
        self.assertEqual(media_type, "text/x.gcode")
        self.assertEqual(filename, f"{accepted['job_id']}.gcode")
        # The curated override reached the worker through the manifest.
        self.assertIn("layer_height = 0.28", path.read_text(encoding="utf-8"))
        self.assertTrue(service.artifact(accepted["job_id"], "result", OWNER_ID)[0].is_file())

    def test_keeps_a_failed_result_but_publishes_no_gcode(self):
        service = self.service("fail")
        job = self.wait(service, self.submit(service)["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "slicing_failed")
        self.assertEqual([item["name"] for item in job["artifacts"]], ["result"])
        self.assertTrue(service.artifact(job["job_id"], "result", OWNER_ID)[0].is_file())
        with self.assertRaises(ApiError) as raised:
            service.artifact(job["job_id"], "gcode", OWNER_ID)
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
            job = service.get(accepted["job_id"], OWNER_ID)
            if job["progress"]["percent"] == 42:
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["progress"], {"stage": "slicing", "percent": 42, "message": "Slicing"})
        self.assertEqual([warning["code"] for warning in job["warnings"]], ["slow_start"])
        service.cancel(accepted["job_id"], OWNER_ID)
        self.assertEqual(self.wait(service, accepted["job_id"])["state"], "canceled")

    def test_cancels_a_running_job_and_removes_its_directory(self):
        service = self.service("slow")
        accepted = self.submit(service)
        deadline = time.monotonic() + 10
        while service.get(accepted["job_id"], OWNER_ID)["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(service.get(accepted["job_id"], OWNER_ID)["state"], "running")

        service.cancel(accepted["job_id"], OWNER_ID)
        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "canceled")
        self.assertEqual(job["error"]["category"], "cancellation")
        self.assertFalse((self.config.jobs_root / job["job_id"]).exists())
        with self.assertRaises(ApiError):
            service.artifact(job["job_id"], "result", OWNER_ID)

    def test_cancels_a_queued_job_before_a_worker_starts(self):
        service = self.service("slow", max_concurrent_jobs=1)
        running = self.submit(service)
        queued = self.submit(service)
        self.assertEqual(service.get(queued["job_id"], OWNER_ID)["state"], "queued")

        canceled = service.cancel(queued["job_id"], OWNER_ID)
        self.assertEqual(canceled["state"], "canceled")
        self.assertIsNone(canceled["started_at"])
        self.assertFalse((self.config.jobs_root / queued["job_id"]).exists())
        service.cancel(running["job_id"], OWNER_ID)
        self.assertEqual(self.wait(service, running["job_id"])["state"], "canceled")

    def test_shutdown_closes_admission_and_never_launches_queued_jobs(self):
        service = self.service("slow", max_concurrent_jobs=1)
        running = self.submit(service)
        deadline = time.monotonic() + 10
        while service.get(running["job_id"], OWNER_ID)["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(service.get(running["job_id"], OWNER_ID)["state"], "running")

        queued = self.submit(service)
        self.assertEqual(service.get(queued["job_id"], OWNER_ID)["state"], "queued")
        service.shutdown()

        running_record = service.get(running["job_id"], OWNER_ID)
        queued_record = service.get(queued["job_id"], OWNER_ID)
        self.assertEqual(running_record["state"], "canceled")
        self.assertEqual(queued_record["state"], "canceled")
        self.assertIsNone(queued_record["started_at"])
        self.assertFalse((self.config.jobs_root / running["job_id"]).exists())
        self.assertFalse((self.config.jobs_root / queued["job_id"]).exists())

        with self.assertRaises(ApiError) as raised:
            self.submit(service)
        self.assertEqual(raised.exception.code, "service_draining")
        self.assertEqual(raised.exception.status, 503)
        # A second lifecycle callback is harmless.
        service.shutdown()

    def test_rechecks_admission_after_a_job_directory_was_staged(self):
        service = self.service()
        upload = self.upload()
        path = self.config.jobs_root / "job-late-admission"
        path.mkdir(parents=True)
        service.begin_shutdown()

        with self.assertRaises(ApiError) as raised:
            service._accept(
                "job-late-admission",
                "slice",
                self.request(upload.upload_id),
                path,
                0,
                "correlation-late",
                OWNER_ID,
                None,
                {},
                [],
            )
        self.assertEqual(raised.exception.code, "service_draining")
        self.assertFalse(path.exists())
        self.assertEqual(service.list(OWNER_ID), [])

    def test_shutdown_waits_for_a_previously_admitted_mutation(self):
        service = self.service()
        finished = threading.Event()

        with service.admission():
            shutdown = threading.Thread(target=lambda: (service.shutdown(), finished.set()))
            shutdown.start()
            deadline = time.monotonic() + 5
            while service.is_accepting() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(service.is_accepting())
            self.assertFalse(finished.wait(0.1))

        shutdown.join(timeout=5)
        self.assertFalse(shutdown.is_alive())
        self.assertTrue(finished.is_set())

    def test_refuses_to_cancel_a_finished_job(self):
        service = self.service()
        job = self.wait(service, self.submit(service)["job_id"])
        with self.assertRaises(ApiError) as raised:
            service.cancel(job["job_id"], OWNER_ID)
        self.assertEqual(raised.exception.code, "job_not_cancelable")

    def test_retries_a_finished_job_with_the_same_inputs(self):
        service = self.service("fail")
        first = self.wait(service, self.submit(service, settings={"layer_height": "0.3"})["job_id"])
        retried = service.retry(first["job_id"], "correlation-2", OWNER_ID)
        self.assertNotEqual(retried["job_id"], first["job_id"])
        self.assertEqual(retried["retry_of"], first["job_id"])
        self.assertEqual(retried["correlation_id"], "correlation-2")
        self.assertEqual(retried["request"], first["request"])
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "failed")

    def test_refuses_to_retry_a_job_that_is_still_running(self):
        service = self.service("slow")
        accepted = self.submit(service)
        with self.assertRaises(ApiError) as raised:
            service.retry(accepted["job_id"], "correlation-3", OWNER_ID)
        self.assertEqual(raised.exception.code, "job_not_retryable")
        service.cancel(accepted["job_id"], OWNER_ID)

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
            (
                {"objects": [{"source_object": 0, "transform": valid_transform, "filament": -1}]},
                "invalid_object_filament",
                400,
            ),
            (
                # Only one filament is named, so slot 2 does not exist.
                {"objects": [{"source_object": 0, "transform": valid_transform, "filament": 2}]},
                "invalid_object_filament",
                400,
            ),
            ({"filament_profiles": ()}, "invalid_filament_profiles", 400),
            ({"filament_profiles": tuple([FILAMENT_ID] * 17)}, "invalid_filament_profiles", 400),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ApiError) as raised:
                    fields = {"upload_id": upload.upload_id, **overrides}
                    service.submit(self.request(**fields), "correlation-4", OWNER_ID)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status, status)
        self.assertEqual(service.list(OWNER_ID), [])

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
        path, _, _ = service.artifact(job["job_id"], "gcode", OWNER_ID)
        # The manifest carried the placement through to the worker intact.
        self.assertIn(json.dumps(objects, separators=(",", ":")), path.read_text(encoding="utf-8"))

        retried = service.retry(job["job_id"], "correlation-objects-retry", OWNER_ID)
        self.assertEqual(retried["request"]["objects"], objects)
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "succeeded")

    def test_slices_with_several_filament_profiles_assigned_per_object(self):
        service = self.service()
        upload = self.upload()
        transform = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        objects = [
            {"source_object": 0, "transform": transform, "filament": 1},
            {"source_object": 0, "transform": transform, "filament": 2},
        ]
        # Two spools of the same bundled material: a repeated filament id is
        # not an error, and each slot still gets its own profile file.
        request = self.request(
            upload.upload_id, filament_profiles=(FILAMENT_ID, FILAMENT_ID), objects=objects
        )
        accepted = service.submit(request, "correlation-multi-filament", OWNER_ID)
        self.assertEqual(accepted["request"]["filament_profiles"], [FILAMENT_ID, FILAMENT_ID])
        # The job report names every filament in the chain, not only the first.
        self.assertEqual(
            [entry["profile_id"] for entry in accepted["profiles"]["filaments"]], [FILAMENT_ID, FILAMENT_ID]
        )
        self.assertNotIn("filament", accepted["profiles"])

        job_dir = self.config.jobs_root / accepted["job_id"]
        manifest = json.loads((job_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest_profiles = manifest["operation"]["payload"]["profiles"]
        self.assertEqual(
            manifest_profiles["filaments"], ["profiles/filament-0.json", "profiles/filament-1.json"]
        )
        self.assertTrue((job_dir / "profiles" / "filament-0.json").is_file())
        self.assertTrue((job_dir / "profiles" / "filament-1.json").is_file())

        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["profiles"]["filaments"][1]["profile_id"], FILAMENT_ID)

    def test_refuses_an_out_of_range_object_filament_through_the_retry_path(self):
        # `retry()` calls this same `submit()` with `retry_of` set and never
        # touches the request schema, so exercise that path directly: even a
        # request that looks like a retry must still be revalidated.
        service = self.service()
        upload = self.upload()
        transform = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        bad = self.request(
            upload.upload_id,
            objects=[{"source_object": 0, "transform": transform, "filament": 2}],
        )
        with self.assertRaises(ApiError) as raised:
            service.submit(bad, "correlation-retry-like", OWNER_ID, retry_of="job-does-not-exist")
        self.assertEqual(raised.exception.code, "invalid_object_filament")
        self.assertEqual(service.list(OWNER_ID), [])

    def test_produces_a_scene_for_an_upload_and_serves_its_objects(self):
        service = self.service()
        upload = self.upload()
        accepted = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-1", OWNER_ID)
        self.assertEqual(accepted["state"], "queued")

        job = self.wait(service, accepted["job_id"])
        self.assertEqual(job["state"], "succeeded")

        index = service.scene(job["job_id"], OWNER_ID)
        self.assertEqual(index["scene_version"], 1)
        self.assertEqual(len(index["objects"]), 2)

        for described in index["objects"]:
            with self.subTest(index=described["index"]):
                path, offset, length = service.scene_object(job["job_id"], described["index"], OWNER_ID)
                self.assertEqual(length, described["length"])
                # Never the whole blob: each object is a strict slice of it.
                self.assertLess(length, index["data_bytes"])
                with open(path, "rb") as stream:
                    stream.seek(offset)
                    self.assertEqual(len(stream.read(length)), length)

        with self.assertRaises(ApiError) as raised:
            service.scene_object(job["job_id"], 99, OWNER_ID)
        self.assertEqual(raised.exception.code, "unknown_scene_object")
        self.assertEqual(raised.exception.status, 404)

    def test_returns_the_existing_scene_job_for_a_repeated_request(self):
        service = self.service()
        upload = self.upload()
        first = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-1", OWNER_ID)
        second = service.submit_scene(self.scene_request(upload.upload_id), "correlation-scene-2", OWNER_ID)
        self.assertEqual(second["job_id"], first["job_id"])
        # The correlation id recorded is the one that actually created the job.
        self.assertEqual(second["correlation_id"], "correlation-scene-1")
        self.assertEqual(self.wait(service, first["job_id"])["state"], "succeeded")

    def test_retries_a_failed_scene_job_as_a_fresh_one(self):
        service = self.service("fail")
        first = self.wait(
            service,
            service.submit_scene(self.scene_request(self.upload().upload_id), "c-1", OWNER_ID)["job_id"],
        )
        self.assertEqual(first["state"], "failed")
        retried = service.retry(first["job_id"], "correlation-scene-retry", OWNER_ID)
        self.assertNotEqual(retried["job_id"], first["job_id"])
        self.assertEqual(retried["retry_of"], first["job_id"])
        self.assertEqual(self.wait(service, retried["job_id"])["state"], "failed")

    # Retention and the deletion audit

    def expire(self, service, job_id):
        """Move one finished job's completion back past the retention window."""
        with service._lock:
            service._jobs[job_id].finished_at -= self.config.job_limits.retention_seconds + 1

    def backdate_uploads(self):
        """Move every upload's recorded creation back past the retention window."""
        for directory in self.config.uploads_root.iterdir():
            metadata_path = directory / "upload.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["created_at"] -= self.config.job_limits.retention_seconds + 1
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def audit(self, **filters):
        store = JobStore(self.config.metadata_path)
        try:
            return store.deletions(**filters)
        finally:
            store.close()

    def test_expires_a_job_by_when_it_finished_and_audits_the_deletion(self):
        service = self.service()
        job = self.wait(service, self.submit(service)["job_id"])
        directory = self.config.jobs_root / job["job_id"]
        self.assertAlmostEqual(job["expires_at"], job["finished_at"] + 3600)
        size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())

        # An old directory does not expire a job that finished recently.
        stale = time.time() - 7200
        os.utime(directory, (stale, stale))
        self.assertEqual(service.sweep()["jobs_removed"], [])
        self.assertEqual(service.get(job["job_id"], OWNER_ID)["state"], "succeeded")

        self.expire(service, job["job_id"])
        self.backdate_uploads()
        report = service.sweep()
        self.assertEqual(report["jobs_removed"], [job["job_id"]])
        self.assertEqual(len(report["uploads_removed"]), 1)
        self.assertFalse(directory.exists())
        with self.assertRaises(ApiError) as raised:
            service.get(job["job_id"], OWNER_ID)
        self.assertEqual(raised.exception.code, "unknown_job")

        rows = self.audit(subject_id=job["job_id"])
        self.assertEqual([(row["kind"], row["reason"], row["outcome"]) for row in rows], [
            ("job_directory", "retention_expired", "removed"),
            ("job_record", "retention_expired", "removed"),
        ])
        self.assertEqual(rows[0]["bytes"], size)
        self.assertEqual({row["owner_id"] for row in rows}, {OWNER_ID})
        self.assertEqual(rows[0]["created_at"], job["created_at"])
        upload_rows = self.audit(subject_id=job["request"]["upload_id"])
        self.assertEqual([(row["kind"], row["owner_id"]) for row in upload_rows], [("upload", OWNER_ID)])
        self.assertGreater(upload_rows[0]["bytes"], 0)
        # Nothing but identifiers, sizes, and times is recorded.
        self.assertNotIn("cube", json.dumps(self.audit()))

    def test_audits_the_directory_a_failed_or_canceled_job_loses(self):
        service = self.service("crash")
        crashed = self.wait(service, self.submit(service)["job_id"])
        self.assertIsNotNone(crashed["expires_at"])
        [row] = self.audit(subject_id=crashed["job_id"])
        self.assertEqual((row["kind"], row["reason"], row["outcome"]), ("job_directory", "job_failed", "removed"))
        self.assertGreater(row["bytes"], 0)

        slow = self.service("slow", max_concurrent_jobs=1)
        running = self.submit(slow)
        queued = self.submit(slow)
        slow.cancel(queued["job_id"], OWNER_ID)
        slow.cancel(running["job_id"], OWNER_ID)
        self.wait(slow, running["job_id"])
        for job_id in (queued["job_id"], running["job_id"]):
            self.assertEqual([row["reason"] for row in self.audit(subject_id=job_id)], ["job_canceled"])

    def test_audits_an_orphaned_directory_no_record_claims(self):
        service = self.service()
        orphan = self.config.jobs_root / "job-orphan"
        orphan.mkdir(parents=True)
        (orphan / "request.json").write_text("{}", encoding="utf-8")
        stale = time.time() - 7200
        os.utime(orphan, (stale, stale))

        self.assertEqual(service.sweep()["jobs_removed"], ["job-orphan"])
        [row] = self.audit(subject_id="job-orphan")
        self.assertEqual((row["reason"], row["owner_id"], row["bytes"]), ("orphaned", None, 2))

    def test_enforces_retention_without_any_request(self):
        service = self.service(retention_sweep_seconds=1)
        job = self.wait(service, self.submit(service)["job_id"])
        self.expire(service, job["job_id"])
        service.start_sweeper()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with service._lock:
                if job["job_id"] not in service._jobs:
                    break
            time.sleep(0.05)
        self.assertFalse((self.config.jobs_root / job["job_id"]).exists())
        self.assertEqual(len(self.audit(subject_id=job["job_id"])), 2)

    def test_refuses_retention_settings_that_cannot_hold(self):
        for overrides in ({"audit_retention_seconds": 3599}, {"retention_sweep_seconds": 0}):
            with self.assertRaises(ApiError) as raised:
                self.service(**overrides)
            self.assertEqual(raised.exception.code, "invalid_api_configuration")

    def test_prunes_audit_rows_older_than_the_audit_retention(self):
        service = self.service(audit_retention_seconds=7200)
        old = time.time() - 7201
        with service._lock:
            service._store.record_deletion(
                {"deleted_at": old, "kind": "upload", "subject_id": "0" * 32, "owner_id": None,
                 "reason": "retention_expired", "outcome": "removed", "bytes": 1, "created_at": None}
            )
        service.sweep()
        self.assertEqual(self.audit(subject_id="0" * 32), [])

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
        service.cancel(accepted["job_id"], OWNER_ID)

    # Restart and recovery

    def test_a_restart_keeps_finished_jobs_and_their_artifacts(self):
        first = self.service()
        sliced = self.wait(first, self.submit(first)["job_id"])
        scene = self.wait(
            first, first.submit_scene(self.scene_request(self.upload().upload_id), "c-scene", OWNER_ID)["job_id"]
        )
        first.shutdown()

        restarted = self.service()
        self.assertEqual(restarted.get(sliced["job_id"], OWNER_ID), sliced)
        self.assertEqual([job["job_id"] for job in restarted.list(OWNER_ID)], [sliced["job_id"], scene["job_id"]])
        path, _, _ = restarted.artifact(sliced["job_id"], "gcode", OWNER_ID)
        self.assertTrue(path.is_file())
        self.assertEqual(len(restarted.preview(sliced["job_id"], OWNER_ID)["layers"]), 2)
        # Scene deduplication survives too: the same request returns the
        # recovered job rather than starting another.
        again = restarted.submit_scene(
            self.scene_request(scene["request"]["upload_id"]), "c-scene-again", OWNER_ID
        )
        self.assertEqual(again["job_id"], scene["job_id"])
        # A recovered job is retried exactly like a live one.
        retried = restarted.retry(sliced["job_id"], "c-retry", OWNER_ID)
        self.assertEqual(retried["retry_of"], sliced["job_id"])
        self.assertEqual(self.wait(restarted, retried["job_id"])["state"], "succeeded")

    def test_a_restart_fails_jobs_an_earlier_process_left_unfinished(self):
        first = self.service("slow")
        accepted = self.submit(first)
        deadline = time.monotonic() + 10
        while first.get(accepted["job_id"], OWNER_ID)["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        # What a crash leaves behind: the stored record still says running,
        # and the directory holds whatever the worker had written.
        stranded = first._jobs[accepted["job_id"]].to_stored()
        self.assertEqual(stranded["state"], "running")
        first.shutdown()
        store = JobStore(self.config.metadata_path)
        store.save(stranded)
        store.close()
        directory = self.config.jobs_root / accepted["job_id"]
        (directory / "output").mkdir(parents=True, exist_ok=True)
        (directory / "output" / "model.gcode").write_text("; partial", encoding="utf-8")

        restarted = self.service()
        job = restarted.get(accepted["job_id"], OWNER_ID)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["category"], "internal")
        self.assertEqual(job["error"]["code"], "job_interrupted")
        self.assertEqual(job["artifacts"], [])
        self.assertFalse(directory.exists())
        retried = restarted.retry(accepted["job_id"], "c-retry", OWNER_ID)
        self.assertEqual(self.wait(restarted, retried["job_id"])["state"], "succeeded")
        # The interruption itself was persisted, so a second restart agrees.
        restarted.shutdown()
        self.assertEqual(self.service().get(accepted["job_id"], OWNER_ID)["error"]["code"], "job_interrupted")

    def test_a_restart_replays_the_quota_windows(self):
        limits = QuotaLimits(max_submissions=1)
        first = self.service(quotas=limits)
        self.wait(first, self.submit(first)["job_id"])
        charged = first.quota(OWNER_ID)["usage"]["cpu_time_ms"]
        self.assertGreater(charged, 0)
        first.shutdown()

        restarted = self.service(quotas=limits)
        usage = restarted.quota(OWNER_ID)["usage"]
        self.assertEqual(usage["submissions"], 1)
        self.assertEqual(usage["cpu_time_ms"], charged)
        with self.assertRaises(ApiError) as raised:
            self.submit(restarted)
        self.assertEqual(raised.exception.code, "job_submission_quota_exceeded")

    def test_forgets_a_finished_record_after_the_retention_window(self):
        service = self.service("crash", retention_seconds=1)
        job = self.wait(service, self.submit(service)["job_id"])
        # A crashed job's directory is already gone; only its record remains.
        self.assertFalse((self.config.jobs_root / job["job_id"]).exists())
        self.expire(service, job["job_id"])
        service.sweep()
        with self.assertRaises(ApiError) as raised:
            service.get(job["job_id"], OWNER_ID)
        self.assertEqual(raised.exception.code, "unknown_job")
        service.shutdown()
        self.assertEqual(self.service().list(OWNER_ID), [])

    def test_skips_a_stored_record_it_cannot_trust(self):
        first = self.service()
        job = self.wait(first, self.submit(first)["job_id"])
        stored = first._jobs[job["job_id"]].to_stored()
        first.shutdown()
        store = JobStore(self.config.metadata_path)
        store.save(dict(stored, job_id="job-escape", owner_id="not-an-owner"))
        store.save(dict(stored, job_id="../outside"))
        store.save(dict(stored, job_id="job-future", record_version=99))
        store.save(dict(stored, job_id="job-bad-request", request={"unexpected": 1}))
        store.close()

        restarted = self.service()
        self.assertEqual([item["job_id"] for item in restarted.list(OWNER_ID)], [job["job_id"]])

    def test_refuses_a_job_the_store_cannot_record(self):
        service = self.service()
        upload = self.upload()
        service._store.close()
        with self.assertRaises(ApiError) as raised:
            service.submit(self.request(upload.upload_id), "c-1", OWNER_ID)
        self.assertEqual(raised.exception.code, "metadata_store_unavailable")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(service.list(OWNER_ID), [])
        self.assertEqual(list(self.config.jobs_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
