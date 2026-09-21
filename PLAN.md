# OrcaWebSlicer implementation plan

Last updated: 2026-09-21

## Objective

Deliver a browser-native, single-plate FFF slicing workflow backed by isolated
native OrcaSlicer workers. Preserve `libslic3r` slicing behavior and keep the
desktop application unchanged.

## Current position

The foundation is complete:

- [x] G0: MVP scope, architecture, safety assumptions, and non-goals documented.
- [x] G1: Repeatable native Docker baseline established.
- [x] G2: Headless STL/OBJ worker built without desktop GUI dependencies.
- [x] Resolved machine, process, and filament profiles load in the worker.
- [x] Cube, bridge, and concave-hole worker output matches the native semantic
  baseline.
- [x] Docker smoke, dependency, and parity checks are automated.
- [x] G3: the versioned worker contract, transactional artifacts, cancellation,
  safety limits, and guarded single-plate 3MF import are complete.

- [x] G4: the minimum API and the first browser screen turn a browser upload
  into downloadable, baseline-equivalent G-code through a disposable worker.

- [x] G5: the browser MVP. The settings and profile UI is generated from a
  versioned catalog the engine exports and compatibility expressions are
  resolved by the engine's own placeholder parser; a sliced job publishes a
  browsable layer preview the API serves one layer at a time; the plater draws
  the scene the worker publishes and slices exactly the placement it displays;
  and the baseline matrix covers support, multipart, invalid configuration,
  Unicode 3MF, and the output-size limit across all three lanes.

Multi-filament landed after G5 closed and is described in section 18.

The active gate is G6, production readiness. Isolation, security scanning, the
reference single-instance lifecycle, per-owner authentication and
authorization, and per-owner quotas are implemented and tested. Abuse controls
and rate limiting are next; an immutable production release unit and durable recovery remain G6
exit gates.

## Phase 1: Finish the worker foundation (G3, priority P0)

### 1. Define the complete versioned protocol

- [x] Separate the stable envelope from versioned operation payloads.
- [x] Define typed job states: `accepted`, `running`, `succeeded`, `failed`, and
  `canceled`.
- [x] Define machine-readable progress, warning, error, and artifact records.
- [x] Choose and document the event transport emitted by the worker, preferably
  newline-delimited JSON on stdout with logs restricted to stderr.
- [x] Add stable error categories for request, input, profile, validation,
  slicing, cancellation, resource-limit, and internal failures.
- [x] Emit a final `result.json` containing the job ID, protocol version,
  outcome, warnings, timing, resource usage, and artifact metadata.
- [x] Add golden fixtures and contract tests for forward-compatible fields,
  unsupported versions, and every terminal state.

### 2. Make artifact creation transactional

- [x] Canonicalize the job root once and reject paths or symlinks that escape it.
- [x] Write G-code and reports to temporary names inside the job directory.
- [x] Atomically promote artifacts only after slicing and validation complete.
- [x] Remove partial artifacts after validation errors and graceful cancellation.
- [x] Remove abandoned partial artifacts after crashes and output-limit failures.
- [x] Record artifact size and SHA-256 in the final result.
- [x] Test existing files, nested output directories, symlink attacks, and
  interrupted writes on Linux and Windows-compatible filesystems.

### 3. Add progress and cancellation

- [x] Adapt existing `Print` status callbacks into monotonic protocol events.
- [x] Map engine stages to stable public stages without exposing internal class
  names as API contracts.
- [x] Install a cancellation token checked by model import, arrangement,
  slicing, and export where the existing engine permits it.
- [x] Handle `SIGINT`, `SIGTERM`, and Windows console cancellation requests
  gracefully in the worker.
- [x] Support forced termination after a bounded grace period in the executor.
- [x] Ensure canceled jobs return a stable exit code and no downloadable partial
  artifacts.
- [x] Add a deliberately slow fixture and automated cancellation tests.

### 4. Enforce initial safety limits

- [x] Enforce server-configured 250 MiB input, 1,000,000 triangle, 300 second,
  4 GiB memory, and 1 GiB output defaults in the worker, while allowing requests
  only to tighten them.
