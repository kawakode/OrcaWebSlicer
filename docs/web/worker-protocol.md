# Worker protocol version 1

Status: Active contract for G3

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
      "settings": {"layer_height": "0.2"}
    }
  }
}
```

Protocol and operation versions are independent. An unsupported
`protocol_version` produces `unsupported_protocol_version`; an unsupported
slice payload version produces `unsupported_operation_version`. Unknown fields
are allowed at every object level. Required fields cannot be removed or change
type within a version.

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

## Artifact publication

The worker canonicalizes the job root once. Input, profile, and output paths
must be relative paths contained by that root; absolute paths, parent traversal,
and symlinks that resolve outside the root are rejected.

G-code is first written to a hidden, job-specific `.partial` file beside its
requested target. The worker validates that temporary file and atomically
renames it to the requested path without replacing an existing target. Normal
validation, slicing, and export failures remove the temporary file. The
executor remains responsible for cleaning abandoned job directories after a
forced termination or process crash. `result.json` uses the same write-then-
rename publication rule.

## Terminal result

`result.json` contains `protocol_version`, `job_id`, terminal `outcome`, all
warnings, `timing.duration_ms`, `timing.cpu_time_ms`,
`resource_usage.peak_memory_bytes`, artifact metadata, and either a categorized
error or `null`. Only a successful result may contain downloadable artifacts.
Golden examples for succeeded, failed, and canceled outcomes live under
`tests/data/web-worker/contracts/`.
