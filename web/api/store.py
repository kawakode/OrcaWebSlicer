"""Durable job metadata: one SQLite database beside the job directories.

The job table in `JobService` stays the authority while the process runs; this
store is its write-through copy, read back once at startup so job IDs, their
state, and their artifacts outlive a restart. Artifacts themselves stay in the
job directories on the same state volume. See ADR 0005 for why SQLite and why
not an object store yet.

Each record is one JSON document keyed by job ID, with the owner and state
lifted into columns so an operator can inspect the table without parsing it.
The schema version lives in `PRAGMA user_version`; a database written by a
newer schema is refused rather than read.

The same database holds the deletion audit: one append-only row per job
directory, job record, or upload the service deleted, saying when, why, whose,
and how many bytes. It never holds a filename, a request, or any content. It is
an additive table the jobs schema does not depend on, so it does not change
the schema version, and a release that predates it simply leaves it alone.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import ApiError


SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner_id);
CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_at REAL NOT NULL,
    kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    owner_id TEXT,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS deletions_time ON deletions (deleted_at);
CREATE INDEX IF NOT EXISTS deletions_owner ON deletions (owner_id);
"""
_DELETION_COLUMNS = ("deleted_at", "kind", "subject_id", "owner_id", "reason", "outcome", "bytes", "created_at")


def _unavailable() -> ApiError:
    return ApiError("metadata_store_unavailable", "Job metadata could not be stored.", 503)


class JobStore:
    """Save, load, and delete serialized job records.

    WAL with `synchronous=NORMAL`: a committed record survives an API crash,
    and a power loss can at most roll the newest records back to an earlier
    state, which recovery already treats as an interrupted job.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Private to the API user, like upload files: workers run as
            # another user and must not read other jobs' records. SQLite gives
            # its -wal and -shm files the database file's mode.
            os.close(os.open(self._path, os.O_WRONLY | os.O_CREAT, 0o600))
            os.chmod(self._path, 0o600)
            # One connection shared by the request and worker threads, and
            # serialized by `_lock`; autocommit, so every statement is durable
            # on its own.
            self._connection = sqlite3.connect(
                str(self._path), check_same_thread=False, isolation_level=None
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ApiError(
                    "invalid_api_configuration",
                    f"the job metadata database has schema {version}; this release reads up to {SCHEMA_VERSION}.",
                    500,
                )
            self._connection.executescript(_SCHEMA)
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except (OSError, sqlite3.Error) as error:
            raise ApiError(
                "invalid_api_configuration", f"the job metadata database could not be opened: {error}", 500
            ) from error

    def load(self) -> List[Dict[str, Any]]:
        """Every stored record, oldest first. A row that is not valid JSON is skipped."""
        with self._lock:
            rows = self._connection.execute("SELECT record FROM jobs ORDER BY created_at").fetchall()
        records = []
        for (serialized,) in rows:
            try:
                record = json.loads(serialized)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def save(self, record: Dict[str, Any]) -> None:
        serialized = json.dumps(record, separators=(",", ":"))
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT OR REPLACE INTO jobs (job_id, owner_id, state, created_at, record) VALUES (?, ?, ?, ?, ?)",
                    (record["job_id"], record["owner_id"], record["state"], record["created_at"], serialized),
                )
        except sqlite3.Error as error:
            raise _unavailable() from error

    def delete(self, job_ids: Iterable[str]) -> None:
        batch = [(job_id,) for job_id in job_ids]
        if not batch:
            return
        try:
            with self._lock:
                self._connection.executemany("DELETE FROM jobs WHERE job_id = ?", batch)
        except sqlite3.Error as error:
            raise _unavailable() from error

    def record_deletion(self, entry: Dict[str, Any]) -> None:
        """Append one deletion audit row; `entry` names every `_DELETION_COLUMNS` field."""
        try:
            with self._lock:
                self._connection.execute(
                    f"INSERT INTO deletions ({', '.join(_DELETION_COLUMNS)}) VALUES ({', '.join('?' * len(_DELETION_COLUMNS))})",
                    tuple(entry[column] for column in _DELETION_COLUMNS),
                )
        except sqlite3.Error as error:
            raise _unavailable() from error

    def deletions(
        self, since: float = 0.0, owner_id: Optional[str] = None, subject_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Audit rows deleted at or after `since`, oldest first, optionally for one owner or subject."""
        query = f"SELECT {', '.join(_DELETION_COLUMNS)} FROM deletions WHERE deleted_at >= ?"
        parameters: List[Any] = [since]
        if owner_id is not None:
            query += " AND owner_id = ?"
            parameters.append(owner_id)
        if subject_id is not None:
            query += " AND subject_id = ?"
            parameters.append(subject_id)
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY id", parameters).fetchall()
        return [dict(zip(_DELETION_COLUMNS, row)) for row in rows]

    def prune_deletions(self, before: float) -> int:
        """Drop audit rows older than the audit's own retention; returns how many."""
        try:
            with self._lock:
                return self._connection.execute("DELETE FROM deletions WHERE deleted_at < ?", (before,)).rowcount
        except sqlite3.Error as error:
            raise _unavailable() from error

    def close(self) -> None:
        with self._lock:
            self._connection.close()
