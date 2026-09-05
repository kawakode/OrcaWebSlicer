#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from web_job_directory import (  # noqa: E402
    JobDirectoryError,
    JobDirectoryLimits,
    JobInput,
    _resolve_staged_path,
    create_job_directory,
    prepare_job_directory,
    remove_job_directory,
    sweep_job_directories,
)


class JobDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "jobs"
        self.sources = Path(self.temporary.name) / "sources"
        self.sources.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name, contents=b"solid model\n"):
        path = self.sources / name
        path.write_bytes(contents)
        return path

    def manifest(self, job_id="job-1"):
        return {
            "protocol_version": 1,
            "job_id": job_id,
            "operation": {
                "name": "slice",
                "version": 1,
                "payload": {"input_model": "input/model.stl", "output_gcode": "output/model.gcode"},
            },
        }

    def prepare(self, job_id="job-1", inputs=None, limits=None):
        return prepare_job_directory(
            self.root,
            job_id,
            self.manifest(job_id),
            inputs if inputs is not None else [JobInput("input/model.stl", self.source("model.stl"))],
            limits,
        )

    def test_stages_only_the_declared_inputs_and_manifest(self):
        prepared = self.prepare(
            inputs=[
                JobInput("input/model.stl", self.source("model.stl")),
                JobInput("profiles/machine.json", self.source("machine.json", b"{}"), kind="profile"),
            ]
        )
        self.source("secret.stl", b"never staged")

        staged = sorted(str(path.relative_to(prepared.path)) for path in prepared.path.rglob("*") if path.is_file())
        self.assertEqual(staged, ["input/model.stl", "profiles/machine.json", "request.json"])
        self.assertEqual(prepared.staged_bytes, len(b"solid model\n") + len(b"{}"))
        self.assertEqual(json.loads(prepared.manifest_path.read_text())["job_id"], "job-1")

    def test_gives_every_job_a_private_directory(self):
        prepared = self.prepare()
        self.assertEqual(prepared.path.stat().st_mode & 0o777, 0o700)

    def test_refuses_to_reuse_a_job_directory(self):
        self.prepare()
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, "job_directory_exists")

    def test_rejects_a_job_id_that_names_a_path(self):
        for job_id in ["..", "../escape", "a/b", "", "x" * 129]:
            with self.subTest(job_id=job_id):
                with self.assertRaises(JobDirectoryError) as raised:
                    create_job_directory(self.root, job_id)
                self.assertEqual(raised.exception.code, "invalid_job_id")

    def test_rejects_staged_paths_that_escape_the_job(self):
        for relative in ["../escaped.stl", "/etc/passwd", "input/../../escaped.stl", "input\\model.stl", ""]:
            with self.subTest(relative=relative):
                with self.assertRaises(JobDirectoryError) as raised:
                    self.prepare(
                        job_id="job-escape", inputs=[JobInput(relative, self.source("model.stl"))]
                    )
                self.assertEqual(raised.exception.code, "invalid_staged_path")
                self.assertFalse((self.root / "job-escape").exists())

    def test_rejects_a_staged_path_that_replaces_the_manifest(self):
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(inputs=[JobInput("request.json", self.source("model.stl"))])
        self.assertEqual(raised.exception.code, "invalid_staged_path")

    def test_rejects_a_duplicate_staged_path(self):
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(
                inputs=[
                    JobInput("input/model.stl", self.source("model.stl")),
                    JobInput("input/model.stl", self.source("other.stl")),
                ]
            )
        self.assertEqual(raised.exception.code, "duplicate_staged_path")

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_does_not_stage_through_a_symlinked_directory(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        job = create_job_directory(self.root, "job-symlink")
        os.symlink(elsewhere, job / "input")

        with self.assertRaises(JobDirectoryError) as raised:
            _resolve_staged_path(job, "input/model.stl")
        self.assertEqual(raised.exception.code, "invalid_staged_path")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_enforces_the_single_input_size_limit(self):
        limits = JobDirectoryLimits(max_input_bytes=8, max_total_input_bytes=64)
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(inputs=[JobInput("model.stl", self.source("model.stl", b"x" * 32))], limits=limits)
        self.assertEqual(raised.exception.code, "job_input_size_limit_exceeded")
        self.assertFalse((self.root / "job-1").exists())

    def test_enforces_the_combined_input_size_limit(self):
        limits = JobDirectoryLimits(max_input_bytes=32, max_total_input_bytes=48)
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(
                inputs=[
                    JobInput("a.stl", self.source("a.stl", b"x" * 32)),
                    JobInput("b.stl", self.source("b.stl", b"x" * 32)),
                ],
                limits=limits,
            )
        self.assertEqual(raised.exception.code, "job_input_size_limit_exceeded")
        self.assertFalse((self.root / "job-1").exists())

    def test_enforces_the_input_count_limit(self):
        limits = JobDirectoryLimits(max_inputs=1)
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(
                inputs=[
                    JobInput("a.stl", self.source("a.stl")),
                    JobInput("b.stl", self.source("b.stl")),
                ],
                limits=limits,
            )
        self.assertEqual(raised.exception.code, "too_many_job_inputs")

    def test_leaves_nothing_behind_when_an_input_is_missing(self):
        with self.assertRaises(JobDirectoryError) as raised:
            self.prepare(inputs=[JobInput("model.stl", self.sources / "absent.stl")])
        self.assertEqual(raised.exception.code, "job_input_unreadable")
        self.assertFalse((self.root / "job-1").exists())

    def test_rejects_a_manifest_for_a_different_job(self):
        with self.assertRaises(JobDirectoryError) as raised:
            prepare_job_directory(self.root, "job-1", self.manifest("job-2"), [])
        self.assertEqual(raised.exception.code, "job_id_mismatch")

    def test_sweeps_expired_directories_and_keeps_recent_ones(self):
        expired = self.prepare(job_id="job-expired")
        recent = self.prepare(job_id="job-recent")
        limits = JobDirectoryLimits(retention_seconds=60)
        os.utime(expired.path, (0, 0))

        report = sweep_job_directories(self.root, limits)
        self.assertEqual(report.removed, ["job-expired"])
        self.assertEqual(report.retained, ["job-recent"])
        self.assertFalse(expired.path.exists())
        self.assertTrue(recent.path.exists())

    def test_never_sweeps_a_running_job(self):
        prepared = self.prepare(job_id="job-running")
        os.utime(prepared.path, (0, 0))

        report = sweep_job_directories(self.root, JobDirectoryLimits(retention_seconds=60), ["job-running"])
        self.assertEqual(report.removed, [])
        self.assertEqual(report.retained, ["job-running"])
        self.assertTrue(prepared.path.exists())

    def test_sweeps_an_abandoned_directory_with_no_manifest(self):
        abandoned = create_job_directory(self.root, "job-abandoned")
        os.utime(abandoned, (0, 0))

        report = sweep_job_directories(self.root, JobDirectoryLimits(retention_seconds=60))
        self.assertEqual(report.removed, ["job-abandoned"])
        self.assertFalse(abandoned.exists())

    def test_sweeps_a_stray_entry_in_the_job_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        stray = self.root / "stray.txt"
        stray.write_text("not a job")

        report = sweep_job_directories(self.root)
        self.assertEqual(report.removed, ["stray.txt"])
        self.assertFalse(stray.exists())

    def test_sweeping_a_missing_root_is_not_an_error(self):
        report = sweep_job_directories(Path(self.temporary.name) / "absent")
        self.assertEqual(report.removed, [])
        self.assertEqual(report.retained, [])

    def test_removing_a_job_directory_is_idempotent(self):
        prepared = self.prepare()
        self.assertTrue(remove_job_directory(prepared.path))
        self.assertFalse(remove_job_directory(prepared.path))


if __name__ == "__main__":
    unittest.main()
