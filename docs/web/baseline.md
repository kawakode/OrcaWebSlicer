# Native slicing compatibility baseline

Status: Native matrix and worker behaviour matrix complete

## Purpose

The baseline prevents the web extraction from silently changing slicing output.
It measures the current native implementation before `OrcaSlicer.cpp` is split
or GUI-owned helpers are moved behind core-facing interfaces.

The baseline has two lanes:

1. **Engine lane:** Catch2 tests call `libslic3r` directly. This is the primary
   compatibility gate because it does not include desktop state.
2. **CLI lane:** End-to-end jobs exercise profile loading, 3MF import, validation,
   progress, G-code export, and result reporting. This captures the behavior the
   worker must replace.

## Fixture matrix

Existing repository fixtures are reused before adding new binary assets.

| Case | Source | Defining behavior |
| --- | --- | --- |
| `cube-default` | `tests/data/20mm_cube.obj` | Basic import, layer generation, extrusion, and G-code export |
| `bridge` | `tests/data/bridge.obj` | Bridge detection and bridge extrusion roles |
| `overhang-support` | `tests/data/overhang.obj` | Support generation when explicitly enabled |
| `concave-hole` | `tests/data/cube_with_concave_hole.obj` | Polygon topology and hole preservation |
| `multipart` | `tests/data/two_hollow_squares.obj` | Multiple disconnected regions in one mesh |
| `unicode-3mf` | `tests/data/test_3mf/Geräte/Büchse.3mf` | 3MF archive import and Unicode paths |
| `invalid-config` | Generated manifest | Stable rejection of an unknown or invalid option value |
| `cancel` | A deliberately slow generated job | Worker cancellation and partial-artifact cleanup |

Additional fixtures are added only when they protect a distinct contract such as
multi-filament tool ordering or modifier volumes.

## Recorded result

Each run writes a JSON record with:

- repository commit and dirty-state marker;
- executable identity and build configuration;
- operating system and CPU architecture;
- fixture and effective configuration identity;
- process exit status and normalized error code;
- wall-clock duration and peak resident memory;
- warnings;
- output file sizes;
- semantic G-code summary;
- raw output SHA-256 for diagnostics only.

The semantic summary contains at least:

- layer count and Z range;
- tool-change count and ordered tool identifiers;
- extrusion-role counts;
- XY motion bounds;
- total travel distance;
- total positive extrusion by tool;
- configured and emitted temperatures;
- required start and end command markers.

Raw G-code is retained as a CI artifact and is not committed to the repository.

## Initial execution result

The Docker baseline completed on 2026-09-01 against commit
`9099cc5a9d7507f69d181b0aca707830e98b72d9` with the groundwork changes present
in the working tree. All 610 registered CTest tests passed; five existing tests
were explicitly skipped. The packaged slicer SHA-256 was
`b1d9bacc6ec8061a19434b05b11721f48946ab6c754f831adc99e43eacf3fda1`.

| Case | Layers | G-code bytes | Wall time range | Peak RSS range | Semantic repeat |
| --- | ---: | ---: | ---: | ---: | --- |
| `cube-default` | 100 | 346,079 | 0.459-0.522 s | 96.2-98.0 MiB | Identical |
| `bridge` | 40 | 188,531 | 0.460-0.461 s | 95.0-96.6 MiB | Identical |
| `concave-hole` | 50 | 348,155 | 0.565-0.566 s | 96.0-100.7 MiB | Identical |

The runner resolves each selected shipped profile's inheritance into a
self-contained temporary JSON file. This matches desktop preset layering while
keeping generated configurations and raw artifacts out of version control.

## Headless parity result

On 2026-09-02, `orca-slicer-worker` loaded the same resolved machine, process,
and filament profiles and reproduced every recorded semantic field for
`cube-default`, `bridge`, and `concave-hole`. The comparison covers layer and Z
ranges, tools, extrusion roles and totals, temperatures, command counts, XY
bounds, and motion distance. Run it against the latest native record with:

```powershell
docker compose -f docker/web/compose.yml run --rm worker-baseline
```

## Worker behaviour matrix

Some fixture-matrix cases have no desktop CLI equivalent: they exercise worker
setting overrides, stable error codes, and limits. They live in
[`worker-matrix-cases.json`](worker-matrix-cases.json) and run with:

```powershell
docker compose -f docker/web/compose.yml run --rm worker-matrix
```

Each case runs twice. The runner checks the terminal outcome, the error code and
offending setting for rejected jobs, the extrusion roles and tools for accepted
ones, that no partial or published artifact survives a failure, and that both
runs produce the same semantic G-code summary.

| Case | Defining behaviour |
| --- | --- |
| `overhang-support` / `overhang-no-support` | The `enable_support` override adds and removes support extrusion |
| `multipart` | Disconnected regions of one mesh slice with a single tool |
| `unicode-3mf` | A 3MF project under a non-ASCII path slices its selected plate |
| `invalid-config` | A malformed value fails with `invalid_setting_value` naming `layer_height` |
| `output-limit` | An exceeded output budget fails with `output_size_limit_exceeded` and publishes nothing |

