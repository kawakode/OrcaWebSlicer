# Layer preview format version 1

Status: Active contract for G5

A sliced job publishes a browsable preview of its own toolpaths as two files:
`preview.json`, a small index, and `preview.bin`, the geometry. Splitting them
is what lets the API serve one layer at a time; a client never downloads a whole
print to look at one layer.

## Where the data comes from

`Print::export_gcode` already runs the G-code processor to produce the file it
writes, so the worker takes that `GCodeProcessorResult` and turns its moves into
the preview. Nothing is re-parsed, and no `GCodeViewer`, OpenGL context, or
framebuffer is involved — the preview is generated in the same headless worker
that produced the G-code, and it is published through the same transactional
artifact rules as the G-code itself.

A preview is auxiliary. If it would exceed `ORCA_WEB_MAX_PREVIEW_BYTES` or the
G-code contains no extrusion, the job still succeeds with its G-code, publishes
no preview, and carries a warning saying why.

## `preview.json`

```json
{
  "preview_version": 1,
  "units": "mm",
  "quantum_mm": 0.001,
  "data": "output/preview.bin",
  "data_bytes": 918273,
  "segment_count": 40312,
  "tools": [0],
  "roles": [{"id": "outer_wall", "label": "Outer wall"}],
  "bounding_box": {"min": [x, y, z], "max": [x, y, z]},
  "layers": [
    {"index": 0, "z": 0.2, "offset": 0, "length": 4096, "segments": 210,
     "roles": ["outer_wall", "sparse_infill"], "tools": [0]}
  ]
}
```

`layers` is ordered by `z`, with the layer id breaking a tie so printing by
object keeps G-code order. `offset` and `length` are the layer's byte range in
`preview.bin`; the ranges are contiguous and cover the file exactly.

`roles` is the preview's own stable vocabulary, indexed by the `role` byte in
the blob. The engine's `role_to_string` returns translated display text, which
is not a wire contract, so the id is defined by the preview and the label
travels beside it.

A layer with no extrusion is start-up or shutdown motion, not a printed layer,
and never appears in `layers`.

## `preview.bin`

Little-endian throughout. Each layer's range is a run of batches; each batch is
one polyline of `points` points, so a batch of *n* points draws *n − 1*
segments.

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | `uint8` | Kind: 0 extrude, 1 travel |
| 1 | `uint8` | Role index into `roles`; 0 for travel |
| 2 | `uint8` | Tool (extruder) id |
| 3 | `uint8` | Reserved, zero |
| 4 | `uint32` | Point count, at least 2 |
| 8 | `float32` | Extrusion width in mm; 0 for travel |
| 12 | `float32` | Extrusion height in mm; 0 for travel |
| 16 | `int32 × 3 × count` | x, y, z in units of `quantum_mm` |

A batch runs while kind, role, tool, width, and height hold and each move starts
where the last one ended. Width and height are rounded to 0.01 mm first: they
only set a rendered ribbon's size, and exact float equality would split one wall
into hundreds of batches.

Coordinates are micrometres in a signed 32-bit integer, which covers any bed
this MVP supports with three orders of magnitude to spare.

## Serving it

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/jobs/{job_id}/preview` | The index |
| `GET` | `/jobs/{job_id}/preview/layers/{n}` | One layer's bytes |
| `GET` | `/jobs/{job_id}/artifacts/preview` | The index as a download |

The API parses the index once per job and keeps it, then answers a layer request
by seeking to that layer's offset and streaming its bytes in chunks. The blob is
never read whole, and neither is the G-code.

## Verifying it

`scripts/test_web_preview.py` slices a fixture through the real worker and
checks the preview against the G-code it was generated from: layer count, the Z
of every layer, the set of tools, and the set of extrusion roles must agree, and
every layer's byte range must decode into whole batches that exactly cover it.
That check is functional acceptance criterion 7 in [mvp.md](mvp.md), and it runs
in the `worker-smoke` service.
