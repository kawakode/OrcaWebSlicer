# Web deployment operations

Status: Reference procedure for the current single-instance filesystem deployment

This runbook covers health checks, graceful replacement, rollback, and recovery
for the web tier as it exists today. It does not select a production host,
authentication provider, database, queue, or object store. Those decisions remain
in G6.

## Supported topology and recovery boundary

Run exactly one API instance against one `ORCA_WEB_STATE_ROOT`. The job registry
and scene index are process memory. Two API instances sharing a state directory
would not share that registry, and each instance's sweeper would not know which
jobs the other instance is running.

Uploads and worker files are stored below the state root. An upload can be opened
again after a restart when the client retained its upload ID. Job records are not
rehydrated. After any API restart, old job IDs and artifact URLs return
`unknown_job`, even when files from those jobs remain on disk. In-flight jobs are
canceled during a graceful stop and are not resumed.

Consequently:

- replacement and rollback are stop-then-start operations, never overlapping
  blue/green instances on the same state root;
- the current recovery point and recovery time objectives are not guaranteed;
- the 24-hour retention window is deletion policy, not a backup or recovery
  guarantee;
- a lost state volume means clients must upload and submit again.

Durable job recovery belongs with the persistent metadata and artifact storage
decision in phase 4 of `PLAN.md`.

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
There is no resumable job state.

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

After restart, wait for healthy status and run the full deployment probe. Treat
old job URLs as expired and have clients resubmit from a surviving upload or
upload the model again.

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
a new empty state root. The current formats have no cross-version migration
contract. Never run the two versions concurrently on the same root.

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

Restoring such a snapshot does not restore old job URLs. It may make a known
upload ID reusable, and it preserves files for authorized manual investigation.
Routine snapshots are deliberately not prescribed: retaining raw models and
G-code beyond their deletion window changes the privacy policy and must be
designed together with persistent storage, deletion auditing, and access
control.

## Destructive reset

`docker compose down --volumes` deletes every named volume in this Compose
project, including compiler caches, the dedicated `orca-web-state` volume, raw
uploads, and job artifacts. It is a full local reset and cannot be undone unless
an operator made an appropriate cold copy first.
