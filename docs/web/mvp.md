# Web MVP scope

Date: 2026-09-01
Status: Accepted for initial implementation

## Goal

A user can open OrcaWebSlicer in a desktop browser, prepare one FFF build plate
from an STL or 3MF model, select bundled profiles, slice it, inspect the layers,
and download valid G-code without installing the desktop application.

The MVP proves that the browser workflow can preserve OrcaSlicer's native
slicing behavior. It is not intended to reproduce every desktop feature.

## Supported workflow

- FFF printers only.
- One build plate per job.
- STL and 3MF input.
- Add and remove objects.
- Select, move, rotate, uniformly scale, duplicate, and arrange objects.
- Select bundled printer, process, and filament profiles.
- Edit a curated set of common setting overrides.
- Display configuration validation errors and slicing warnings.
- Start, monitor, cancel, and retry a slicing job.
- Preview layers and toolpaths.
- Download G-code and a machine-readable job report.

## Explicit non-goals

- SLA slicing.
- Support, seam, color, fuzzy-skin, or height-range painting.
- Cut, emboss, SVG, text, measurement, and mesh-repair gizmos.
- Multiple build plates.
- Full expert-setting parity.
- User-created or Python plugins.
- Arbitrary post-processing commands.
- Direct LAN printer discovery, upload, monitoring, or camera streaming.
- Offline slicing and a full `libslic3r` WebAssembly build.
- Mobile and touch-first editing.

These are sequencing decisions, not permanent exclusions.

## Initial compatibility and safety limits

The limits are server configuration with conservative defaults, not constants in
the slicing engine:

- Current stable desktop Chrome, Edge, and Firefox are release-blocking.
- Safari is tested, but is not release-blocking for the first MVP.
- Maximum compressed upload: 250 MiB.
- Maximum extracted 3MF content: 1 GiB.
- Maximum triangle count per plate: 1,000,000, matching the existing CLI default.
- Maximum slicing time: 300 seconds, matching the existing CLI default.
- Maximum worker memory: 4 GiB.
- Maximum generated G-code: 1 GiB.
- Job artifacts expire after 24 hours unless a deployment overrides retention.

Every limit must produce a stable error code and a useful user-facing message.

## Functional acceptance criteria

The MVP is accepted when all of the following are automated:

1. A user uploads each supported fixture and sees the correct model bounds.
2. Submitted transforms are applied in millimetres and reproduce the displayed
   placement within a documented tolerance.
3. Profile inheritance and user overrides resolve to the same effective
   configuration as the native baseline.
4. Valid baseline jobs produce semantically equivalent G-code.
5. Invalid models and configurations return stable, categorized errors.
6. Progress is monotonic, warnings remain attached to their job, and cancellation
   terminates the worker and removes partial downloadable artifacts.
7. Layer preview agrees with the produced G-code on layer count, tools, extrusion
   roles, and Z range.
8. A worker crash affects one job and does not terminate the API service.

Semantic equivalence covers layer count, tool changes, extrusion roles, motion
and extrusion totals within tolerance, bounding boxes, temperatures, and the
presence and order of required machine commands. Byte-for-byte identity is not a
goal because generated timestamps and non-contractual comments may differ.

## Non-functional acceptance criteria

- Untrusted files are parsed only in a disposable worker process.
- Paths from archives cannot escape the job directory.
- Network access is disabled for slicing workers by default.
- CPU, memory, execution time, extracted size, and output size are bounded.
- Logs do not contain model contents, credentials, or complete G-code.
- Every job has a correlation identifier across API, queue, worker, and artifacts.
- The deployment prominently offers corresponding source as required by AGPLv3
  section 13. This requirement must be reviewed before public hosting.

## Deferred decisions

The frontend framework, API framework, database, queue, and object store are not
selected in G0. Those choices do not affect the worker boundary and will be made
after the first native vertical slice exposes actual throughput and operational
requirements.
