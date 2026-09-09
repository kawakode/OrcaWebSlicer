# Worker protocol version 1

Status: Active contract

The worker reads one manifest from a file, emits newline-delimited JSON events
on stdout, writes diagnostics and logs only to stderr, and publishes a terminal
`result.json` in the job directory. Consumers must ignore unknown object fields
so compatible records can gain optional data without a protocol version bump.

## Commands

| Command | Purpose |
| --- | --- |
| `--version` | The worker and protocol versions |
| `--validate-manifest <path>` | Check one envelope without running it |
| `--slice-manifest <path>` | Run one slice job in the manifest's directory |
| `--inspect-manifest <path>` | Publish one model's geometry and bed as a scene |
| `--export-settings-catalog` | Serialize `PrintConfigDef` for the curated settings |
| `--evaluate-compatibility <path>` | Resolve `compatible_printers_condition` expressions |

The first four carry untrusted job input and are what a deployment isolates.
The last two are engine metadata: they take no model, produce no artifact, and
are read once by the API at startup from the trusted worker executable. They are
described in [the API contract](api.md); both answer on stdout, report a stable
error document on stderr, and use the exit codes below.

## Request envelope

The stable envelope identifies the protocol, job, and versioned operation. All
operation-specific fields live inside `operation.payload`.

```json
{
  "protocol_version": 1,
  "job_id": "job-2026.09_02",
  "operation": {
    "name": "slice",
    "version": 1,
    "payload": {
      "input_model": "input/model.stl",
      "output_gcode": "output/model.gcode",
      "output_preview": "output/preview.json",
      "profiles": {
        "machine": "profiles/machine.json",
        "process": "profiles/process.json",
        "filament": "profiles/filament.json"
      },
      "settings": {"layer_height": "0.2"},
      "plate_index": 1
    }
  }
}
```

Protocol and operation versions are independent. An unsupported
`protocol_version` produces `unsupported_protocol_version`; an unsupported
slice payload version produces `unsupported_operation_version`. Unknown fields
are allowed at every object level. Required fields cannot be removed or change
type within a version.

The manifest file is limited to 1 MiB, and a slice payload may contain at most
256 serialized setting overrides. The worker rejects model formats other than
STL, OBJ, and 3MF before invoking an importer.

`plate_index` is the optional 1-based plate to slice from a project archive. It
defaults to 1 and is ignored for meshes, which always describe one plate.

`profiles.filament` names one filament, exactly as shown above. A request that
prints in more than one filament instead names `profiles.filaments`, an array
of 1-16 safe relative JSON paths, one per filament slot in order:

```json
"profiles": {
  "machine": "profiles/machine.json",
  "process": "profiles/process.json",
  "filaments": ["profiles/filament-0.json", "profiles/filament-1.json"]
}
```

Naming both `filament` and `filaments` is `invalid_profiles`; an unsafe or
non-JSON entry inside `filaments`, or a `filaments` array with zero or more
than 16 entries, is `invalid_profile_path` / `invalid_filaments` respectively,
the same as an invalid `filament`. Every entry is subject to the same
safe-relative-path and `.json`-extension checks as every other profile path.

`objects` is optional. When present, each entry names a `source_object` index
into the scene the `inspect` operation published and a 16-number column-major
millimetre transform, and the worker slices exactly those placements instead of
arranging; a repeated `source_object` is a duplicate. At most 64 entries are
accepted. See [scene-format.md](scene-format.md), which also documents the
`inspect` operation, its `scene.json` / `scene.bin` artifacts, and
`ORCA_WEB_MAX_SCENE_BYTES`.

Each `objects` entry may also carry a 1-based `filament`, naming which
filament slot (from `profiles.filament`/`profiles.filaments`) that placement
prints in:

```json
{"source_object": 0, "transform": [ /* 16 numbers */ ], "filament": 2}
```

Omitting `filament`, or giving it as `0`, leaves the object's own extruder
assignment alone. A value greater than the number of filaments the request
names is `invalid_object_filament`, checked before the model is touched, the
same as an out-of-range `source_object` is `invalid_source_object`.

