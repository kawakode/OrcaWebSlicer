# ADR 0001: Browser UI with native slicing workers

Date: 2026-09-01
Status: Accepted

## Context

OrcaSlicer separates much of its slicing logic into `libslic3r`, but the desktop
application and current CLI still depend on wxWidgets GUI classes, desktop OpenGL,
native filesystems, device networking, and embedded Python. The browser cannot
provide equivalent native APIs, and the complete dependency graph is not prepared
for Emscripten.

The service must safely process untrusted, memory-intensive geometry without a
failed slice terminating other requests. It must also preserve compatibility with
OrcaSlicer profiles, 3MF projects, and generated G-code.

## Decision

Use a browser-native frontend and a native `libslic3r` worker process.

The system is divided into four boundaries:

1. **Browser:** model visualization and manipulation, setting controls, validation
   presentation, job progress, and toolpath preview.
2. **API:** authentication, upload handling, job metadata, quotas, and artifact
   authorization. It never parses model geometry.
3. **Coordinator:** queueing, worker lifecycle, cancellation, resource limits, and
   artifact collection.
4. **Worker:** native model/profile loading, configuration normalization,
   validation, slicing, and G-code generation in a disposable process.

The worker consumes a versioned job manifest plus files in an isolated job
directory. It emits newline-delimited structured events and writes declared
artifacts into an output directory. The API does not link `libslic3r` in-process.

The worker target must not link:

- `libslic3r_gui` or wxWidgets;
- desktop OpenGL, GLFW, ImGui, or WebView libraries;
- printer discovery and device-management implementations;
- embedded Python or user plugins.

Core dependencies required by supported import and slicing operations remain
allowed initially. They can be split later using measurements rather than
speculation.

## Consequences

### Positive

- Reuses the proven native slicing engine and profile formats.
- Isolates crashes, leaks, and resource exhaustion by job.
- Allows horizontal worker scaling independently of the API.
- Keeps the browser contract stable while C++ internals evolve.
- Avoids making browser memory and cross-origin isolation prerequisites for the
  initial product.

### Costs

- Requires server compute and artifact storage.
- Needs an explicit serialization boundary and preview representation.
- Does not provide offline slicing in the MVP.
- A remote service cannot discover printers on a user's LAN. That requires a
  later local companion or printer-specific browser-compatible API.

## Rejected alternatives

### Compile the full desktop application to WebAssembly

Rejected for the MVP. wxWidgets, legacy desktop OpenGL paths, native device APIs,
filesystem assumptions, TBB, OpenCASCADE, OpenVDB, Python plugins, and other
dependencies make this a large and fragile port. It would still produce a desktop
UI inside a browser rather than a browser-native experience.

### Link `libslic3r` directly into the API process

Rejected. A malformed model, assertion, memory leak, or runaway slice could affect
unrelated requests and complicate safe cancellation and resource accounting.

### Shell out to the current desktop CLI permanently

Accepted only as a temporary baseline tool. The current CLI includes GUI-owned
plate, color, thumbnail, OpenGL, and G-code-viewer code. The production worker will
replace that coupling with a narrow headless interface.

### Rewrite the slicer in a web language

Rejected. It would discard the most valuable and heavily tested part of the
codebase while creating long-term profile and G-code compatibility risk.

## Follow-up decisions

Separate ADRs will choose the manifest schema, event transport, preview format,
frontend framework, service framework, queue, and storage only when their decision
inputs are available.
