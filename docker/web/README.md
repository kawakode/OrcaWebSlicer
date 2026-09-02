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
runs are incremental.

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
