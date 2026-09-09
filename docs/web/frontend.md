# Browser screen

Status: Active contract

`web/frontend/` is the React, TypeScript, and Vite application chosen in
[ADR 0002](adr/0002-web-stack.md).

## What the screen does

- Select one STL, OBJ, or 3MF file and see its name and size.
- Choose a bundled printer, then a process and one or more filaments narrowed
  to that printer by the [API](api.md).
- Lay out the plate: see the model on the printer's own bed, select, move,
  rotate, scale, duplicate, delete, and arrange objects; assign each object a
  filament when more than one is chosen; and slice exactly the placement and
  assignment on screen.
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

## The plate

`Plater` is the browser half of [scene-format.md](scene-format.md). Choosing a
file uploads it immediately — the plater has to inspect it before anything is
sliced — and the plater then runs the upload's `inspect` job, reads the scene
index, and fetches each object's geometry one request at a time, the way the
API serves it.

What it submits is the write side of that same document: one `objects` entry
per placed copy, each a 16-number column-major millimetre matrix. When the
plater has a scene, a slice request always carries the placement on screen, so
the worker places rather than arranges. When it has none — an upload it could
not inspect, or a deployment whose worker has no `inspect` operation — the
request carries no `objects` at all and behaviour is exactly what it was before
the plater existed.

Two deliberate departures from "report placement as imported":

- **A freshly loaded plate is arranged.** `inspect` reports placement exactly as
  the file declares it, which for a loose mesh is wherever its author left it —
  possibly off the bed. Arranging on load is what a slice without explicit
  placement would have done anyway. The browser's own shelf packing is not the
  engine's `arrange`; it leaves a fixed 6 mm gap, because the browser cannot
  evaluate `min_object_distance` without the resolved configuration.
- **Every object sits on the bed.** The worker only lifts an object that is
  *entirely* below the bed, so a plater that let one hang would slice exactly
  the hanging placement. Z is therefore derived, not edited.

### Filament slots

A slice request carries a filament **list**, `filament_profiles`, rather than
one profile: `App` holds an ordered array of slots, each narrowed to the
current printer exactly like the old single select was. A slot can be added
up to 16 and removed down to 1 — never fewer, since a plate always slices with
at least one filament. With a single slot the screen looks and behaves exactly
as it always has: no per-object control, no swatches, one plain "Filament"
select with the same test id it has always had.

Once a second slot exists, every placed object carries a `filament` — a
1-based index into that array — shown next to its position, rotation, and
scale in the transform panel and submitted as that object's `filament`. A
freshly loaded or duplicated object starts on slot 1.

**Removing a slot never leaves an object pointing at a filament that no
longer exists.** The rule is a plain clamp to the new valid range: any
object's `filament` above the new slot count falls to the new last slot. This
is deliberately simple rather than trying to preserve which physical filament
an object was on — it is a pure function of the new count, not of which slot
was removed, so it is predictable — and it is visible immediately, because
the clamped object's color and its own select both update on the same render
that dropped the slot.

Each filament slot is colored, and every object on the canvas is painted in
its assigned filament's color, so the plate is readable at a glance; the
on-screen filament list uses the exact same palette (`filamentColors.ts`), so
the two always agree. Selection is drawn as a two-tone wireframe halo over an
object's own color rather than a color swap, precisely because the fill now
carries meaning — swapping it away on selection would hide which filament an
object is on, and risks landing on a color indistinguishable from another
slot's.

Filament color, filament-to-extruder mapping, and purge volumes are all the
worker's job. The browser never computes or sends any of them — it only
sends the ordered list of profile ids and each object's 1-based index into
it.

### Drawing it

`PlaterCanvas` is an orthographic camera with painter's-algorithm depth sorting
and flat shading — drawn with the Canvas 2D API, not WebGL. That is a tested
constraint rather than a preference: headless Firefox, one of the three
release-blocking browsers, exposes no WebGL context at all in the container the
Playwright matrix runs in (verified for `webgl` and `webgl2`, with
`webgl.force-enabled` and software WebRender both forced on), while Chromium and
WebKit get one from SwiftShader. A WebGL plater could not have been verified in
a browser the release depends on. Canvas 2D also matches how the layer preview
is already drawn, and adds no dependency.

Because a 2D canvas paints one path per triangle, drawing is bounded: past
20,000 triangles across the plate, objects are drawn as their bounding boxes
instead. The ceiling the worker enforces is a million, so this is a frame-time
budget for the view and never a limit on what can be sliced.

Pointer gestures — click to select, drag an object to move it on the bed, drag
the background to orbit, wheel to zoom — are conveniences. Every operation is
also a real control: the object list selects, the numeric fields move, rotate
and scale, and Duplicate, Delete and Arrange are buttons, so the plate is fully
editable from the keyboard.

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

`web/frontend/e2e/` holds the Playwright suite, run against the API serving
the real built bundle and the real native worker.

`slice.spec.ts` drives the supported flow end to end:

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

`plater.spec.ts` covers the plate:

- An upload shows the model's own bounds on the printer's own bed, arranged.
- Duplicate, Arrange, and Delete change the plate, and an empty plate disables
  Slice and says why.
- Rotation and uniform scale change the placed size the panel reports.
- The placement on screen is the placement that gets sliced: the displayed
  transform is the one the API recorded, the printed outline sits within
  `PLACEMENT_TOLERANCE_MM` (1 mm, half an extrusion width with skirt and brim
  switched off) of the placed model's box, and translating the object moves the
  printed outline by exactly that translation. `scripts/test_web_placement.py`
  makes the same checks against the HTTP API without a browser.
- Multi-filament plates: with a single slot the per-object filament control
  is absent; adding a slot and assigning a duplicated object to it submits
  the expected `filament_profiles` and per-object `filament`; removing a slot
  clamps rather than dangling; the job report names every filament; and a
  finished multi-filament slice's G-code carries a `T1` tool change.

`accessibility.spec.ts` covers the same screen from an accessibility angle:

- An automated `@axe-core/playwright` pass over the initial screen, over a
  finished job with its layer preview open, over a plate with several
  filament slots and an object reassigned off the default one, and over a
  screen showing an error, each asserting zero violations.
- A keyboard-only walk of the whole flow — the file input, the profile
  selects, the generated settings controls, Slice, and the layer preview's
  slider and travel toggle — asserting forward focus order and that every one
  of those controls is reachable and operable without a mouse.

### Browser matrix

Per [mvp.md](mvp.md), Chrome, Edge, and Firefox are release-blocking; **Safari
is tested but not blocking**. `playwright.config.ts` declares one project per
engine or channel — `chromium`, `firefox`, `msedge`, and `webkit` (WebKit
stands in for Safari, which cannot be automated outside macOS). Playwright has
no config-level flag to make one project's failures non-fatal, so that split
lives at the script level instead:

- `npm run e2e` — Chromium only. The fast default for local iteration, and
  what running the suite with no arguments has always done.
- `npm run e2e:matrix` — the three release-blocking browsers as one
  invocation; a failure in any of them fails it.
- `npm run e2e:webkit` — WebKit alone, as its own invocation.
- `npm run e2e:all` — runs both of the above, but does not propagate
  `e2e:webkit`'s exit code, so a WebKit-only failure is reported without
  failing the build.

The `msedge` project disables Edge's SmartScreen download protection. That
check has no answer for a file served from `127.0.0.1` and takes the headless
browser process down with it, which failed the download test in Edge alone;
the download is the thing under test, so the check is turned off rather than
the test.

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