A request that names more than one filament does not need to supply
`filament_colour`, `filament_map`, or `flush_volumes_matrix` either: the
worker synthesizes all three the same way the desktop's preset pipeline does
before any multi-filament print, so a plain request with only `machine`,
`process`, and `filaments` slices correctly. `filament_colour` takes each
slot's colour from its own filament profile where declared, falling back to
the engine default otherwise (two slots sharing a colour is a legitimate
configuration, not an error). `filament_map` assigns every filament to the
printer's first nozzle, which is the only mapping a single-nozzle printer
has; a *multi-filament* request against a machine profile with more than one
nozzle (`nozzle_diameter` declaring more than one entry, e.g. some bundled
BBL and WEMAKE3D printers) is rejected as
`multi_nozzle_filament_map_unsupported` in the `profile` category, since
deciding which nozzle each filament actually feeds is not implemented yet. A
single-filament request is unaffected by this limit regardless of nozzle
count.

Multi-filament requests also get the prime tower positioned onto the bed
automatically. The desktop places it as part of a project's per-plate layout
(`PartPlate`, GUI-only); the worker has no such concept, so a request that
doesn't set `wipe_tower_x`/`wipe_tower_y` gets the engine's own defaults
(15 mm, 220 mm), which sit outside or at the very edge of most bundled
printers' beds and would otherwise make every multi-filament slice fail
validation. When the prime tower is enabled and its configured footprint
(including its brim) does not fit the bed, the worker moves it just inside
the bed's near corner and reports a warning naming the new position. A
footprint that already fits -- including one the request placed deliberately
-- is left untouched, and single-filament requests are never affected.

`output_preview` is optional. When present it names the JSON index of the
[layer preview](preview-format.md); the binary companion is the same path with a
`.bin` extension, so one field names both files. A preview is auxiliary: if it
would exceed its size limit, or the G-code has no extrusion, the job still
succeeds with its G-code and carries a warning instead.

The optional `operation.payload.limits` object accepts positive unsigned
integers for `max_input_bytes`, `max_triangles`, `max_wall_time_ms`,
`max_memory_bytes`, `max_output_bytes`, `max_extracted_bytes`, and
`max_preview_bytes`. A request may
tighten, but never raise, the server-configured ceiling. The worker applies these
defaults when a field is omitted:

| Environment variable | Default | Stable error code |
| --- | ---: | --- |
| `ORCA_WEB_MAX_INPUT_BYTES` | 250 MiB | `input_size_limit_exceeded` |
| `ORCA_WEB_MAX_TRIANGLES` | 1,000,000 | `triangle_limit_exceeded` |
| `ORCA_WEB_MAX_WALL_TIME_MS` | 300,000 | `wall_time_limit_exceeded` |
| `ORCA_WEB_MAX_MEMORY_BYTES` | 4 GiB | `memory_limit_exceeded` |
| `ORCA_WEB_MAX_OUTPUT_BYTES` | 1 GiB | `output_size_limit_exceeded` |
| `ORCA_WEB_MAX_EXTRACTED_BYTES` | 1 GiB | `archive_extracted_size_limit_exceeded` |
| `ORCA_WEB_MAX_PREVIEW_BYTES` | 256 MiB | warning, not a failure |

Input size is checked before import and triangle count immediately after import.
Wall time and memory are checked between stages and monitored during slicing and
G-code export. Output size is monitored on the temporary G-code and checked
again before publication. Extracted content is checked against a project
archive's central directory before the importer opens it. A limit failure
publishes no G-code and exits with code 7.

These checks provide useful terminal results during graceful shutdown. The
executor must also impose hard process, memory, CPU, and wall-time ceilings and
forcibly terminate a worker that does not stop within its grace period.

The implemented [isolated executor](executor.md) applies those hard Linux
limits, bounds event and diagnostic capture, and validates the terminal worker
contract before returning it to a future API.

## Event transport

Each stdout line is one complete compact JSON object. Every event contains
`protocol_version`, `job_id`, a zero-based monotonically increasing `sequence`,
and `type`. State values are `accepted`, `running`, `succeeded`, `failed`, and
`canceled`. The last event for a valid job is exactly one terminal state.

Event types and their typed records are:

- `state`: `state`
- `progress`: `progress.stage`, `progress.percent`, and `progress.message`
- `warning`: `warning.code` and `warning.message`
- `error`: `error.category`, `error.code`, and `error.message`
- `artifact`: `artifact.kind`, relative `artifact.path`, `size_bytes`, and
  lowercase hexadecimal `sha256`

