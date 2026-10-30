# OrcaWebSlicer roadmap

OrcaWebSlicer will be a browser-native user interface backed by isolated native
OrcaSlicer workers. The project will preserve `libslic3r` as the slicing engine;
it will not attempt to compile the existing wxWidgets application into a browser.

This directory is the source of truth for the web effort:

- [MVP scope and acceptance criteria](mvp.md)
- [Native compatibility baseline](baseline.md)
- [Headless extraction audit](headless-audit.md)
- [Worker protocol version 1](worker-protocol.md)
- [Isolated worker executor and job directories](executor.md)
- [Minimum API](api.md)
- [Browser application](frontend.md)
- [Browser plater](plater.md)
- [Layer preview](preview.md)
- [Projects and thumbnails](projects.md)
- [ADR 0001: browser UI with native workers](adr/0001-browser-ui-native-workers.md)
- [ADR 0002: minimum web stack](adr/0002-web-stack.md)
- [ADR 0003: multiple plates](adr/0003-multiple-plates.md)

## Delivery gates

Work proceeds through these gates in order. A later gate may be prototyped to
reduce uncertainty, but it must not become a production dependency before its
prerequisites pass.

| Gate | Deliverable | Exit condition | Status |
| --- | --- | --- | --- |
| G0 | Scope and architecture | MVP, non-goals, limits, licensing, and security assumptions are documented | Complete |
| G1 | Native baseline | Representative fixtures pass with recorded semantic output, wall time, and peak memory | Complete (initial matrix) |
| G2 | Headless boundary | A worker linked without wxWidgets, desktop OpenGL, device code, or embedded Python reproduces G1 | Complete |
| G3 | Worker contract | Versioned jobs, events, errors, cancellation, and artifacts have contract tests | Complete |
| G4 | Vertical slice | Browser upload produces downloadable G-code through an isolated worker | Complete |
| G5 | Browser MVP | Plater, common settings, validation, and layer preview meet the MVP criteria | Complete |
| G6 | Production readiness | Authentication, quotas, isolation, observability, retention, and deployment checks pass | Not started |

## Engineering rules

- Slicing compatibility is tested semantically. Timestamps, comments, archive
  ordering, and harmless floating-point formatting are not contracts.
- The web service never loads untrusted models in its long-lived API process.
- Each slicing job runs in a separate process with time, memory, input, and
  output limits.
- The browser contract is versioned independently from internal C++ classes.
- Desktop behavior remains unchanged while the headless boundary is extracted.
- Frontend controls for slicing settings are generated from engine metadata;
  setting definitions are not manually duplicated in TypeScript.
- Every feature added after the MVP brings a fixture or targeted behavioral test.

## Immediate implementation sequence

Transactional publication, path containment, cooperative worker limits, the
isolated executor boundary, guarded single-plate 3MF import, the web-stack ADR,
the disposable job-directory lifecycle, the [minimum API](api.md), and the
[first browser screen](frontend.md) are now in place, so G4 is complete: a
browser upload produces semantically baseline-equivalent G-code through a
disposable worker.

G5 is complete as well: the [plater](plater.md) composes one plate from several
uploads and submits explicit transforms, settings controls come from the engine
catalog, and the [layer preview](preview.md) pages worker-generated toolpaths.
The next phase goes beyond the single-plate MVP; production deployment (G6)
comes after it. Its first step is in place: a project holds several
[plates](plater.md#plates), sliced one job per plate, and desktop projects
with several plates import with their placements.

The canonical build environment is defined by
[docker/web/compose.yml](../../docker/web/compose.yml).

The initial G1 run on 2026-09-01 built the Linux package, passed all 610
registered CTest tests, and produced deterministic semantic output for the
cube, bridge, and concave-hole CLI fixtures. Five tests were explicitly marked
skipped by the existing suite. On 2026-09-02, the headless worker reproduced all
recorded semantic fields for those three fixtures while passing its forbidden-
dependency audit. The next active gate is G3.
