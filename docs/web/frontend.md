# First browser screen

Status: Active contract for G4

`web/frontend/` is the React, TypeScript, and Vite application chosen in
[ADR 0002](adr/0002-web-stack.md). G4 keeps it deliberately small: it is one
screen that proves a browser upload reaches a disposable worker and comes back
as downloadable G-code. The plater, the generated settings UI, and the layer
preview are G5 and are not started here.

## What the screen does

- Select one STL, OBJ, or 3MF file and see its name and size.
- Choose a bundled printer, then a process and filament narrowed to that
  printer by the [API](api.md).
- Edit four curated overrides: `layer_height`, `sparse_infill_density`,
  `wall_loops`, and `enable_support`. Blank fields are not sent, so the
  profile's own value stands.
- Slice, Cancel, and Retry, each disabled when the job state makes it
  meaningless.
- Watch the worker's stage and percentage while the job runs, and read its
  warnings and its categorized error when it fails.
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

`web/frontend/e2e/` holds the Playwright suite, which drives real Chromium
against the API serving the real built bundle and the real native worker:

- A browser upload produces downloadable G-code, and the downloaded file is the
  published artifact with the expected layer count.
- A rejected setting surfaces the worker's stable error, offers no G-code
  download, and can be retried.
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
