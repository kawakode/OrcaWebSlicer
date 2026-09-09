# Minimum API

Status: Active contract for G5

`web/api/` is the FastAPI service chosen in [ADR 0002](adr/0002-web-stack.md).
It is the only long-lived process in the web tier, and it deliberately does very
little: it validates requests, stores uploaded bytes, resolves bundled profiles,
and drives one disposable worker per job through the modules documented in
[the executor contract](executor.md).

The process never links `libslic3r` and never parses model geometry. An upload
is streamed to disk after an extension and size check; the first thing to read
its contents is a worker process inside its own job directory.

## Surface

Every route is under `/api/v1`. The generated OpenAPI document is served at
`/openapi.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness plus the protocol and API versions |
| `GET` | `/profiles` | Bundled machine, process, and filament profiles |
| `GET` | `/settings` | The engine's own definition of every curated setting |
| `POST` | `/uploads` | Store one STL, OBJ, or 3MF model |
| `POST` | `/uploads/{upload_id}/scene` | Start (or return) the inspect job for one upload |
| `POST` | `/jobs` | Submit one slice request |
| `GET` | `/jobs` | List the jobs this process still holds |
| `GET` | `/jobs/{job_id}` | Read state, progress, warnings, and artifacts |
| `POST` | `/jobs/{job_id}/cancel` | Cancel a queued or running job |
| `POST` | `/jobs/{job_id}/retry` | Rerun the same inputs under a new job ID |
| `GET` | `/jobs/{job_id}/artifacts/{name}` | Download `gcode`, `result`, or `preview` |
| `GET` | `/jobs/{job_id}/preview` | The layer preview index |
| `GET` | `/jobs/{job_id}/preview/layers/{n}` | One layer's toolpaths |
| `GET` | `/scenes/{job_id}` | The scene index: bed shape and per-object geometry |
| `GET` | `/scenes/{job_id}/objects/{n}` | One object's triangle data |

Request bodies are validated against the published schema before any handler
runs, and unknown fields are refused rather than ignored. A slice request names
an upload, a machine and process profile, one or more filament profiles,
optional curated setting overrides, an optional 1-based `plate_index`, and
optional explicit object placement:

```json
{
  "upload_id": "3f2a...",
  "machine_profile": "Anycubic/machine/Anycubic Kobra 0.4 nozzle",
  "process_profile": "Anycubic/process/0.20mm Standard @Anycubic Kobra",
  "filament_profiles": [
    "Anycubic/filament/Anycubic Generic PLA",
    "Anycubic/filament/Anycubic Generic PETG"
  ],
  "settings": {"layer_height": "0.28"},
  "plate_index": 1,
  "objects": [
    {"source_object": 0, "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 5, 0, 1], "filament": 1},
    {"source_object": 1, "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -10, 5, 0, 1], "filament": 2}
  ]
}
```

`filament_profiles` names 1-16 filament profiles, one per filament slot, in
the order the worker should see them; the same profile id may repeat (two
spools of the same material). `filament_profile` (a single string) is kept as
the single-filament spelling and is equivalent to a one-element
`filament_profiles`. A request naming both is refused with `invalid_profiles`,
mirroring the worker's own manifest validation, rather than one silently
winning. The API always forwards the array form to the worker's manifest
(`profiles.filaments`), even for a single filament, so it only ever has to
speak one spelling; a one-element array takes the worker's unchanged
single-filament code path. The API never sends filament colour, filament
mapping, or the flush-volume matrix — the worker synthesizes those itself.

`objects` places the plate explicitly: each entry names a `source_object`
index into a scene's `objects` array (below), a 16-number column-major
transform in millimetres, and an optional 1-based `filament` assigning that
object to one of the request's `filament_profiles` slots. Omitting `filament`
(or sending `0`) leaves the object's own assignment alone, matching the
worker's meaning for the field; a value outside `1..len(filament_profiles)` is
refused with `invalid_object_filament`. A `source_object` may repeat to place
a duplicate, and at most 64 entries are accepted. Omitting `objects` keeps the
worker's existing default placement.

A failure is always the same shape, and always carries a stable code from the
layer that produced it:

```json
{"error": {"code": "incompatible_profile", "message": "..."}, "correlation_id": "..."}
```

## Job lifecycle

A job is `queued`, then `running`, then exactly one of `succeeded`, `failed`, or
`canceled`. `progress` mirrors the worker's public stages as its events arrive,
so a client polling `GET /jobs/{job_id}` sees the same stage and percentage the
worker reported.

Cancellation is accepted while a job is queued or running. A queued job is
canceled before a worker exists; a running job is canceled through the
executor, which escalates from `SIGTERM` to `SIGKILL` after its grace period.
Cancelling a finished job is `job_not_cancelable`.

Retry is the opposite: it requires a finished job. It resubmits the same
immutable inputs under a new job ID and records `retry_of`, so the original job
and its report stay intact.

Every terminal path is contained. A worker crash, a malformed event stream, a
timeout, a limit violation, and a missing artifact are all one job's failure,
recorded with the executor's own error code, and the API process keeps serving.

## Artifacts

Artifacts are named, not addressed by path. A client asks for `gcode` or
`result`, and the API returns the file it validated inside that job directory;
the worker's relative paths never reach a URL.

- `result` is available once the executor validated a terminal `result.json`,
  whatever the outcome, because a failed run's report is the useful part.
- `gcode` is available only for a `succeeded` job that declared it.
- `preview` is the layer preview index, available on the same terms as the
  G-code. Its binary companion is never downloaded whole: it is read one layer
  at a time through `/preview/layers/{n}`, described in
  [preview-format.md](preview-format.md).

Anything the executor did not validate is deleted rather than served. A
cancellation, crash, timeout, or output-limit failure removes the whole job
directory, so a partially written G-code file is never downloadable.

## Profiles

`GET /profiles` reads the bundled vendor indexes under `resources/profiles` and
lists only user-selectable profiles. Passing `?printer=<machine profile id>`
narrows the process and filament lists to those the printer accepts. Every
entry carries its `inherits_chain`: the flattened inheritance chain, root first.

Compatibility follows the desktop's own order. A declared `compatible_printers`
list wins. A profile that declares only a `compatible_printers_condition` is
resolved once at startup by `orca-slicer-worker --evaluate-compatibility`, which
runs the engine's placeholder parser over each printer's resolved configuration;
the API never interprets an expression itself. A profile with neither is offered
for every printer, and so is one whose condition the engine could not parse,
which is what the desktop does with a broken expression.

A selected chain is flattened once, at submission, into job-local JSON files:
one machine, one process, and one filament file per named filament slot
(`filament-0.json`, `filament-1.json`, ...). The worker therefore never reads
the bundled profile tree and never resolves a path outside its own job
directory.

## Settings

`GET /settings` serves the document `orca-slicer-worker --export-settings-catalog`
produces, which serializes `PrintConfigDef` for the curated MVP settings. It is
read once at startup and carries, per setting, the engine's type, scope, unit,
range, enum values and labels, serialized default, mode, and the setting that
gates it:

```json
{
  "catalog_version": 1,
  "engine_version": "2.3.1",
  "groups": [{"id": "quality", "label": "Quality"}],
  "settings": [
    {"key": "layer_height", "group": "quality", "scope": "process", "type": "float",
     "vector": false, "unit": "mm", "min": 0.001, "max": 100, "default": "0.2", "…": "…"}
  ]
}
```

The browser generates its whole overrides form from this document, so no type,
range, enum, or default is restated in TypeScript. The API uses the same
document to refuse an out-of-range or misspelled override with
`invalid_setting_value` or `unknown_setting` before a worker is spawned; the
worker still validates everything it is given, so this check only makes the
common mistakes cheap and legible.

A deployment whose worker cannot answer keeps serving. `GET /settings` then
returns `settings_catalog_unavailable` (503), the browser hides the overrides
form, and jobs run with the selected profiles unchanged.

## Job reports

`GET /jobs/{job_id}` carries what was actually sliced alongside the state:

- `profiles` names the machine and process entries, plus a `filaments` array
  (one entry per named filament slot, in order — always a list, even for a
  single-filament request), each with the `inherits_chain` it flattens. There
  is no singular `filament` key; a redundant duplicate of `filaments[0]` would
  only drift.
- `overrides` lists every submitted override paired with the engine's label,
  unit, and scope for that setting.

Both are recorded when the job is accepted, so a finished job still explains
itself after its directory is reclaimed.

## Correlation IDs

Every request carries a correlation ID: the supplied `X-Correlation-Id` when it
is well formed, otherwise a generated one. It is echoed on every response
including errors, stored on the job the request created, and written to the log
lines for that job, so one identifier links an API request to its worker run.

## Configuration

The API adds four settings to the worker and executor variables listed in
[executor.md](executor.md), which it also honours.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `ORCA_WEB_STATE_ROOT` | `<repo>/build/web-state` | Uploads and job directories |
| `ORCA_WEB_WORKER` | `<repo>/build-worker/src/Release/orca-slicer-worker` | Worker executable |
| `ORCA_WEB_PROFILE_VENDORS` | `Anycubic` | Comma-separated bundled vendors |
| `ORCA_WEB_MAX_CONCURRENT_JOBS` | 2 | Workers running at once |
| `ORCA_WEB_FRONTEND_DIST` | `<repo>/web/frontend/dist` | Built browser screen |

When the frontend build exists it is mounted after every API route, so the
[browser screen](frontend.md) is served from this same origin. When it does not,
the API is a bare JSON service.

Uploads and job directories are reclaimed by the same retention window as the
job-directory sweep, which runs at startup and before each submission.

## Running it

```powershell
docker compose -f docker/web/compose.yml run --rm worker-build
docker compose -f docker/web/compose.yml run --rm frontend-build
docker compose -f docker/web/compose.yml up api
docker compose -f docker/web/compose.yml run --rm executor-smoke
docker compose -f docker/web/compose.yml run --rm api-baseline
docker compose -f docker/web/compose.yml run --rm placement-check
```

`api` serves on `http://localhost:8000` and reads the worker built by
`worker-build`. `executor-smoke` runs the whole `tests/web` suite, which covers
the profile catalog, the job service against fake workers, and the HTTP surface.
`api-baseline` slices the recorded baseline fixtures through the API and
compares the downloaded G-code with the native baseline run. `placement-check`
inspects a fixture, slices it at explicit placements, and checks that the
G-code lands where the transform said it would — the write side of
[scene-format.md](scene-format.md), verified end to end.

## Still deferred

Job state and artifacts live on the job-directory filesystem behind the
`JobService` interface. Database, queue, and object-store products stay
unselected until local throughput and artifact sizes are measured. Retention
tied to completion and its deletion audit, authentication, quotas, and rate
limiting are G6 work.