Progress percentages are integers from 0 through 100 and never decrease.
Public stages currently include `input`, `configuration`, `arrangement`,
`slicing`, `export`, and `finalize`; they intentionally do not expose internal
class or print-step names.

## Errors and exit codes

Every error has one stable category: `request`, `input`, `profile`,
`validation`, `slicing`, `cancellation`, `resource_limit`, or `internal`.
Specific codes may be added within those categories.

| Exit | Meaning |
| ---: | --- |
| 0 | Succeeded |
| 2 | Invalid command-line usage |
| 3 | Manifest could not be read or exceeded its size limit |
| 4 | Manifest envelope or operation payload was invalid |
| 5 | Slicing or artifact generation failed |
| 6 | Job was canceled |
| 7 | A configured resource limit was exceeded |
| 8 | Worker setup or result publication failed |

After accepting a slice job, the worker treats `SIGINT` and `SIGTERM` on POSIX
and console close, Ctrl+C, and Ctrl+Break events on Windows as cancellation
requests. Cancellation is propagated through the engine's existing checks. A
gracefully canceled job emits an error with category `cancellation` and code
`job_canceled`, publishes a canceled `result.json`, emits a final `canceled`
state, and exits with code 6. It does not publish G-code.

Envelope failures cannot safely identify a job and therefore write diagnostics
to stderr without job events or a result. Once an envelope is accepted, every
terminal path emits a terminal state and attempts to publish `result.json`.

## Project archive input

A `.3mf` input is imported through the core project importer, which needs no
desktop plate list, and never through the desktop's OpenGL thumbnail or
auxiliary-file paths.

Before the importer opens the archive, its central directory is checked entry by
entry. An entry is rejected as `archive_entry_unsafe` when it is a symbolic link
or when its name is absolute, carries a drive letter, uses backslashes, contains
a `.` or `..` component, or contains control characters. An archive that is not
a readable ZIP container is rejected as `archive_unreadable`. Declared sizes are
bounded by `archive_entry_count_limit_exceeded`,
`archive_entry_size_limit_exceeded`,
`archive_compression_ratio_limit_exceeded`, and
`archive_extracted_size_limit_exceeded`, all in the `resource_limit` category.

Only the first plate is in scope. A project holding more than one plate is
rejected with `multi_plate_project_unsupported`, and a `plate_index` beyond the
project's plates is rejected with `plate_index_out_of_range`, because slicing a
later plate requires the desktop plate-list layout that offsets every plate on
one shared coordinate system. A plate with no printable object is rejected with
`empty_plate_selection`. All four are `input` failures.

When the archive declares a plate, the importer keeps its stored object
transforms: the objects are never re-arranged, and only objects entirely below
the bed are lifted onto it. A plain 3MF that declares no plate is a mesh
container whose coordinates carry no placement, so it is arranged like a loose
mesh instead.

The configuration chain is the engine defaults, then the archive's embedded
project and plate configuration including its filament mapping, then any
profiles resolved by the request, then the request's curated setting overrides.

## Artifact publication

The worker canonicalizes the job root once. Input, profile, and output paths
must be relative paths contained by that root; absolute paths, parent traversal,
and symlinks that resolve outside the root are rejected.

G-code is first written to a hidden, job-specific `.partial` file beside its
requested target. The worker validates that temporary file and atomically
renames it to the requested path without replacing an existing target. Normal
validation, slicing, export, cancellation, and output-limit failures remove the
temporary file. Before rerunning an accepted job ID, the worker removes that
job's abandoned `.partial` files from earlier crashes. The executor remains
responsible for deleting the complete job directory after a forced termination
or process crash. `result.json` and both layer-preview files use the same
write-then-rename publication rule.

Each published artifact declares a `kind`: `gcode`, and, when a preview was
requested and produced, `preview` for its JSON index and `preview_data` for the
binary blob it indexes. A cancellation or a hashing failure discards every
artifact the run committed, not only the G-code, so an abandoned job leaves none
of them behind.

## Terminal result

`result.json` contains `protocol_version`, `job_id`, terminal `outcome`, all
warnings, `timing.duration_ms`, `timing.cpu_time_ms`,
`resource_usage.peak_memory_bytes`, artifact metadata, and either a categorized
error or `null`. Only a successful result may contain downloadable artifacts.
Golden examples for succeeded, failed, and canceled outcomes live under
`tests/data/web-worker/contracts/`.
