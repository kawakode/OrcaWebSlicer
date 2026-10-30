# Worker protocol version 1

Status: Active contract; G5 added plate composition, inspection, and preview;
phase 3b added object edits

The worker reads one manifest from a file, emits newline-delimited JSON events
on stdout, writes diagnostics and logs only to stderr, and publishes a terminal
`result.json` in the job directory. Consumers must ignore unknown object fields
so compatible records can gain optional data without a protocol version bump.

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

The optional `operation.payload.limits` object accepts positive unsigned
integers for `max_input_bytes`, `max_triangles`, `max_wall_time_ms`,
`max_memory_bytes`, `max_output_bytes`, and `max_extracted_bytes`. A request may
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

## Plate composition

A slice names its geometry in one of two ways, never both
(`ambiguous_plate`):

- `input_model` slices one file. Loose meshes and plate-less archives are
  arranged by the engine, exactly as in G4.
- `objects` slices a plate the browser composed. Each entry names a `model`
  file, the 0-based `object` inside it (default 0), and 1 to 64 `instances`,
  each a `transform`: a row-major 4x4 matrix in millimetres.

```json
"objects": [
  {"model": "input/model-0.stl", "object": 0, "instances": [
    {"transform": [1,0,0,60, 0,1,0,70, 0,0,1,0, 0,0,0,1]}
  ]}
]
```

A plate holds at most 256 objects from at most 16 model files. A transform must
be finite and affine with an invertible linear part and a translation within
100 m (`invalid_instance_transform`); an object index the file does not contain
is `unknown_model_object`. The matrix replaces the instance placement the file
declared and is applied to the object's own coordinates, the same coordinates
an inspection exports. The worker neither arranges nor centers such a plate:
it only lifts objects that are entirely below the bed, and refuses any instance
whose footprint leaves the printer's `printable_area` or whose top exceeds
`printable_height` with the `validation` error `object_outside_plate`. When
the plate draws on a project archive, the first archive listed contributes its
embedded configuration.

An object may also carry its own `settings`, and `layer_ranges` that apply
settings between two heights measured from the object's bottom, exactly as the
desktop's per-object settings and height range modifiers do:

```json
{"model": "input/model-0.stl", "instances": [...],
 "settings": {"wall_loops": "5"},
 "layer_ranges": [{"min_z": 5, "max_z": 10, "settings": {"layer_height": "0.1"}}]}
```

Values use the same serialized string form as the request's `settings`, at most
256 per object or range. An object accepts the engine's object and region
options (`PrintObjectConfig` and `PrintRegionConfig`), a range the region options
and `layer_height`, the same sets the desktop's object list offers; any other
key is the `validation` error `setting_not_per_object`, naming the setting. An
object holds at most 32 ranges, each with `0 <= min_z < max_z` and at least one
setting, and its ranges may not overlap (`invalid_layer_ranges`). A range that
does not set `layer_height` keeps the object's. The overrides are applied on top
of any configuration the object brought from its file, and affect that object
only.

`output_preview` and `output_preview_index` optionally ask for a layer preview
of the produced G-code, always together. The format is documented in
[the preview contract](preview.md). The preview is published only alongside its
G-code, and withdrawn with it if either fails.

## Inspection operation

`operation.name` may be `inspect`. Its payload names one `input_model` and two
outputs, `output_mesh` (`.bin`) and `output_inspection` (`.json`); profiles and
settings are not read. The model is loaded with the same importer, size,
triangle, and archive limits as a slice, and its display geometry is published
as artifacts of kind `mesh` and `inspection`, described in
[the plater contract](plater.md). An inspection is how the browser sees a model
without the API ever parsing one.

## Edit operation

`operation.name` may be `edit`. It applies one object tool to one object of
one `input_model` and publishes the result as new models; profiles and
settings are not read, and the input limits are those of an inspection.

```json
"payload": {
  "input_model": "input/model.stl",
  "object": 0,
  "transform": [1,0,0,100, 0,1,0,100, 0,0,1,0, 0,0,0,1],
  "tool": "cut",
  "cut": {"point": [110, 110, 10], "normal": [0, 0, 1], "keep": "both",
          "flip_upper": false, "place_on_cut": false},
  "output_models": ["output/part-0.stl", "output/part-1.stl"],
  "output_report": "output/edit.json"
}
```

`transform` is the object's placement on the plate, validated like a plate
instance (`invalid_instance_transform`). The object is edited where it stands,
and every produced model is a binary STL of its printable parts with that
placement baked in, so its vertices are plate coordinates: drawn with an
identity transform, a part sits exactly where it was. Modifiers and negative
volumes do not survive an edit.

`tool` is `cut` or `repair` (`invalid_edit_tool`).

- A cut is a plane given by a `point` and a `normal` in plate millimetres; the
  normal points at the upper part (`invalid_cut_plane`). `keep` is `both`,
  `upper`, or `lower`. `flip_upper` turns the upper part upside down, and
  `place_on_cut` lays each part on its cut face, which only matters for a tilted
  plane. The cut goes through the engine's `Cut`, the code behind the desktop
  cut tool, and caps every part. A plane that does not cross the object is the
  `validation` error `cut_plane_misses_object`.
- A repair welds coincident vertices, drops degenerate faces, and runs admesh's
  full repair: nearby edges are joined, unconnected facets removed, holes
  filled, and normals made consistent. Import already applies the same repair
  except hole filling, which the desktop leaves to the slicer.

`output_models` lists one or two `.stl` paths, used in order. The models are
published with kind `model`, then `output_report` with kind `edit_report`:

```json
{"format": "orca-web-edit", "version": 1, "tool": "cut", "parts": [
  {"side": "upper", "name": "cube (upper)", "model": "output/part-0.stl",
   "triangle_count": 12, "bounding_box": {"min": [100,100,10], "max": [120,120,20]}}
]}
```

`side` is `upper` or `lower` for a cut and `whole` for a repair. A repair's
report adds `repair`: `open_edges_before`, `open_edges_after`,
`merged_vertices`, `degenerate_facets`, `edges_fixed`, `facets_removed`,
`facets_added`, `facets_reversed`, and `backwards_edges`.

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
`slicing`, `edit`, `export`, and `finalize`; they intentionally do not expose internal
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
profiles resolved by the request, then the request's setting overrides. A plate
object's own settings and height ranges then apply to that object only.

## Artifact publication

The worker canonicalizes the job root once. Input, profile, and output paths
must be relative paths contained by that root; absolute paths, parent traversal,
and symlinks that resolve outside the root are rejected.

Every artifact, including the inspection and preview files, follows the same
rule. G-code is first written to a hidden, job-specific `.partial` file beside its
requested target. The worker validates that temporary file and atomically
renames it to the requested path without replacing an existing target. Normal
validation, slicing, export, cancellation, and output-limit failures remove the
temporary file. Before rerunning an accepted job ID, the worker removes that
job's abandoned `.partial` files from earlier crashes. The executor remains
responsible for deleting the complete job directory after a forced termination
or process crash. `result.json` uses the same write-then-rename publication
rule.

## Terminal result

`result.json` contains `protocol_version`, `job_id`, terminal `outcome`, all
warnings, `timing.duration_ms`, `timing.cpu_time_ms`,
`resource_usage.peak_memory_bytes`, artifact metadata, and either a categorized
error or `null`. Only a successful result may contain downloadable artifacts.
Golden examples for succeeded, failed, and canceled outcomes live under
`tests/data/web-worker/contracts/`.