- [x] Enforce the 1 GiB extracted-content limit when 3MF support is added.
- [x] Keep hard CPU, memory, process, and wall-time limits in the worker runtime;
  use worker-side checks for clearer errors where possible.
- [x] Run slicing workers with network access disabled.
- [x] Reject unsupported formats before invoking expensive import paths.
- [x] Bound manifest size and setting count.
- [x] Bound log and event volume.
- [x] Verify logs never include complete G-code, model contents, or credentials.

### 5. Add single-plate 3MF input

- [x] Identify the smallest core 3MF import path that does not construct
  `GUI::PartPlateList`.
- [x] Extract the selected plate's model, transforms, project configuration, and
  filament mapping into the neutral worker request model.
- [x] Reject multi-plate selection beyond the declared single-plate scope with a
  stable error.
- [x] Protect archive extraction against absolute paths, `..`, symlinks,
  decompression bombs, and oversized entries.
- [x] Preserve existing embedded metadata where possible; do not generate
  desktop OpenGL thumbnails.
- [x] Add Unicode-path, invalid-archive, multi-plate, and compatibility fixtures.

### G3 exit criteria

- [x] Every terminal outcome has a contract test and stable exit behavior.
- [x] Progress is monotonic and warnings remain attached to the job.
- [x] Cancellation stops a real slice and removes partial artifacts.
- [x] Limits and path containment are tested with adversarial inputs; worker-side
  limits, path/symlink attacks, executor hard-limit cases, and 3MF archive
  containment and decompression cases are covered.
- [x] STL, OBJ, and one selected 3MF plate produce transactional artifacts.
- [x] `worker-smoke`, `worker-baseline`, and the forbidden-dependency audit pass.

## Phase 2: Build the first browser vertical slice (G4, priority P1)

### 6. Select the minimum web stack

- [x] Record an ADR choosing the frontend framework, API framework, and local
  development layout.
- [x] Prefer a thin API that never parses untrusted models in its long-lived
  process.
- [x] Defer database, queue, and object-store commitments until local job
  throughput and artifact sizes are measured.

### 7. Implement the isolated job executor

- [x] Create a fresh job directory for every request.
- [x] Copy only declared inputs and resolved profiles into that directory.
- [x] Spawn exactly one worker process per job with explicit resources and
  limits.
- [x] Parse worker events, persist the final result, and expose cancellation.
- [x] Treat malformed output, worker crashes, timeouts, and missing artifacts as
  isolated job failures.
- [x] Add cleanup for expired and abandoned job directories.

### 8. Add the minimum API

- [x] Upload an STL, OBJ, or supported 3MF file.
- [x] List bundled printer, process, and filament profiles.
- [x] Submit one slice request with curated setting overrides.
- [x] Read job status and progress.
- [x] Cancel and retry a job.
- [x] Download G-code and `result.json` only after successful publication.
- [x] Add API schema validation, correlation IDs, and integration tests.

### 9. Add the first browser screen

- [x] Provide file upload and bundled profile selection.
- [x] Show selected file, profiles, validation errors, progress, and warnings.
- [x] Provide Slice, Cancel, Retry, and Download actions.
- [x] Keep this screen intentionally simple; do not build the full plater yet.
- [x] Add an end-to-end test proving browser upload to downloadable G-code.

### G4 exit criteria

- [x] A browser upload produces semantically baseline-equivalent G-code through
  a disposable worker.
- [x] Cancellation, worker crash, invalid input, and timeout behavior are covered
  end to end.
- [x] The API process remains healthy after every worker failure scenario.

## Phase 3: Complete the browser MVP (G5, priority P2)

### 10. Implement the single-plate plater

The plate is drawn on a 2D canvas with an orthographic camera rather than in
WebGL. That is a tested constraint, not a preference: headless Firefox, one of
the three release-blocking browsers, exposes no WebGL context at all in the
container the Playwright matrix runs in, so a WebGL plater could not have been
verified in a browser the release depends on.

- [x] Publish model geometry and configured bed bounds from the engine, and
  serve them to the browser one object at a time.
- [x] Render that geometry and those bed bounds in the browser.
- [x] Add selection, deletion, move, rotate, uniform scale, duplicate, and
  arrange operations.
