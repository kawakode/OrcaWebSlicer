# Browser application

Status: Active contract for G5

`web/frontend/` is the React, TypeScript, and Vite application chosen in
[ADR 0002](adr/0002-web-stack.md), with three.js for the plate and the preview.

## What the application does

- Add STL, OBJ, and 3MF models to one plate. Each is inspected by a worker and
  drawn from the geometry it exported; see [the plater](plater.md).
- Select, move, rotate, scale, duplicate, delete, and arrange objects with the
  pointer, the object panel, or the keyboard. An object outside the printer's
  printable volume is flagged and blocks slicing.
- Choose a bundled printer, then a process and filament narrowed to that
  printer by the [API](api.md), including `compatible_printers_condition`
  answers from the engine.
- Edit the curated overrides in a form generated from the engine's settings
  catalog: each control's type, unit, choices, and dependency come from
  `PrintConfig.cpp`, and its placeholder is the selected profile's own value.
  Blank fields are not sent, so the profile's value stands.
- Slice, Cancel, and Retry, each disabled when the job state makes it
  meaningless.
- Watch the worker's stage and percentage while the job runs, and read its
  warnings, its categorized error, the effective profile chain, and the
  overrides it applied.
- Inspect the result in the Preview tab, which opens on its own when a slice
  succeeds; see [the layer preview](preview.md).
- Download the G-code and the machine-readable job report.

Every failure the user sees is the API's stable code plus its message.
Framework schema rejections are reduced to the same shape, so the screen has one
error path rather than two.

## How it is served

The built bundle is served by the API itself, so the app always talks to its own
origin and a deployment is one process. `create_app` mounts
`web/frontend/dist` last, after every API route, when that directory exists;
`ORCA_WEB_FRONTEND_DIST` overrides the location. A bare API with no build is
still a valid JSON service.

The Vite dev server is the exception: it proxies `/api` to `ORCA_WEB_API_URL`
so the screen can be edited with hot reload against a separately running API.

## Tests

`npm test` runs the Vitest unit tests for the placement and preview logic.

`web/frontend/e2e/` holds the Playwright suite, which drives real Chromium
against the API serving the real built bundle and the real native worker:

- An uploaded cube shows its true bounds, is sliced where it was placed within
  the documented tolerance, and its preview agrees with the G-code on layer
  count and Z range.
- Duplicated and arranged objects are all present in the sliced result.
- The plate can be edited from the keyboard.
- An object outside the printable volume blocks slicing.
- The settings form is generated from the engine catalog, including dependent
  controls.
- A rejected setting is refused with a stable code before any job exists, and
  its control is marked invalid. A configuration error the engine raises while
  slicing names its setting the same way.
- An unsupported or unreadable model never reaches the plate.
- The prepared plate has no serious or critical axe-core WCAG 2 A/AA
  violations.

Chromium always runs. `ORCA_WEB_E2E_BROWSERS` adds `firefox`, `webkit`, or
`msedge` when that browser is installed. The `frontend-e2e` service runs
Chromium and Firefox as release-blocking and WebKit, Safari's engine, as
reported but non-blocking, as `docs/web/mvp.md` requires. Edge shares
Chromium's engine and runs wherever its `msedge` channel is installed.

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
