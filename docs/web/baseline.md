# Native slicing compatibility baseline

Status: Fixture matrix expanded to support, multipart, invalid-configuration,
Unicode 3MF, and output-limit cases. Multi-filament and cancellation are
deliberately not part of this manifest; see the note at the end of the fixture
matrix.

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

Existing repository fixtures are reused before adding new binary assets. Every
case is declared once in `docs/web/baseline-cases.json` and driven by three
runners that share that manifest: `scripts/web_baseline.py` (native desktop
CLI), `scripts/web_worker_baseline.py` (headless `orca-slicer-worker`), and
`scripts/test_web_api_baseline.py` (the HTTP API through `TestClient`). A
case's `lanes` field lists which of the three it runs in; when omitted it
runs in all three.

| Case | Source | Lanes | Defining behavior |
| --- | --- | --- | --- |
| `cube-default` | `tests/data/20mm_cube.obj` | native, worker, api | Basic import, layer generation, extrusion, and G-code export |
| `bridge` | `tests/data/bridge.obj` | native, worker, api | Bridge detection and bridge extrusion roles |
| `concave-hole` | `tests/data/cube_with_concave_hole.obj` | native, worker, api | Polygon topology and hole preservation |
| `overhang-support` | `tests/data/overhang.obj` (`enable_support=1`) | native, worker, api | Support generation when explicitly enabled |
| `multipart` | `tests/data/two_hollow_squares.obj` | native, worker, api | Multiple disconnected regions in one mesh |
| `unicode-3mf` | `tests/data/test_3mf/Geräte/Büchse.3mf` | native, worker, api | 3MF archive import and a non-ASCII file name |
| `invalid-config` | `tests/data/20mm_cube.obj` (`layer_height=10` against a 0.4 mm nozzle) | native, worker, api | Stable rejection by `Print::validate()` instead of G-code |
| `output-limit` | `tests/data/20mm_cube.obj` (`limits.max_output_bytes=1`) | worker only | Stable rejection by the worker's own output-size ceiling |

Most cases compare a semantic G-code summary across lanes, as before. A case
that declares `expect` (`invalid-config`, `output-limit`) never produces
G-code on purpose: the worker and API lanes instead assert that the run
failed with exactly the declared `{"code": ..., "category": ...}`, taken
verbatim from the worker's own `result.json`/`error` document. `invalid-config`
also runs natively, where the same rejection is asserted through
`expected_exit_codes` and `allow_no_gcode` rather than `expect` (the native
CLI has no stable code/category document to compare).

`output-limit` is worker-lane only: the limit that trips it
(`limits.max_output_bytes`) is a field of the worker's own slice-manifest
payload, not something the public API lets a caller set — the API applies its
own server-configured ceiling instead, so there is no way to reproduce this
fixture through that lane without changing what the API accepts.

Multi-filament and cancellation fixtures are intentionally not part of this
manifest:

- The worker protocol accepts exactly one filament profile
  (`payload.profiles.filament` is a single path), so a multi-filament case
  cannot be expressed in the worker or API lanes without a protocol change.
- Cancellation is already covered by `scripts/test_web_worker_cancellation.py`,
  which signals a running worker process mid-slice — a capability this
  manifest-driven runner does not have.

Additional fixtures are added only when they protect a distinct contract such
as modifier volumes.

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

## Expanded matrix result

The expanded matrix was recorded on 2026-09-08 against commit
`62423275fbafd7338d35b252c1275d8aea96d255` with this gate's changes in the
working tree. Every case passed and repeated identically.

| Case | Layers | G-code bytes | Wall time range | Exit |
| --- | ---: | ---: | ---: | ---: |
| `cube-default` | 100 | 346,079 | 0.565-0.615 s | 0 |
| `bridge` | 40 | 188,531 | 0.520-0.634 s | 0 |
| `concave-hole` | 50 | 348,155 | 0.618-0.622 s | 0 |
| `overhang-support` | 39 | 375,644 | 0.512-0.515 s | 0 |
| `multipart` | 14 | 92,235 | 0.617-0.627 s | 0 |
| `unicode-3mf` | 150 | 351,381 | 0.565-0.614 s | 0 |
| `invalid-config` | — | — | 0.359-0.577 s | 205 |

The three original cases reproduce the byte counts recorded on 2026-09-01
exactly, which is what confirms that declaring each case's model once and
referring to it as `{model}` in the arguments changed no native invocation.
`overhang-support` emits `Support` and `Support interface` extrusion roles, so
the case measures support generation rather than merely enabling it.

The worker lane matched the native semantic summary for every comparison case
and produced the declared error for each `expect` case
(`slice_validation_failed`/`validation` at exit 5, and
`output_size_limit_exceeded`/`resource_limit` at exit 7, publishing no
artifact). The API lane matched on every case it runs.

Run these against the recorded native run with:

```powershell
$env:ORCA_WEB_GIT_COMMIT = (git rev-parse HEAD).Trim()
$env:ORCA_WEB_GIT_DIRTY = if (git status --porcelain) { "true" } else { "false" }
docker compose -f docker/web/compose.yml run --rm baseline
docker compose -f docker/web/compose.yml run --rm worker-baseline
docker compose -f docker/web/compose.yml run --rm api-baseline
```

Injecting the commit and dirty flag is not optional in practice: without them
the runner falls back to `git status --porcelain`, which stats the whole
bind-mounted worktree from inside the container and can stall the run for many
minutes before the first case starts.

## Headless parity result

On 2026-09-02, `orca-slicer-worker` loaded the same resolved machine, process,
and filament profiles and reproduced every recorded semantic field for
`cube-default`, `bridge`, and `concave-hole`. The comparison covers layer and Z
ranges, tools, extrusion roles and totals, temperatures, command counts, XY
bounds, and motion distance. Run it against the latest native record with:

```powershell
docker compose -f docker/web/compose.yml run --rm worker-baseline
```

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
