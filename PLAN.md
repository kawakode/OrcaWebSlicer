# OrcaWebSlicer implementation plan

Last updated: 2026-10-28

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

- [x] G5: the browser MVP adds the single-plate plater, settings generated from
  engine metadata, and the paged browser layer preview.

The next phase goes beyond the single-plate MVP toward desktop feature parity
(multiple plates, multi-material, and further tools). Production deployment
(G6) follows that work; its hosting target is a VPS running Docker.

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

- [x] Render supported model geometry and configured bed bounds.
- [x] Add selection, deletion, move, rotate, uniform scale, duplicate, and
  arrange operations.
- [x] Use millimetres consistently and submit explicit transforms to the worker.
- [x] Verify displayed and sliced placement against fixtures within a documented
  tolerance.
- [x] Support adding multiple objects to one plate while retaining the one-plate
  limit.

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
- [x] Expand the baseline with support, multipart, multi-filament, invalid
  configuration, Unicode 3MF, cancellation, and output-limit fixtures.
- [x] Test current desktop Chrome, Edge, and Firefox; test Safari as non-blocking.
- [x] Add accessibility and keyboard-navigation checks for the supported flow.

### G5 exit criteria

- [x] Every functional acceptance criterion in `docs/web/mvp.md` is automated.
- [x] The complete supported workflow works without installing the desktop app.
- [x] Desktop behavior and native baseline tests remain unchanged.

## Phase 3b: Beyond the MVP

Work toward desktop feature parity, in independent batches. Each batch keeps the
checks below green in CI.

### Continuous integration of the web checks

- [x] Run the Compose checks on every pull request with GitHub Actions:
  worker build, `worker-smoke`, `worker-matrix`, `executor-smoke`,
  `frontend-build`, Chromium and Firefox end-to-end tests (WebKit reported but
  not blocking), `worker-baseline`, and `api-baseline`.
- [x] Produce the native desktop baseline in CI and cache it until the desktop
  slicer, its dependencies, or the baseline inputs change.
- [x] Document the jobs, caches, and expected durations in
  [`docs/web/baseline.md`](docs/web/baseline.md#continuous-integration).

### Expert and per-object settings (lot 3)

- [x] Export the whole process and filament catalog from `PrintConfigDef`
  (settings catalog version 2), grouped by the engine's categories, with each
  option's desktop mode and whether an object or a height range may override it.
- [x] Search the catalog and switch between simple, advanced, and expert modes
  in the browser, still without any setting definition in TypeScript.
- [x] Override settings per plate object and per non-overlapping height range,
  limited to the engine's object and region options as on the desktop.
- [x] Verify that a per-object override changes only that object's G-code
  (Catch2), and the search, mode, and per-object editing flow end to end.

### Object tools

- [x] Cut an object with a plane through the engine's own `Cut`, keeping
  either or both parts, optionally flipped or laid on the cut face.
- [x] Repair a mesh: weld vertices, drop degenerate and unconnected facets,
  fill holes, and fix normals, with a report of what changed.
- [x] Publish edited parts as new inspected uploads in plate coordinates, so
  the plater keeps them where they were.
- [x] Lay an object on a picked face and measure between two points in the
  browser.
- [x] Cover edit requests, cut bounds, and repair in Catch2, the edit route in
  the API suite, and every tool in the browser suite.
- [ ] Offer a tilted cut plane, connectors, and dowels in the cut panel.
- [x] Keep an object's setting overrides on its edited parts, and its height
  ranges after a repair.
- [ ] Carry modifiers and per-volume settings through an edit.

### Projects: save and export 3MF

- [x] Save the composed plate (objects, instance transforms, selected presets,
  and overrides) as a desktop-compatible 3MF through the worker's
  `export_project` operation and the engine's own 3MF exporter; the API never
  writes the archive.
- [x] Record the selection as the desktop does (`*_settings_id` and
  `different_settings_to_system`) so the desktop opens the same presets,
  marked as modified, and the browser restores them from an inspection.
- [x] Render plate thumbnails on the CPU, without OpenGL, for the project and,
  on request, for the G-code at the sizes the printer's `thumbnails` setting
  asks for.
- [x] Reopen a saved project in the browser with its placement, profiles, and
  overrides; a project added to an existing plate only adds its objects.
- [x] Catch2 round trip through `load_bbs_3mf`, API tests, and end-to-end tests.
- [ ] Export every plate once projects hold several plates.

### Multiple plates

- [x] Record an ADR choosing one job per plate over a multi-plate worker
  operation ([ADR 0003](docs/web/adr/0003-multiple-plates.md)).
- [x] Import every plate of a desktop project archive and place each instance
  on its own plate's bed; withdraw `multi_plate_project_unsupported`.
- [x] Inspect a project's plates, names, and per-instance plates.
- [x] Slice a project's plates as one batch of per-plate jobs, with artifacts
  named after their plate.
- [x] Show plates as tabs, move objects between plates, and arrange, slice, and
  preview each plate.
- [x] Cover the plate layout and per-plate slicing in Catch2, batches in the API
  tests, and plates in the browser suite.

## Phase 4: Production readiness (G6, priority P3)

### 14. Harden isolation and deployment

- [ ] Run workers as an unprivileged user with a read-only root filesystem,
  private temporary storage, no network, dropped capabilities, and bounded
  resources.
- [ ] Select and document the production container/runtime isolation model.
- [ ] Scan images and dependencies and generate an SBOM.
- [ ] Add health checks, graceful shutdown, deployment rollback, and disaster
  recovery procedures.

### 15. Add service controls

- [ ] Implement authentication and authorization appropriate to the deployment.
- [ ] Add per-user concurrency, storage, CPU-time, and request quotas.
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

- [x] Add CI jobs for headless builds, contract tests, semantic parity, and
  browser tests (`.github/workflows/web.yml`, see Phase 3b).
- [ ] Add CI jobs for dependency audits and container security checks.
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

These choices are not blockers for G3 and should be made only when their phase
begins:

- Frontend and API frameworks.
- Database, queue, and object-store products.
- Preview artifact encoding.
- Production hosting and sandbox technology.
- Authentication provider and billing model.

SLA slicing, painting tools, full desktop gizmo parity, direct
printer control, plugins, arbitrary post-processing, offline WebAssembly
slicing, and mobile-first editing remain outside the MVP.
