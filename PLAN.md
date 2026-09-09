# OrcaWebSlicer implementation plan

Last updated: 2026-09-07

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

The active gate is G5, the browser MVP. All three of its features are in place:

- [x] The settings and profile UI is generated from a versioned catalog the
  engine exports, and compatibility expressions are resolved by the engine's own
  placeholder parser.
- [x] A sliced job publishes a browsable layer preview that the API serves one
  layer at a time.
- [x] The browser plater draws the scene the worker publishes and slices exactly
  the placement it displays.

What remains for G5 is the expanded baseline matrix.

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

SLA slicing, multiple plates, painting tools, desktop gizmo parity, direct
printer control, plugins, arbitrary post-processing, offline WebAssembly
slicing, and mobile-first editing remain outside the MVP.