- [x] Use millimetres consistently and accept explicit transforms on a slice
  request, replacing arrangement with exactly the placement that was submitted.
- [x] Submit those transforms from the plater.
- [x] Verify displayed and sliced placement against fixtures within a documented
  tolerance.
- [x] Support several objects on one plate, including duplicates of one imported
  object, while retaining the one-plate limit.

### 11. Generate settings and profile UI from engine metadata

- [x] Export a versioned catalog of bundled profiles and relevant setting
  metadata from the engine.
- [x] Implement printer, process, and filament compatibility filtering.
- [x] Add the curated common settings required by the MVP.
- [x] Preserve types, units, ranges, enum values, defaults, dependencies, and
  validation messages without duplicating definitions manually in TypeScript.
- [x] Show the effective profile chain and user overrides in the job report.

### 12. Add browser layer preview

- [x] Choose and document a compact preview artifact format.
- [x] Generate preview data without `GCodeViewer`, desktop OpenGL, or framebuffer
  thumbnails.
- [x] Render layers, toolpaths, tools, and extrusion roles in the browser.
- [x] Stream or page large previews so the API does not load complete G-code into
  memory.
- [x] Verify preview layer count, Z range, tools, and roles against produced
  G-code.

### 13. Finish MVP behavior and compatibility

- [x] Surface slicing warnings and actionable configuration errors.
- [x] Add retry using the same immutable inputs and a new job ID.
- [x] Expand the baseline with support, multipart, invalid configuration,
  Unicode 3MF, and output-limit fixtures, each declared once in
  `docs/web/baseline-cases.json` and run through the native, worker, and API
  lanes. Cancellation stays in `scripts/test_web_worker_cancellation.py`, which
  signals a running worker mid-slice — something a manifest-driven runner
  cannot do.
- [x] Multi-filament: a slice request names 1-16 filament profiles and assigns
  each placed object to one of them, end to end from the browser. It has no
  baseline fixture, and cannot have one, for the reason recorded in
  [baseline.md](docs/web/baseline.md): the desktop CLI crashes whenever a
  second filament is actually used, so there is no native run to compare
  against. The worker is verified directly instead.
- [x] Test current desktop Chrome, Edge, and Firefox; test Safari as non-blocking.
- [x] Add accessibility and keyboard-navigation checks for the supported flow.

### G5 exit criteria

- [x] Every functional acceptance criterion in `docs/web/mvp.md` is automated:
  model bounds per supported format by `scripts/test_web_scene.py`, placement by
  `scripts/test_web_placement.py` and `plater.spec.ts`, effective configuration
  and semantic equivalence by the three baseline lanes, categorized errors by
  the `invalid-config` case and the API tests, progress and cancellation by
  `worker-smoke` and the executor tests, preview agreement by
  `scripts/test_web_preview.py`, and worker-crash isolation by
  `tests/web/test_job_service.py`.
- [x] The complete supported workflow works without installing the desktop app:
  the Playwright suite drives upload, plate, overrides, slice, preview, and
  download against the API and a disposable worker.
- [x] Desktop behavior and native baseline tests remain unchanged. This gate's
  final batch changed no C++ translation unit, and the re-recorded native
  baseline reproduces the byte counts recorded on 2026-09-01 exactly for the
  three original cases.

## Phase 4: Production readiness (G6, priority P3)

### 14. Harden isolation and deployment

- [x] Run workers as an unprivileged user with a read-only root filesystem,
  private temporary storage, no network, dropped capabilities, and bounded
  resources. The executor applies everything a process can apply to itself —
  a seccomp filter that denies every address family but `AF_UNIX`,
  `PR_SET_NO_NEW_PRIVS`, capability drop, the user switch with the job
  directory handed over, `TMPDIR` and `HOME` inside the job, and an
  allowlisted environment — and fails the job closed when one cannot hold. The
  read-only root filesystem stays the container's, because a process cannot
  give itself one without namespaces the runtime refuses.
- [x] Select and document the production container/runtime isolation model:
  [ADR 0003](docs/web/adr/0003-worker-sandbox.md). It also records the residual
  shared-uid risk, and the two engine defects that running a worker
  unprivileged for the first time surfaced — a 3MF backup tree written to
  `/orcaslicer_model` at the filesystem root, and Boost.Log's default sink
  writing engine diagnostics onto the protocol's stdout.
