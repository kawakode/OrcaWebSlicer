# Docker baseline environment

This Compose project is the canonical Linux environment for the web worker and
native compatibility baseline. It uses Ubuntu 24.04 and the same CMake and Linux
dependency installation paths as OrcaSlicer's CI.

Docker Desktop should have at least 8 GiB of memory and 20 GiB of free disk for
the initial dependency and application build. Two compile jobs are used by
default; reduce this on a smaller machine:

```powershell
$env:ORCA_WEB_BUILD_JOBS = "1"
```

## Build

From the repository root:

```powershell
docker compose -f docker/web/compose.yml build
docker compose -f docker/web/compose.yml run --rm build
```

The first build is long because OrcaSlicer's native dependencies are compiled.
The dependency build, application build, and ccache are named volumes, so later
runs are incremental. All services share one build-environment image; only their
commands and build-volume targets differ.

## Build the headless worker

The worker has a separate CMake tree configured with `SLIC3R_GUI=OFF`. This is
an architectural check, not just a differently named desktop executable:

```powershell
docker compose -f docker/web/compose.yml run --rm worker-build
docker compose -f docker/web/compose.yml run --rm worker-smoke
docker compose -f docker/web/compose.yml run --rm executor-smoke
docker compose -f docker/web/compose.yml run --rm worker-baseline
```

The Compose environment sets `ORCA_SLICER_RESOURCES=/workspace/resources`.
Packaged workers must set the same variable when the resources directory is not
discoverable beside the executable or in the current working directory.

Worker ceilings default to 250 MiB of input, 1,000,000 triangles, 300 seconds,
4 GiB of memory, 1 GiB of generated G-code, 1 GiB of content extracted from a
project archive, and 256 MiB of layer preview. Override them with
`ORCA_WEB_MAX_INPUT_BYTES`, `ORCA_WEB_MAX_TRIANGLES`,
`ORCA_WEB_MAX_WALL_TIME_MS`, `ORCA_WEB_MAX_MEMORY_BYTES`,
`ORCA_WEB_MAX_OUTPUT_BYTES`, `ORCA_WEB_MAX_EXTRACTED_BYTES`, and
`ORCA_WEB_MAX_PREVIEW_BYTES`. Values are byte counts except wall time, which is
milliseconds. Manifest limits can only tighten these server ceilings. A print
whose preview would exceed its ceiling still succeeds, with the G-code and a
warning saying the preview was omitted.

## Run the API and the browser screen

```powershell
docker compose -f docker/web/compose.yml run --rm frontend-build
docker compose -f docker/web/compose.yml up api
docker compose -f docker/web/compose.yml run --rm frontend-e2e
docker compose -f docker/web/compose.yml run --rm api-baseline
```

