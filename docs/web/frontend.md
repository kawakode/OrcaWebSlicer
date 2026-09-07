# Browser screen

Status: Active contract for G5

`web/frontend/` is the React, TypeScript, and Vite application chosen in
[ADR 0002](adr/0002-web-stack.md).

## What the screen does

- Select one STL, OBJ, or 3MF file and see its name and size.
- Choose a bundled printer, then a process and filament narrowed to that
  printer by the [API](api.md).
- Edit the curated overrides, in a form generated from the engine's own setting
  definitions. Blank fields are not sent, so the profile's own value stands.
- Slice, Cancel, and Retry, each disabled when the job state makes it
  meaningless.
- Watch the worker's stage and percentage while the job runs, and read its
  warnings and its categorized error when it fails.
- Read the effective configuration: the profile chain each selection flattens
  and every override that displaced it.
- Step through the layer preview of the finished job.
- Download the G-code and the machine-readable job report.

Every failure the user sees is the API's stable code plus its message.
Framework schema rejections are reduced to the same shape, so the screen has one
error path rather than two.

## Generated settings

`SettingsForm` renders `GET /api/v1/settings` and knows nothing about any
individual setting. The control shape follows the engine's declared type — a
select for an enum or a boolean, a bounded number input for an integer or float,
a text field for anything that may carry a `%` — and the label, unit, tooltip,
range, options, and placeholder default are all the engine's own. Adding a
setting to the MVP is a change to the curated list in
`src/libslic3r/Web/SettingsCatalog.cpp` and nothing else.

Every control has an explicit "profile default" state, so a value is only ever
sent because the user chose it. A setting the engine gates on another one is
disabled once the user switches that other one off here; while it is unset the
selected profile decides, and the browser cannot know what the profile chose
without slicing it.

When the deployment's worker cannot describe its settings the form is replaced
by a note and the profiles are used unchanged, so the screen keeps working.

## Layer preview

`LayerPreviewPanel` reads the job's preview index and then fetches exactly one
layer at a time, so a tall print costs one layer of bandwidth and memory rather
than the whole preview. Each layer is decoded from the binary format in
[preview-format.md](preview-format.md) and drawn on a 2D canvas: extrusions
colored by role, travels behind a toggle, and a legend naming the roles that
layer actually contains.

Role colors are presentation and live in the app; the role ids and their labels
come from the preview document, so a role the engine adds later still draws.

## How it is served

The built bundle is served by the API itself, so the app always talks to its own
origin and a deployment is one process. `create_app` mounts
`web/frontend/dist` last, after every API route, when that directory exists;
`ORCA_WEB_FRONTEND_DIST` overrides the location. A bare API with no build is
still a valid JSON service.

The Vite dev server is the exception: it proxies `/api` to `ORCA_WEB_API_URL`
so the screen can be edited with hot reload against a separately running API.

## Tests

`web/frontend/e2e/` holds the Playwright suite, which drives real Chromium
against the API serving the real built bundle and the real native worker:

- A browser upload produces downloadable G-code, and the downloaded file is the
  published artifact with the expected layer count.
- The overrides form carries the engine's own bounds and options: a bounded
  setting renders its range, an unbounded one renders none, and a gated setting
  is disabled once its gate is switched off.
- An override outside the engine's range is refused before a job is created.
- The job report names the flattened profile chain and the overrides, and the
  job can be retried under a new id.
- The layer preview steps through the layers, and its index agrees with the
  G-code the same job published while each request returns one layer's bytes.
- An unsupported model is refused before any job is created.

Semantic equivalence is checked separately, by
`scripts/test_web_api_baseline.py`, which slices the recorded baseline fixtures
through the HTTP API and compares the downloaded G-code against the native
baseline run.

## Running it

```powershell
docker compose -f docker/web/compose.yml run --rm frontend-build
docker compose -f docker/web/compose.yml up api
docker compose -f docker/web/compose.yml run --rm frontend-e2e
docker compose -f docker/web/compose.yml run --rm api-baseline
```

`frontend-build` produces `dist`, after which `api` serves the screen on
`http://localhost:8000`. For hot reload, run `api` and then `frontend`, and open
`http://localhost:5173`. `node_modules` and the Playwright browser cache live in
named volumes, so they stay off the host bind mount.
