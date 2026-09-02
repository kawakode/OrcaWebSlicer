# Native slicing compatibility baseline

Status: Initial native matrix complete; expansion cases remain tracked below

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