- [x] Scan the canonical web image and frontend dependencies with pinned Syft
  and Grype versions, retain CycloneDX SBOMs and complete JSON findings, and
  fail the release gate on fixable High or Critical vulnerabilities. Static
  custom-CMake dependencies remain an explicitly documented cataloger gap.
- [x] Add separate liveness and readiness checks, close admission atomically,
  hold shutdown behind already-admitted uploads and job staging, cancel queued
  jobs without launching them, and configure the reference container's bounded
  graceful stop. Focused tests exercise admission races and an actual Uvicorn
  `SIGTERM` with running and queued jobs; a black-box canary verifies readiness,
  upload, slicing, and artifact checksums. The rollback and empty-state disaster
  recovery runbook is explicit that job metadata is process-local, the current
  Compose image is not immutable, and durable recovery remains gated by
  persistent storage.

### 15. Add service controls

- [x] Select and document the provider-neutral identity boundary, public probe
  surface, authenticated browser session contract, and secure production
  default before wiring ownership into uploads and jobs. ADR 0004 keeps OIDC
  and browser sessions at the edge, requires the API to verify a short-lived
  signed assertion, and derives the ownership and quota key from its issuer and
  opaque subject without persisting the raw identity claims.
- [x] Implement authentication and authorization appropriate to the
  deployment. `ORCA_WEB_AUTH_MODE` defaults to `required`: every `/api/v1`
  route but the three health checks verifies a short-lived RS256 bearer
  assertion against a local, read-only JWKS file, and `owner_id` (derived from
  the assertion's issuer and subject, never persisted or logged raw) is
  enforced on every upload, job, artifact, preview, and scene lookup, list,
  cancellation, and retry. `disabled` mode, used only by the reference
  development Compose file and isolated tests, is an explicit fixed
  `local-development` principal. See
  [ADR 0004](docs/web/adr/0004-service-identity.md) and
  [the API contract](docs/web/api.md#authentication-and-authorization).
- [x] Add per-user concurrency, storage, CPU-time, and request quotas. Each is
  keyed by `owner_id` and refused with a stable HTTP 429 code: queued-or-running
  jobs, job submissions and charged worker time over a rolling window, and
  bytes held in uploads plus retained job directories. Job quotas are checked
  before staging and again atomically when the job is recorded; an upload is
  streamed against a reservation of the remaining storage budget. The worker
  is untrusted, so a run is charged at least the wall time the executor
  measured. `GET /api/v1/quota` reports limits and usage. The rolling windows
  are process-local until persistent metadata is selected; see
  [the API contract](docs/web/api.md#quotas).
- [ ] Add abuse controls and rate limiting without weakening stable job errors.
- [ ] Select persistent metadata and artifact storage based on measured needs.
- [ ] Enforce the default 24-hour artifact retention policy and deletion audit.

### 16. Add observability and operations

- [ ] Emit structured API, executor, and worker logs linked by job ID.
- [ ] Record queue time, slice time, peak memory, failures, cancellations,
  artifact size, and cleanup outcomes.
- [ ] Add dashboards and alerts for saturation, crash loops, timeout rates,
  storage growth, and parity regressions.
- [ ] Document operator runbooks and privacy-safe support diagnostics.

### 17. Complete release and compliance work

- [ ] Add CI jobs for headless builds, contract tests, semantic parity, browser
  tests, dependency audits, and container security checks.
- [ ] Review AGPLv3 section 13 obligations before public hosting.
- [ ] Prominently offer the exact corresponding source for the deployed version.
- [ ] Document third-party licenses, privacy behavior, retention, security
  assumptions, and known limitations.
- [ ] Perform a security review and resolve all release-blocking findings.

### G6 exit criteria

- [ ] Authentication, quotas, isolation, observability, retention, compliance,
  deployment, and rollback checks pass in a production-like environment.
- [ ] A worker compromise or crash is contained to one disposable job.
- [ ] The release checklist is reproducible from a clean checkout.

## Phase 5: Beyond the MVP

### 18. Multi-filament

Completed on 2026-09-09, after G5 closed. It was scoped out of the gate as a
fixture-sized deferral; it turned out to be a feature the engine could not
perform at all, so it is recorded here rather than folded back into G5.

- [x] A slice request names 1-16 filament profiles, and each placed object
  names which of them it prints in.
- [x] The worker synthesizes the per-filament state the engine indexes but no
  headless producer sizes — `filament_colour`, `filament_map`, and the N x N
  `flush_volumes_matrix` — the way `PresetBundle` does for the desktop. Without
  it the engine reads past the end of vectors sized for one filament; see
  [baseline.md](docs/web/baseline.md) for the measurements.
- [x] The worker places a prime tower that does not fit the printable area and
  warns where it moved it, standing in for the desktop's `PartPlate`, which the
  headless worker has no equivalent of.
- [x] The browser chooses filament slots, assigns each object to one, and
  colors the plate by filament.
- [x] The pure colour-space maths moved from `src/slic3r/Utils/` into
  `libslic3r`, so `FlushVolCalc` no longer reaches up into the GUI layer for
  `RGB2HSV` and the headless worker can link it.

Deliberately not done, and why:

- [ ] Multi-nozzle printers. A multi-filament request against a machine
  declaring more than one `nozzle_diameter` is refused with
  `multi_nozzle_filament_map_unsupported` rather than mapped onto the first
  nozzle. Deciding which nozzle each filament feeds is real assignment logic
  (see `FilamentGroup.cpp`), and silently guessing it would mis-slice hardware
  where a wrong nozzle is physically reachable.
- [ ] A baseline fixture. The native lane is the desktop CLI, which crashes on
  this input, so there is no reference run to compare against. The capability
  is covered by the worker's own tests, the API suite, and the browser suite.
- [ ] Fixing the desktop CLI. The same synthesis would plausibly repair it, but
  that was not established, so no claim is made. It is upstream work the web
  tier does not depend on.

### 19. Desktop-style interface

The browser screen is rebuilt to look and work like the OrcaSlicer desktop
application's Prepare/Preview workspace. No desktop UI code runs in the
browser: wxWidgets and the desktop OpenGL viewer cannot, and compiling the
desktop app to WebAssembly or streaming it over VNC would give up the
per-owner isolation and quotas the service is built on.

#### Step 1: layout and theme (done 2026-09-21)

- [x] Dark title bar holding Import, the model name, the Prepare/Preview
  workspace tabs, and the plate actions (Slice plate, Cancel, Retry, Export
  G-code, Report).
- [x] Sidebar with the desktop's Printer, Filament, and Process panels in the
  desktop's order, with settings grouped under the engine's group titles and
  the desktop's own group icons.
- [x] Viewport with a canvas toolbar, the plate, and an Objects/Object
  transform panel. The layer preview has a floating legend, and a status dock
  holds hints, errors, and the job report.
- [x] Slicing switches to Preview, and the plate stays mounted so it keeps
  every edit. A model dropped on the viewport is imported.
- [x] Icons imported from `resources/images`, not copied. Colors follow the
  desktop palette in light and dark themes and are checked against WCAG AA.
- [x] Every `data-testid` kept. The keyboard walk and the placement test are
  updated for the new order, and the axe pass is unchanged.

#### Step 2: a WebGL 3D plate and preview (planned)

The 2D canvas stays. Headless Firefox, a release-blocking browser, has no
WebGL in the test container, so the 2D renderers remain both the fallback and
the renderer Firefox is verified with. WebGL is an enhancement chosen at run
time, never a requirement.

2a. Decision and renderer seam

- [ ] Record ADR 0005: three.js (pinned, MIT, scanned by the existing Grype
  gate) against raw WebGL2. The recommendation is three.js, for its camera,
  picking, and line/instancing support. Record the bundle-size budget it may
  add.
- [ ] Extract the interface `PlaterCanvas` already implies (bed, draw items,
  camera, select, move) so `Plater` keeps owning all state and either
  renderer can be mounted. Do the same for the layer preview.
- [ ] Choose the renderer by probing for a `webgl2` context, with a
  `?renderer=2d|webgl` override so both paths can be tested in one browser.
  The active renderer is exposed as a `data-renderer` attribute for tests.

2b. 3D plate at parity

- [ ] A perspective camera with orbit, pan, and zoom, and the desktop's view
  presets (top, bottom, front, rear, left, right, isometric).
- [ ] Shaded meshes with one buffer per source object, drawn instanced for
  duplicates, which replaces the 20,000-triangle bounding-box budget. Measure
  a frame-time budget up to the worker's million-triangle ceiling.
- [ ] Selection outline, click-to-select by ray picking, and drag on the bed
  plane, feeding the same `onSelect`/`onMove` the 2D canvas does.
- [ ] The bed drawn as a grid within the printable-area shape the scene
  already carries, with the printable-height volume outlined. Objects outside
  it are tinted, matching the existing outside-the-build-volume notice.

2c. Printer bed models and textures

- [ ] Serve a printer profile's `bed_model` and `bed_texture` through a new,
  owner-agnostic, allowlisted catalog route. Only files the bundled profile
  tree names are served, with the same path-containment checks the catalog
  applies, and a missing or unsupported file falls back to the grid.

2d. 3D layer preview

- [ ] Draw a contiguous range of layers stacked in 3D, with the desktop's
  two-handled vertical layer-range slider and the horizontal within-layer
  move slider. Layers are still fetched one at a time, now progressively and
  under a memory budget, so a tall print never loads whole.
- [ ] Extrusions drawn as instanced quads or tubes, using each batch's own
  width and height, colored by feature type or by tool, with travels behind
  the existing toggle.
- [ ] Speed, fan, and temperature color schemes are not planned here: the
  preview format does not carry them. They would need a `preview_version` 2
  in the engine, which is its own batch.

2e. Gizmos (optional, last)

- [ ] On-canvas move arrows, a Z-rotation ring, and a uniform-scale handle,
  limited to the transforms a slice request can express. Every gizmo action
  stays a real control too, so the plate remains fully keyboard-operable.

Step 2 exit criteria

- [ ] The Playwright matrix passes with the 2D renderer everywhere and the
  WebGL renderer in Chromium, Edge, and WebKit. That includes the placement
  test (displayed placement equals sliced placement) under WebGL.
- [ ] Firefox without WebGL selects the 2D renderer automatically, and a test
  proves it.
- [ ] The axe pass and the keyboard walk are unchanged under both renderers.
- [ ] The added bundle size and the measured frame times are recorded in
  `docs/web/frontend.md`, and ADR 0005 is accepted.

Still out of scope: painting tools (supports, seams, colors), cut, text,
measure, variable layer height, and multiple plates. Each needs engine or
request-format support that a slice request cannot yet express.

## Required checks for every implementation batch

- [ ] Add focused tests for every behavior change.
- [ ] Build the headless worker with `SLIC3R_GUI=OFF`.
- [ ] Run `worker-smoke` and the forbidden-dependency audit.
- [ ] Run `worker-baseline` for slicing or profile changes.
- [ ] Compile affected desktop translation units and run proportional native
  tests to protect desktop compatibility.
- [ ] Run `git diff --check` and keep unrelated worktree changes untouched.
- [ ] Update the roadmap and contract documentation when a gate changes.

## Decisions intentionally deferred

A choice is made only when its phase begins. These are the ones still open:

- Database, queue, and object-store products. Job state and artifacts live on
  the job-directory filesystem behind `JobService` until local throughput and
  artifact sizes are measured.
- Production hosting, and with it whether each job gets its own container or
  its own uid. The in-process sandbox in
  [ADR 0003](docs/web/adr/0003-worker-sandbox.md) composes with either.
- Authentication provider and billing model.

Settled since: the frontend and API frameworks in
[ADR 0002](docs/web/adr/0002-web-stack.md), the preview artifact encoding in
[preview-format.md](docs/web/preview-format.md), and the scene encoding the
plater consumes in [scene-format.md](docs/web/scene-format.md).

SLA slicing, multiple plates, painting tools, desktop gizmo parity, direct
printer control, plugins, arbitrary post-processing, offline WebAssembly
slicing, and mobile-first editing remain outside the MVP.
