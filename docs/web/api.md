# Minimum API

Status: Active contract; extended for G5

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
| `GET` | `/profiles/effective` | A selected chain's curated values and bed |
| `GET` | `/settings` | The engine's catalog of overridable settings |
| `POST` | `/uploads` | Store one STL, OBJ, or 3MF model and inspect it |
| `POST` | `/jobs` | Submit one slice request |
| `POST` | `/projects` | Save a plate as a 3MF project |
| `GET` | `/jobs` | List the jobs this process still holds |
| `GET` | `/jobs/{job_id}` | Read state, progress, warnings, and artifacts |
| `POST` | `/jobs/{job_id}/cancel` | Cancel a queued or running job |
| `POST` | `/jobs/{job_id}/retry` | Rerun the same inputs under a new job ID |
| `GET` | `/jobs/{job_id}/artifacts/{name}` | Download one named artifact |
| `GET` | `/jobs/{job_id}/preview/layers` | Stream a range of preview layers |

Request bodies are validated against the published schema before any handler
runs, and unknown fields are refused rather than ignored. A slice request names
its geometry, one profile of each kind, optional curated setting overrides, and
an optional 1-based `plate_index`. The geometry is either one `upload_id`,
which the worker arranges as in G4:

```json
{
  "upload_id": "3f2a...",
  "machine_profile": "Anycubic/machine/Anycubic Kobra 0.4 nozzle",
  "process_profile": "Anycubic/process/0.20mm Standard @Anycubic Kobra",
  "filament_profile": "Anycubic/filament/Anycubic Generic PLA",
  "settings": {"layer_height": "0.28"},
  "plate_index": 1
}
```

or the `objects` of a plate composed in the [plater](plater.md), each naming an
upload, an object index inside it, and one row-major 4x4 transform in
millimetres per instance. The worker applies those transforms unchanged:

```json
{
  "objects": [
    {"upload_id": "3f2a...", "object": 0, "instances": [
      {"transform": [1,0,0,60, 0,1,0,70, 0,0,1,0, 0,0,0,1]},
      {"transform": [1,0,0,90, 0,1,0,70, 0,0,1,0, 0,0,0,1]}
    ]}
  ],
  "machine_profile": "Anycubic/machine/Anycubic Kobra 0.4 nozzle",
  "process_profile": "Anycubic/process/0.20mm Standard @Anycubic Kobra",
  "filament_profile": "Anycubic/filament/Anycubic Generic PLA"
}
```

Each upload a plate uses is staged into the job once, whatever the number of
objects that reference it. A plate holds at most 256 objects from 16 uploads.

A slice request may add `"thumbnails": true` to embed rendered thumbnails in
the G-code; it is off by default. `POST /projects` takes the same body without
`thumbnails` and starts a job that saves the plate as a desktop-compatible 3MF
instead of slicing it; see [projects](projects.md).

Overrides are checked against the engine's [settings catalog](#settings) before
any worker starts: an unknown key is `unknown_setting`, and a value of the wrong
type, outside its range, or not among its choices is `invalid_setting_value`,
both 422. The worker still validates everything it receives.

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

## Inspections

Every upload is inspected as soon as it is stored. `POST /uploads` returns the
upload record together with `inspection`, an ordinary job whose `operation` is
`inspect`. When it succeeds it publishes the `mesh` and `inspection` artifacts
the plater draws. An unreadable model fails that job with the worker's own
`input` error, such as `model_load_failed`, and is never offered to a slice.
Inspections share the worker pool, limits, and retention of slices.

## Artifacts

Artifacts are named, not addressed by path. A client asks for one by name, and
the API returns the file it validated inside that job directory; the worker's
relative paths never reach a URL.

- `result` is available once the executor validated a terminal `result.json`,
  whatever the outcome, because a failed run's report is the useful part.
- `gcode`, `preview`, and `preview_index` are published by a succeeded slice.
- `mesh` and `inspection` are published by a succeeded inspection.
- `project` is published by a succeeded project export, as `model/3mf`.

The layer preview is also served in pages: `GET
/jobs/{job_id}/preview/layers?start=a&end=b` streams layers `a` through `b`
without loading the file, as described in [the preview contract](preview.md).

## Settings

`GET /settings` serves the catalog the worker exports with
`--export-settings-catalog`, read once at startup. Each curated setting carries
its group, the profile kind that owns it, its type, unit, range, choices,
default, and the setting that enables it, all taken from `PrintConfig.cpp`. The
browser builds its settings form from this document alone.

`GET /profiles/effective?machine=...&process=...&filament=...` resolves a
selected chain and returns each curated setting's flattened value, the three
profiles with their `inherits_chain`, and the printer's `printable_area` and
`printable_height`. A blank override in the browser keeps exactly the value
shown here.

Every job records `profiles`, the full inheritance chain of each selected
profile, root first, next to the overrides it applied, so a report names every
profile that contributed to the result.

Anything the executor did not validate is deleted rather than served. A
cancellation, crash, timeout, or output-limit failure removes the whole job
directory, so a partially written G-code file is never downloadable.

## Profiles

`GET /profiles` reads the bundled vendor indexes under `resources/profiles` and
lists only user-selectable profiles. Passing `?printer=<machine profile id>`
narrows the process and filament lists to those the printer accepts.

A declared `compatible_printers` list wins, as on the desktop. A profile that
only declares a `compatible_printers_condition` is evaluated once at startup by
the worker's `--evaluate-compatibility`, which runs the engine's own placeholder
parser against every bundled printer; the API never interprets the expression
language. A condition the engine cannot parse is treated as compatible, again
as on the desktop. A profile that declares no restriction is offered for every
printer.

A selected chain is flattened once, at submission, into three job-local JSON
files. The worker therefore never reads the bundled profile tree and never
resolves a path outside its own job directory.

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
```

`api` serves on `http://localhost:8000` and reads the worker built by
`worker-build`. `executor-smoke` runs the whole `tests/web` suite, which covers
the profile catalog, the job service against fake workers, and the HTTP surface.
`api-baseline` slices the recorded baseline fixtures through the API and
compares the downloaded G-code with the native baseline run.

## Still deferred

Job state and artifacts live on the job-directory filesystem behind the
`JobService` interface. Database, queue, and object-store products stay
unselected until local throughput and artifact sizes are measured. Retention
tied to completion and its deletion audit, authentication, quotas, and rate
limiting are G6 work.
