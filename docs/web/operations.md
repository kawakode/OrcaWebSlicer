# Web deployment operations

Status: Reference procedure for the current single-instance filesystem deployment

This runbook covers health checks, graceful replacement, rollback, and recovery
for the web tier as it exists today. Job metadata storage is selected in
[ADR 0005](adr/0005-job-metadata-storage.md). This runbook does not select a
production host, an authentication provider, a database server, a queue, or an
object store.

## Supported topology and recovery boundary

Run exactly one API instance against one `ORCA_WEB_STATE_ROOT`. The state root
holds three things:

| Path | Contents |
| --- | --- |
| `uploads/` | One directory per upload: the model and its metadata |
| `jobs/` | One directory per job: staged inputs, the worker's report, and its artifacts |
| `metadata.sqlite3` (and its `-wal` and `-shm` files) | The job table |

The running API holds the job table in memory and writes every change through
to `metadata.sqlite3`. It reads the table back once at startup. Two API
instances sharing a state root would not see each other's jobs, and each
instance's sweeper would not know which jobs the other is running.

After a restart:

- uploads, finished jobs, their reports and artifacts, and their previews and
  scenes are served under the same IDs and URLs as before;
- jobs that a graceful stop canceled stay `canceled`;
- jobs that were queued or running when the process died are reported as
  `failed` with `job_interrupted`, and their directories are removed. They are
  not resumed. A client retries them.
- per-owner quota windows are rebuilt from the job records, and the request
  rate limiter starts empty.

Consequently:

- replacement and rollback are stop-then-start operations, never overlapping
  blue/green instances on the same state root;
- the recovery point is the last committed job record. A power loss can roll
  back the newest few, which then recover as interrupted or disappear;
- the 24-hour retention window is deletion policy, not a backup or recovery
  guarantee;
- a lost state volume means clients must upload and submit again.

## Probes

The service exposes three related endpoints:

| Endpoint | Meaning | Use |
| --- | --- | --- |
| `/api/v1/health` | Compatibility alias for process liveness | Existing clients |
| `/api/v1/health/live` | The HTTP process can answer | Process restart policy |
| `/api/v1/health/ready` | Admission is open, the worker command exists, and the state root accepts a write | Rollout and traffic gate |

Readiness does not fail merely because all worker slots are occupied. Saturation
is not a broken instance. Readiness changes to HTTP 503 when graceful shutdown
closes admission or when the worker or state mount is unavailable.

The Compose `api` service checks readiness on loopback. Inspect it with:

```powershell
docker compose -f docker/web/compose.yml up -d api
docker compose -f docker/web/compose.yml ps api
docker inspect --format '{{json .State.Health}}' orca-web-api-1
```

A shallow health response cannot prove that native slicing and artifact
publication work. Before sending traffic to a new or rolled-back release, run a
canary slice with deployment profile IDs:

```powershell
docker compose -f docker/web/compose.yml run --rm deployment-probe `
  python3 scripts/web_deployment_probe.py `
  --base-url http://api:8000 `
  --model tests/data/20mm_cube.obj `
  --machine 'Anycubic/machine/Anycubic Kobra 0.4 nozzle' `
  --process 'Anycubic/process/0.20mm Standard @Anycubic Kobra' `
  --filament 'Anycubic/filament/Anycubic Generic PLA'
```

The probe waits for readiness, uploads the model, slices it, downloads
`result.json` and G-code, and verifies the G-code size and SHA-256 against the
published artifact metadata. Run it from a container or host that can reach the
deployment; `http://api:8000` is only the Compose-network form.
Use `GET /api/v1/profiles` to discover the full `vendor/kind/name` IDs for a
different deployed catalog.

The reference API path is fixed to `/var/lib/orca-web` because that is where its
named volume is mounted. Changing `ORCA_WEB_STATE_ROOT` requires a Compose
override that changes the volume target at the same time. An environment-only
override would put state in the disposable container layer and is unsupported.

## Release record and preflight

Keep a release record outside the service's state volume. It must contain:

- the exact Git commit and whether the source tree was clean;
- the immutable API/worker/frontend image digest when a production runtime
  image exists;
- API and worker protocol versions;
- the profile and resource revision;
- non-secret configuration, including limits and retention;
- the SBOM, vulnerability report, and scanner versions from
  [security-scanning.md](security-scanning.md);
- the previous known-good release identifier.

The current Compose service is a development/reference topology. It runs
`orca-web-build:latest` with a bind-mounted checkout and mutable build volumes,
so its local tag is not an immutable rollback artifact. A production deployment
must pair the API, frontend, worker, resources, and profiles in one versioned
release and pin it by digest.

Before replacement:

