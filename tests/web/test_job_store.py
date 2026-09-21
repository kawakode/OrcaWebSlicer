#!/usr/bin/env python3

import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from web.api.errors import ApiError  # noqa: E402
from web.api.store import SCHEMA_VERSION, JobStore  # noqa: E402


def record(job_id, created_at=1.0, state="queued"):
    return {"job_id": job_id, "owner_id": "1" * 64, "state": state, "created_at": created_at, "payload": [1, 2]}


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state" / "metadata.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trips_records_oldest_first_across_connections(self):
        store = JobStore(self.path)
        store.save(record("job-b", created_at=2.0))
        store.save(record("job-a", created_at=1.0))
        store.save(record("job-b", created_at=2.0, state="succeeded"))
        store.close()

        reopened = JobStore(self.path)
        loaded = reopened.load()
        self.assertEqual([item["job_id"] for item in loaded], ["job-a", "job-b"])
        self.assertEqual(loaded[1]["state"], "succeeded")
        self.assertEqual(loaded[0]["payload"], [1, 2])

        reopened.delete(["job-a", "job-never-stored"])
        self.assertEqual([item["job_id"] for item in reopened.load()], ["job-b"])
        reopened.close()

    @unittest.skipUnless(sys.platform == "linux", "POSIX modes are checked on Linux")
    def test_keeps_the_database_and_its_journal_private(self):
        store = JobStore(self.path)
        store.save(record("job-a"))
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.path}{suffix}")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        store.close()

    def test_skips_a_row_that_is_not_a_json_object(self):
        JobStore(self.path).close()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", ("job-x", "1" * 64, "queued", 1.0, "{not json")
            )
            connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", ("job-y", "1" * 64, "queued", 2.0, "[]"))
        store = JobStore(self.path)
        self.assertEqual(store.load(), [])
        store.close()

    def test_refuses_a_database_written_by_a_newer_schema(self):
        JobStore(self.path).close()
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaises(ApiError) as raised:
            JobStore(self.path)
        self.assertEqual(raised.exception.code, "invalid_api_configuration")

    def test_reports_a_write_to_a_closed_store_as_unavailable(self):
        store = JobStore(self.path)
        store.close()
        with self.assertRaises(ApiError) as raised:
            store.save(record("job-a"))
        self.assertEqual(raised.exception.code, "metadata_store_unavailable")
        self.assertEqual(raised.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