Cancellation is covered by `scripts/test_web_worker_cancellation.py` and the
remaining limits by `scripts/test_web_worker_limits.py`, both run by
`worker-smoke`. Multi-filament tool ordering is deferred with multi-material
support: the MVP worker accepts one filament profile per job.

## Continuous integration

[`.github/workflows/web.yml`](../../.github/workflows/web.yml) runs the Compose
checks on every pull request to `main`, on every push to `main`, and on demand.
Each check step calls `scripts/run_web_service.py <service>`, which reads the
service's command, working directory, and environment from
`docker compose config` and runs it directly on the Ubuntu 24.04 runner. The
Compose file therefore stays the only definition of each check: a new assertion
added to a service runs in CI without touching the workflow. The runner exposes
the checkout as `/workspace` and creates `/cache`, the two paths the services
use. Container-only settings (`user`, `read_only`, `network_mode`, `pids_limit`)
are not reproduced on the runner; the executor's own process limits still apply.

| Job | Compose services | Needs |
| --- | --- | --- |
| Dependencies | none (`build_linux.sh -drlL`) | |
| Worker build, smoke and matrix | `worker-build`, `worker-smoke`, `worker-matrix` | Dependencies |
| Native baseline | `baseline`, after `build_linux.sh -srlL` | Dependencies |
| Frontend build | `frontend-build` | |
| Executor and baseline parity | `executor-smoke`, `worker-baseline`, `api-baseline` | Worker, native baseline |
| Browser end-to-end | `frontend-e2e` (Chromium and Firefox block; WebKit is reported only) | Worker |

The native baseline is a CI artifact (`web-native-baseline`, kept 30 days)
together with its raw G-code; it is never committed. The worker binary is passed
between jobs as the `orca-slicer-worker` artifact.

### Caches

| Cache | Key | Saved by |
| --- | --- | --- |
| Dependency build `deps/build/OrcaSlicer_dep` | `linux-clang-cache-orcaslicer_deps-build-<hash of deps/**>` | Dependencies job on a miss; shared with `build_all.yml` |
| Worker ccache `/cache/ccache` | `web-worker-ccache-<sha>`, restored by prefix | `main`, or a pull request that found none |
| Desktop ccache `/cache/ccache` | `web-desktop-ccache-<sha>`, restored by prefix | as above |
| Native baseline `build/web-baseline` | `web-native-baseline-<hash of the desktop sources, deps, Anycubic profiles, baseline manifest and runner>` | Native baseline job on a miss |
| npm | `actions/setup-node` cache of `package-lock.json` | every run |
| Playwright browsers `/cache/playwright` | `web-playwright-<hash of package-lock.json>` | every run on a miss |

Pull requests read the caches saved on `main`. They save a compiler cache only
when none existed, so several open branches cannot evict the dependency build
from the repository's 10 GB cache budget. The baseline key excludes
`src/libslic3r/Web/**` and `src/OrcaSlicerWorker.cpp`, which the desktop slicer
does not use, so web-only changes reuse the recorded reference. Each compiler
cache is capped at 1.5 GB (`CCACHE_MAXSIZE`).

### Durations

Expected wall-clock times on a standard 4-core `ubuntu-24.04` runner, before
the first measured run replaces them:

| Job | Cold caches | Warm caches |
| --- | ---: | ---: |
| Dependencies | about 90 min | under 1 min |
| Worker build, smoke and matrix | about 60 min | about 15 min |
| Native baseline | about 75 min | under 1 min (cache hit) |
| Frontend build | about 2 min | about 1 min |
| Executor and baseline parity | about 5 min | about 5 min |
| Browser end-to-end | about 10 min | about 6 min |

A pull request with warm caches finishes in about 25 minutes; the first run on
a new dependency hash takes about three hours.

## Reproducibility rules

- Pin the compiler, dependency build, profiles, locale, and worker resource limits.
- Set every configuration value on which an assertion depends.
- Normalize only known nondeterministic fields; never filter geometry or motion.
- Run every fixture twice. A semantic difference between identical consecutive
  runs is a baseline failure.
- Record performance but use a broad regression threshold until CI variance is
  measured. Correctness failures are always blocking.
- Keep desktop and extracted-worker results in the same report for direct review.

## First execution

Linux Docker is the canonical baseline environment. Build and test it from the
repository root:

```powershell
docker compose -f docker/web/compose.yml build
docker compose -f docker/web/compose.yml run --rm build
docker compose -f docker/web/compose.yml run --rm tests
```

See [the Docker baseline guide](../../docker/web/README.md) for resource,
caching, artifact-copy, and reset instructions.

After the existing tests are green, the next implementation change adds the
end-to-end baseline runner and its machine-readable manifest:

```powershell
$env:ORCA_WEB_GIT_COMMIT = (git rev-parse HEAD).Trim()
$env:ORCA_WEB_GIT_DIRTY = if (git status --porcelain) { "true" } else { "false" }
docker compose -f docker/web/compose.yml run --rm baseline
```

The runner executes each case twice, stores logs and raw artifacts below
`build/web-baseline`, produces semantic G-code summaries, and fails when the two
runs differ semantically. Measurements must not be committed until they have been
produced by this runner.
