# ADR 0005: Job metadata and artifact storage

Date: 2026-09-21
Status: Accepted

## Context

[ADR 0002](0002-web-stack.md) deferred the database, queue, and object-store
products until local job throughput and artifact sizes had been measured. Until
now the job table lived only in the API's memory. Uploads already survived a
restart, because each carries its own metadata file, but every job ID, its
state, its report, and the path to its artifacts were lost when the process
stopped. The artifacts stayed on disk with nothing pointing at them. The rolling
CPU and submission quota windows were lost the same way.

The measurements were taken on the reference Compose deployment on 2026-09-21,
on its `orca-web-state` volume.

| Measure | Value |
| --- | --- |
| A 7.1 MB STL upload, sliced | 2.5 MB G-code, 1.8 MB preview blob, 22 KB preview index |
| The same upload, inspected | 0.95 MB scene blob, 525 B scene index |
| `result.json` | 510-810 B |
| A serialized job record | about 1-2 KB, dominated by the embedded result and profile chain |
| SQLite commit (WAL, `synchronous=FULL`) | 9.8 ms median, 40 ms p99 per insert |
| SQLite commit (WAL, `synchronous=NORMAL`) | 0.09 ms median, 0.9 ms p99 per insert; 0.06 / 0.5 ms per update |
| Loading 500 records at startup | 3 ms |

Throughput is bounded by the worker pool, not by storage.
`ORCA_WEB_MAX_CONCURRENT_JOBS` defaults to 2, and a slice takes from
tenths of a second to minutes. A job writes its record four times: accepted,
running, charged, and finished. At that rate the metadata store sees a few
writes per second at most. Artifacts are megabytes per job. They are written
once by one worker inside its own job directory, and then served by byte range.

## Decision

### Metadata: SQLite, embedded in the API process

Job records are stored in `metadata.sqlite3` at the root of the state volume,
beside `uploads/` and `jobs/`. Each record is one JSON document keyed by job ID,
with `owner_id`, `state`, and `created_at` lifted into columns so an operator
can inspect the table. The schema version is stored in `PRAGMA user_version`.
A database written by a newer schema is refused at startup rather than read.
The database and its journal files are mode `0600`, like upload files, so a
worker running as another user cannot read other owners' job records.

The in-memory table in `JobService` remains the authority while the process
runs. The store is a write-through copy, read once at startup:

- A job is saved before it is queued. If the store cannot record it, the job is
  refused with `metadata_store_unavailable` (HTTP 503) and its staged directory
  is removed, so no job runs that a restart would forget.
- Later transitions are saved as they happen. A failed later write is logged,
  not raised. The worst a stale copy can cause is the next case.
- At startup, a record that is still `queued` or `running` has no worker left.
  Its directory may hold files no executor validated, so it is removed, and the
  job becomes `failed` with `internal` / `job_interrupted`. A retry reruns the
  same inputs.
- The quota windows are rebuilt from the records: each job's submission time,
  and the CPU charge and charge time now stored on it.
- A finished record, its directory, and its artifacts expire together, one
  retention window after the job finished. That includes records for failed
  and canceled jobs, whose directories were removed when they finished.
- The deletion audit is a second, append-only table, `deletions`, in the same
  database. It is additive, so it does not change the schema version, and a
  release that predates it ignores it. See
  [api.md](../api.md#retention-and-deletion-audit).

WAL with `synchronous=NORMAL` is chosen over `FULL` because it is a hundred
times cheaper and every record write happens under the job table's lock. A
committed record survives an API crash. A power loss or kernel crash can roll
back the newest commits. A job then reappears in an earlier state, which
recovery already fails as interrupted, or it disappears, and its orphaned
directory is reclaimed by the retention sweep. The job directories themselves
are not fsynced either, so `FULL` would not buy more than that.

`sqlite3` is in the Python standard library. It adds no dependency to scan, no
service to run, and no credentials to manage.

### Artifacts: the job directories on the state volume

Artifacts stay where the worker writes them, in the job directory the executor
validated. That is the one place the sandbox, the transactional publication in
the worker protocol, and the path-containment checks already agree on. Byte-range
serving of preview layers and scene objects reads the files directly. Copying
megabytes per job to a second store would add latency and a second copy to
delete, and it would protect nothing, because the database sits on the same
volume.

### Deliberately not selected

- **A database server such as PostgreSQL.** It pays off only when several API
  instances must share job state, and the supported topology is one instance
  per state root. Moving the store to a server later changes one module,
  because the store's interface is three calls: load, save, and delete.
- **An object store such as S3.** It pays off when artifacts must outlive or
  leave the host, or when API instances run on different hosts. Neither is true
  yet, and the worker would still have to write to local disk first.
- **A queue.** The executor pool is in-process and bounded. Queued jobs are not
  resumed after a restart, so there is nothing a queue would hold.

## Consequences

### Positive

- Job IDs, reports, artifacts, previews, scenes, retries, and scene
  deduplication survive an API restart and a stop-then-start rollback.
- The quota windows survive a restart instead of resetting to empty.
- Failed and canceled job records no longer accumulate for the life of the
  process.
- No new dependency, service, or credential.

### Costs and limits

- Still one API instance per state root. SQLite makes concurrent access safe,
  but the in-memory table would not see another instance's jobs.
- A job that was running when the API stopped is not resumed. A graceful stop
  cancels it, and a crash fails it as interrupted.
- A job interrupted by a crash was never charged CPU time, because the charge
  is recorded only when its worker exits.
- Quota windows longer than the retention window lose their oldest entries
  after a restart, because the records that carried them were swept.
- The per-owner request rate limiter is still process-local. It measures bursts
  over seconds, so losing it on restart does not matter.
- A backup of the state volume still has to be cold. A live copy of the
  database and the job directories is not a consistent snapshot of the service.

## Rejected alternatives

### One metadata file per job directory

This would match how uploads work. It was rejected because failed and canceled
jobs have no directory by design, so their state would be lost, and because
loading the table would mean reading one file per job.

### Keeping metadata in memory

This was rejected because it is what made every restart and rollback lose
every job. Durable recovery is a G6 exit gate.
