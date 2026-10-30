# ADR 0003: Multiple plates

Date: 2026-10-28
Status: Accepted

## Context

The MVP sliced one plate. Going beyond it, a browser project holds several
plates, each of which must be sliceable on its own, and all of which must be
sliceable together. Desktop projects already hold several plates, and the G3
worker refused them with `multi_plate_project_unsupported`.

Two shapes were possible:

- A worker operation that slices every plate in one process, taking
  `plates: [{objects...}]` and publishing one G-code per plate.
- One ordinary `slice` job per plate, with the API looping over the plates.

## Decision

Each plate is one ordinary `slice` job, and the API submits a project's plates
together as a batch (`POST /batches`). The worker contract keeps one plate per
job: a slice still publishes exactly one G-code, one preview, and one result.

The worker learns only what a desktop project needs: it imports every plate of
a project archive and places each instance on its own plate's bed, so a browser
plate is described exactly as a G5 plate was, in the coordinates of one bed.

## Rationale

- **Isolation stays per plate.** A crash, a timeout, or a limit failure on one
  plate fails that plate's job and nothing else, exactly as G4 required of a
  job. A multi-plate worker would need partial-success results and would have
  to decide which plates' artifacts survive a failure on another.
- **Limits stay meaningful.** Wall-time, memory, and output limits are per job,
  so a project of ten plates does not get one ten-plate budget, nor does it
  need a new budget model.
- **Plates run in parallel.** The API's worker pool already runs jobs
  concurrently; one process slicing plates in turn would serialize them.
- **Every job operation keeps working.** Cancel, retry, progress, preview
  paging, and artifact download need no change, because a plate is a job.
  The batch only groups jobs: it can be read and canceled as one, and its jobs
  are otherwise independent.
- **The worker transaction model is unchanged.** One artifact transaction per
  output, published together or not at all, holds per plate.

The cost is that the inputs of a batch are staged once per plate, and each
plate loads its models in its own process. Model files are bounded by the
existing input limits, and the desktop's 36-plate ceiling bounds a batch, so
this is acceptable until measured otherwise.

## Consequences

- A project archive's every plate is imported. An instance belongs to the plate
  its bounding-box centre falls on in the desktop's plate layout: plates in a
  grid of `ceil(sqrt(count))` columns, each one bed width (in whole
  millimetres) plus a fifth apart, from the archive's own `printable_area`.
  This mirrors `PartPlateList` without linking it.
- An inspection lists every object of every plate, with each instance's plate
  and its placement relative to that plate's bed. An `objects` slice indexes
  objects the same way, whichever plate it slices.
- `input_model` with `plate_index` slices that plate of an archive, on its own
  bed. `plate_index_out_of_range` and `empty_plate_selection` remain.
- `multi_plate_project_unsupported` is withdrawn. A client that relied on it
  now receives the selected plate's result instead; no stored data changes, so
  no other migration is needed.
- Artifact downloads carry the plate number (`<job>-plate-<n>.gcode`) when the
  request names its plate.
