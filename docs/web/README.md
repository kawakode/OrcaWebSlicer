# OrcaWebSlicer roadmap

OrcaWebSlicer will be a browser-native user interface backed by isolated native
OrcaSlicer workers. The project will preserve `libslic3r` as the slicing engine;
it will not attempt to compile the existing wxWidgets application into a browser.

This directory is the source of truth for the web effort:

- [MVP scope and acceptance criteria](mvp.md)
- [Native compatibility baseline](baseline.md)
- [Headless extraction audit](headless-audit.md)
- [Worker protocol version 1](worker-protocol.md)
- [ADR 0001: browser UI with native workers](adr/0001-browser-ui-native-workers.md)

## Delivery gates

Work proceeds through these gates in order. A later gate may be prototyped to
reduce uncertainty, but it must not become a production dependency before its
prerequisites pass.

| Gate | Deliverable | Exit condition | Status |
| --- | --- | --- | --- |
| G0 | Scope and architecture | MVP, non-goals, limits, licensing, and security assumptions are documented | Complete |
| G1 | Native baseline | Representative fixtures pass with recorded semantic output, wall time, and peak memory | Complete (initial matrix) |
| G2 | Headless boundary | A worker linked without wxWidgets, desktop OpenGL, device code, or embedded Python reproduces G1 | Complete |
| G3 | Worker contract | Versioned jobs, events, errors, cancellation, and artifacts have contract tests | In progress |
| G4 | Vertical slice | Browser upload produces downloadable G-code through an isolated worker | Not started |
| G5 | Browser MVP | Plater, common settings, validation, and layer preview meet the MVP criteria | Not started |
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

1. Write G-code through an artifact transaction so failed or canceled jobs never
   expose partial downloads.
2. Add process-level cancellation and resource-limit tests before connecting a
   long-lived API service.
3. Add guarded single-plate 3MF import after path containment and extraction
   limits are enforced.

The first browser screen is intentionally after these steps.

The canonical build environment is defined by
[docker/web/compose.yml](../../docker/web/compose.yml).

The initial G1 run on 2026-09-01 built the Linux package, passed all 610
registered CTest tests, and produced deterministic semantic output for the
cube, bridge, and concave-hole CLI fixtures. Five tests were explicitly marked
skipped by the existing suite. On 2026-09-02, the headless worker reproduced all
recorded semantic fields for those three fixtures while passing its forbidden-
dependency audit. The next active gate is G3.
