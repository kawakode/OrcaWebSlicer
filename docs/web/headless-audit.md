# Headless extraction audit

Date: 2026-09-02
Status: G2 complete; remaining items belong to later MVP gates

## Target boundary

The production target will be an `orca-slicer-worker` executable linked to
`libslic3r` and serialization/logging dependencies required by its public
interface. It must build when `SLIC3R_GUI=OFF` and must not link `libslic3r_gui`.

The existing `OrcaSlicer` executable is the behavioral reference, not the worker
implementation. Its `CLI::run` method is more than 6,000 lines and mixes argument
handling, profile resolution, model manipulation, multi-plate orchestration,
slicing, 3MF packaging, thumbnails, progress reporting, and process termination.
Copying that method into a new executable would preserve the coupling we need to
remove.

## Original blockers and disposition

### Unconditional desktop includes

`src/OrcaSlicer.cpp` includes these even outside its `SLIC3R_GUI` startup guard:

- `wx/stdpaths.h`;
- `slic3r/GUI/PartPlate.hpp`;
- `slic3r/GUI/BitmapCache.hpp`;
- `slic3r/GUI/OpenGLManager.hpp`;
- `slic3r/GUI/GLCanvas3D.hpp`;
- `slic3r/GUI/Camera.hpp`;
- `slic3r/GUI/Plater.hpp`;
- `slic3r/GUI/GuiColor.hpp`;
- `GLFW/glfw3.h`.

Consequently, the top-level `SLIC3R_GUI` option does not currently define a clean
CLI boundary even though `libslic3r` itself is added outside the GUI conditional.

### Plate orchestration

`CLI::run` constructs `GUI::PartPlateList` and uses `GUI::PartPlate` throughout
loading, validation, slicing, 3MF metadata, and artifact generation. This is the
largest extraction seam.

For the one-plate MVP, the first worker does not need a generic replacement for
all desktop multi-plate behavior. It needs a small neutral request model:

```text
SliceJob
  model and imported project metadata
  one PlateSelection
  effective DynamicPrintConfig
  output policy
```

The worker will use `Model`, `Print`, core format loaders, and core plate metadata
directly. Multi-plate orchestration remains deferred until the MVP boundary is
stable. Shared plate algorithms discovered during extraction should move to
`libslic3r`; desktop presentation state must remain in `libslic3r_gui`.

### Color and flush helpers

The CLI calls `GUI::BitmapCache::parse_color4` for configuration data and
`GUI::get_min_flush_volumes` during filament reconciliation. Neither operation is
inherently graphical.

Before creating worker equivalents:

1. Search for an existing core color parser or flush-volume implementation.
2. If none is reusable, extract the calculation into a small `libslic3r` utility.
3. Keep GUI adapters calling the same core function so behavior has one owner.
4. Add a focused `libslic3r` test before changing CLI call sites.

Completed: color decoding and minimum flush-volume calculations now have core
APIs and focused headless tests. Desktop callers use the same implementations.

### Thumbnail rendering

CLI project export creates hidden GLFW contexts and calls `OpenGLManager`,
`GLCanvas3D::render_thumbnail_framebuffer`, and `GCodeViewer`. This is incompatible
with a minimal server worker and unnecessary for G-code-only jobs.

The worker output policy will initially support:

- `gcode`: no OpenGL context and no project thumbnail;
- `project`: preserve existing embedded thumbnails when possible, but do not
  regenerate them in the first headless milestone;
- `preview`: emit toolpath data or metadata, not a desktop framebuffer capture.

Browser-rendered thumbnails can be added later as declared input artifacts when a
3MF export requires them.

### Filesystem and process ownership

The current CLI discovers resources relative to its executable, writes progress
and result files directly, creates temporary paths, and sometimes terminates the
process from deep error paths. The worker must instead receive explicit paths and
return errors to one top-level process boundary.

Required rules:

- resources directory is an explicit startup argument;
- input and output roots are explicit and canonicalized once;
- core orchestration returns typed results rather than calling `exit`;
- only the worker entry point maps a result to a process exit status;
- outputs are first written to temporary names and atomically promoted on success;
- cancellation is represented by a token checked by existing status callbacks.

## Extraction units

The work will be split into reviewable units in this order:

1. **Baseline runner:** capture existing CLI and engine behavior without moving
   production code.
2. **Worker shell:** add a target that accepts `--version` and validates a manifest
   envelope, while linking only the allowed native target set.
3. **Core color utilities:** remove GUI ownership from data-only parsing and flush
   calculations used by the CLI path.
4. **Single-plate request builder:** convert manifest inputs into `Model` and
   `DynamicPrintConfig` without `PartPlateList`.
5. **Slice operation:** call validation, `Print::process`, and G-code export with
   progress and cancellation callbacks.
6. **Artifact transaction:** write G-code and a structured result without desktop
   thumbnails.
7. **CLI comparison:** execute the same baseline cases through both paths and block
   on semantic differences.

Each unit must leave the desktop executable behavior unchanged.

All seven G2 units are complete for job-local STL/OBJ input. The worker loads
resolved profiles, uses core arrangement and slicing APIs, exports G-code, and
matches the initial native semantic matrix without linking the desktop target.
3MF import, transactional artifacts, progress, and cancellation remain explicit
G3/G4 work rather than blockers to the headless boundary.

## Dependency guard

Once the worker target exists, CI will inspect its link closure. At minimum it will
fail on these names:

```text
libslic3r_gui
wx
OpenGL
glfw
imgui
webkit
WebView2
python
avcodec
```

Platform-specific system libraries pulled transitively by allowed core import
dependencies will be reviewed separately. The guard is an architectural test, not
a claim that `libslic3r` already has a minimal dependency graph.

## First unresolved questions

- Which current 3MF loader path exposes the exact one-plate metadata needed without
  constructing `PartPlateList`?
- Which CLI validations are web contracts and which are upload-service policy?
- Can all required progress stages be represented by existing `Print` status
  callbacks, or is orchestration-level instrumentation needed?
- What is the smallest preview representation that preserves feature roles without
  loading complete G-code into API memory?

These questions are answered with baseline fixtures and targeted code spikes, not
by widening the initial worker interface.