`api` serves the FastAPI service documented in [docs/web/api.md](../../docs/web/api.md)
on `http://localhost:8000`, driving the worker built by `worker-build`. Once
`frontend-build` has produced `web/frontend/dist`, the same origin also serves
the [browser screen](../../docs/web/frontend.md). Set `ORCA_WEB_API_PORT` to
publish it elsewhere. `ORCA_WEB_STATE_ROOT`, `ORCA_WEB_WORKER`,
`ORCA_WEB_PROFILE_VENDORS`, `ORCA_WEB_MAX_CONCURRENT_JOBS`,
`ORCA_WEB_FRONTEND_DIST`, the retention and deletion-audit settings
([api.md](../../docs/web/api.md#retention-and-deletion-audit)), the
`ORCA_WEB_QUOTA_*` per-owner quotas ([api.md](../../docs/web/api.md#quotas)), and the `ORCA_WEB_RATE_*` request
rate ([api.md](../../docs/web/api.md#abuse-controls-and-rate-limiting))
configure it.

The compose file sets `ORCA_WEB_AUTH_MODE=disabled` for this `api` service,
visibly, because this reference topology has no authenticating edge proxy in
front of it yet — that is also why its port is bound to `127.0.0.1` only. It
treats every request as one fixed `local-development` principal. A real
deployment sets `ORCA_WEB_AUTH_MODE=required` behind an edge that injects a
verified `Authorization: Bearer` assertion, and mounts `ORCA_WEB_AUTH_ISSUER`,
`ORCA_WEB_AUTH_AUDIENCE`, and a read-only `ORCA_WEB_AUTH_JWKS_PATH`; see the
[authentication section of the API contract](../../docs/web/api.md#authentication-and-authorization)
and [ADR 0004](../../docs/web/adr/0004-service-identity.md).

The container healthcheck gates on `/api/v1/health/ready`; `/health/live` is the
process-only probe. Mutable uploads and jobs live in the dedicated
`orca-web-state` volume at `/var/lib/orca-web`, separate from build outputs.
`init: true`, Uvicorn's 10-second HTTP shutdown window, and the configurable
`ORCA_WEB_API_STOP_GRACE_PERIOD` (30 seconds by default) give workers time to
cancel and escalate cleanly. The full replacement, rollback, and disaster
recovery procedure is in [operations.md](../../docs/web/operations.md).
The Compose state path is intentionally fixed to `/var/lib/orca-web`; changing
it safely requires an override that also changes the `orca-web-state` mount
target.

`frontend` runs the Vite dev server with hot reload on port 5173 and proxies
`/api` to `ORCA_WEB_API_URL`. `frontend-e2e` builds the bundle, installs the
browser matrix, and runs the Playwright suite against the API and the real
worker. Chrome, Edge, and Firefox are release-blocking, so a failure in any of
them fails the service; WebKit stands in for Safari and is reported without
failing it. `npm run e2e` alone stays Chromium-only for a fast local loop.
`api-baseline` slices the recorded baseline fixtures through the HTTP API and
compares the downloaded G-code with the native baseline run.

Node and the browser cache follow the same rule as the C++ build: `node_modules`
and `/cache/playwright` are named volumes, so their many small files never touch
the host bind mount.

`executor-smoke` runs the whole `tests/web` suite: the profile catalog, the job
service against fake workers, and the HTTP surface, in addition to verifying the
framework-neutral process boundary against fake failure workers and the real C++
worker. The executor applies hard Linux CPU,
address-space, file-size, process-count, and file-descriptor limits; bounds
NDJSON events and stderr; and escalates from `SIGTERM` to `SIGKILL` after a
configurable grace period. Its additional settings are
`ORCA_WEB_MAX_EVENT_BYTES`, `ORCA_WEB_MAX_EVENT_LINE_BYTES`,
`ORCA_WEB_MAX_EVENTS`, `ORCA_WEB_MAX_LOG_BYTES`,
`ORCA_WEB_MAX_OPEN_FILES`, `ORCA_WEB_MAX_PROCESSES`,
`ORCA_WEB_MAX_CPU_TIME_MS`, and `ORCA_WEB_TERMINATION_GRACE_MS`.

The smoke service itself is unprivileged, offline, capability-free, read-only
outside its private temporary storage, and cgroup PID-limited, so it also
covers the case where the executor has no privilege to give away.

`sandbox-smoke` covers the other case, and the one a deployment actually runs:
a service that starts as root, on a network, launching workers that get
neither. It keeps the container's network on purpose — that is what makes the
worker's own refusal to reach it worth measuring — and runs the sandbox suite
plus the real worker end to end.

```powershell
docker compose -f docker/web/compose.yml run --rm sandbox-smoke
```

Every worker runs confined either way: no network, no capabilities, no
privileges to gain, `TMPDIR` and `HOME` inside its own job directory, an
environment rebuilt from an allowlist, and never as root.
`ORCA_WEB_WORKER_SANDBOX` defaults to `required` and fails a job closed when a
control cannot be applied; `off` disables it for a host that cannot provide the
Linux controls. `ORCA_WEB_WORKER_USER` names the numeric `uid[:gid]` a root
executor switches its workers to, and is required of one.
[ADR 0003](../../docs/web/adr/0003-worker-sandbox.md) records why these are
seccomp and `prctl` rather than namespaces.

## Generate SBOMs and scan dependencies

After building `orca-web-build:latest`, run the pinned Syft and Grype toolchain:

```powershell
docker compose -f docker/web/compose.yml build security-scan
docker compose -f docker/web/compose.yml run --rm security-scan
```

The command inventories the image and the frontend lockfile, writes CycloneDX
SBOMs and complete vulnerability reports under `artifacts/web-security/`, and
fails on fixable High or Critical findings. See
[security-scanning.md](../../docs/web/security-scanning.md) for artifact names,
the scanner update procedure, the socket-free scanner boundary, and native C++
coverage limitations.

The smoke check runs the focused manifest, protocol, request, project-archive,
settings-catalog, profile-compatibility, core-color, and flush-volume
contract tests; exercises stable input, triangle, wall-time, memory, and output
limit failures; slices a 20 mm cube both from explicit settings and from resolved
Anycubic machine/process/filament profiles; slices a Unicode-path 3MF project and
rejects multi-plate, over-extracting, and root-escaping archives; exercises the version and
manifest-envelope commands; exports the engine settings catalog and resolves a
`compatible_printers_condition` against a real bundled printer; verifies the
published layer preview against the G-code it came from; verifies terminal NDJSON
events, `result.json` artifact metadata, and the invalid-manifest exit code; and
rejects GUI, OpenGL, device-access, embedded Python, WebKit, and media libraries
in the worker's dynamic dependency list.

`worker-baseline` compares cube, bridge, and concave-hole worker output against
the newest native run under `build/web-baseline`. Run the `baseline` service
first when that volume is empty or when the desktop reference needs refreshing.

## Verify and record the baseline

After a successful build:

```powershell
docker compose -f docker/web/compose.yml run --rm tests
$env:ORCA_WEB_GIT_COMMIT = (git rev-parse HEAD).Trim()
$env:ORCA_WEB_GIT_DIRTY = if (git status --porcelain) { "true" } else { "false" }
docker compose -f docker/web/compose.yml run --rm baseline
```

Passing the Git metadata from the host avoids a slow full repository scan
through Docker Desktop's Windows bind mount. If these variables are omitted,
the runner discovers the same metadata with Git inside the container.

Reports and raw baseline artifacts are written into the `orca-app-build` volume at
`/workspace/build/web-baseline`. To copy the latest reports to the host:

```powershell
docker compose -f docker/web/compose.yml run --rm --no-deps `
  -v "${PWD}/artifacts:/artifacts" baseline `
  bash -lc 'cp -a build/web-baseline/. /artifacts/'
```

`artifacts/` is diagnostic output and must not be committed.

The baseline runner materializes inherited process, machine, and filament
profiles inside each report directory before invoking the CLI. Its report
records the source chain and SHA-256 of each effective profile.

## Reset

Stop containers without deleting the build cache:

```powershell
docker compose -f docker/web/compose.yml down
```

Delete every named volume only when a genuinely full local reset is required:

```powershell
docker compose -f docker/web/compose.yml down --volumes
```

This also permanently deletes the `orca-web-state` volume, including raw model
uploads and job artifacts. It is not merely a build-cache cleanup command.
