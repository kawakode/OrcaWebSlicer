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
docker compose -f docker/web/compose.yml run --rm worker-baseline
```

The Compose environment sets `ORCA_SLICER_RESOURCES=/workspace/resources`.
Packaged workers must set the same variable when the resources directory is not
discoverable beside the executable or in the current working directory.

The smoke check runs the focused manifest, protocol, request, core-color, and flush-volume
contract tests; slices a 20 mm cube both from explicit settings and from resolved
Anycubic machine/process/filament profiles; exercises the version and manifest-
envelope commands; verifies terminal NDJSON events, `result.json` artifact
metadata, and the invalid-manifest exit code; and rejects GUI,
OpenGL, device-access, embedded Python, WebKit, and media libraries in the
worker's dynamic dependency list.

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

Delete all compiled dependencies, application outputs, and ccache only when a
genuinely clean rebuild is required:

```powershell
docker compose -f docker/web/compose.yml down --volumes
```