1. Build and test the candidate from a clean checkout.
2. Run worker, executor, API, browser, baseline, and security checks required by
   `PLAN.md`.
3. Record the candidate and previous release identifiers.
4. Verify enough free space exists for the state retention window.
5. Launch the candidate against a separate disposable state root, without
   access to live state. Verify `/health/ready`, then run the full deployment
   probe. Stop that candidate before the live replacement begins.

Do not copy or snapshot the state directory while the API or a worker is running.
Requests and result files are transactional within a job, but a live copy is not
a consistent snapshot of the whole service.

## Graceful shutdown and replacement

On shutdown the server stops accepting HTTP connections, then the application:

1. closes job and upload admission;
2. marks queued jobs canceled without launching their workers and attempts to
   remove their staged directories, logging a cleanup failure for operations;
3. signals running workers for cancellation;
4. lets the executor escalate from `SIGTERM` to `SIGKILL` after its configured
   grace period;
5. waits for executor threads to finish and exits.

Reads and artifact downloads remain available while the process is reachable.
Every job's final state is stored before the process exits. The next process
serves the finished jobs and reports the canceled ones as canceled. It does not
resume them.

Compose gives Uvicorn 10 seconds for HTTP work and the container 30 seconds in
total. The worker termination grace defaults to 5 seconds, followed by bounded
pipe cleanup. If `ORCA_WEB_TERMINATION_GRACE_MS` is increased, also increase
`ORCA_WEB_API_STOP_GRACE_PERIOD` so the container runtime does not kill the API
before cleanup finishes.

```powershell
docker compose -f docker/web/compose.yml stop --timeout 30 api
docker compose -f docker/web/compose.yml up -d api
docker compose -f docker/web/compose.yml ps api
```

After restart, wait for healthy status and run the full deployment probe. Job
URLs from before the restart keep working until retention reclaims them. Jobs
the stop canceled can be retried.

## Rollback

Rollback restores software, profiles, and resources as one unit. It does not
roll worker and API versions independently.

1. Remove the candidate from traffic and stop it gracefully.
2. Confirm no API or worker process is still using the state root.
3. Start the previous release by its recorded immutable digest. For the current
   reference Compose topology, check out the recorded clean commit and rebuild;
   do not trust a mutable `latest` tag as evidence of identity.
4. Reapply the previous release's compatible non-secret configuration.
5. Wait for readiness and run the full deployment probe.
6. Restore traffic only after both checks pass.
7. Record the rollback reason, release IDs, data-loss boundary, and probe result.

If the previous version cannot safely read state written by the candidate, use
a new empty state root. The job database records its schema version, and a
release refuses to start against a newer schema than it knows. A release from
before ADR 0005 ignores the database, so its jobs are lost as they were before.
When a later release starts again, the records it finds that point at swept
directories are reclaimed by retention. Upload metadata and job directories have
no cross-version migration contract. Never run the two versions concurrently on
the same root.

The release that first introduces the dedicated `orca-web-state` volume is a
one-time storage boundary. Existing state under
`orca-app-build:/workspace/build/web-state` is not migrated automatically, and
a pre-boundary checkout does not mount the new volume. Rollback across that
boundary must use empty state unless an operator has performed and verified a
cold, permissions-preserving copy through an explicit Compose override.

## Disaster recovery

The supported recovery today is an empty-state rebuild:

1. Provision a clean host/runtime and a new private state volume.
2. Deploy the exact recorded release unit and non-secret configuration.
3. Verify liveness and readiness.
4. Run the full deployment probe.
5. Reopen traffic and tell clients to upload and submit again.

An operator may take a cold filesystem snapshot for short-lived forensic or
best-effort upload recovery, but it is not a durable service backup:

1. Stop the API and confirm no workers remain.
2. Copy the state root with permissions intact to encrypted, access-controlled
   storage.
3. Expire the snapshot no later than the data's remaining retention period.
4. Restore only into a stopped, single API instance using the same software
   release first.
5. Start the API and run the deployment probe.

Restoring such a snapshot restores the job table with it. Job URLs whose
directories are still inside the retention window work again. Jobs that were
running when the snapshot was taken are reported as interrupted. The snapshot
also makes known upload IDs reusable and preserves files for authorized manual
investigation.
Routine snapshots are deliberately not prescribed: retaining raw models and
G-code beyond their deletion window changes the privacy policy and must be
designed together with persistent storage, deletion auditing, and access
control.

## Destructive reset

`docker compose down --volumes` deletes every named volume in this Compose
project, including compiler caches, the dedicated `orca-web-state` volume, raw
uploads, job artifacts, and the job database. It is a full local reset and
cannot be undone unless an operator made an appropriate cold copy first.
