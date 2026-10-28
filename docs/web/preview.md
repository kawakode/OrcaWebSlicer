# Layer preview

Status: Active contract for G5

A slice publishes a layer preview beside its G-code. It is produced by the
worker from the engine's own G-code processor result, so it needs neither the
desktop `GCodeViewer`, an OpenGL context, nor a framebuffer.

## Format

The preview is two artifacts.

`preview` is a sequence of fixed 36-byte little-endian records, one per
extrusion move, grouped by layer in print order:

| Bytes | Field |
| --- | --- |
| 0-23 | `float32` x0, y0, z0, x1, y1, z1 in millimetres |
| 24-27 | `float32` extrusion width |
| 28-31 | `float32` layer height |
| 32 | `uint8` extrusion role |
| 33 | `uint8` tool |
| 34-35 | reserved |

Travels, retractions, and wipes are not recorded: they change where the next
segment starts, but are not drawn.

`preview_index` is the JSON index:

```json
{
  "format": "orca-web-preview",
  "version": 1,
  "segment_bytes": 36,
  "roles": [{"id": 2, "name": "Outer wall", "used": true}],
  "tools": [0],
  "layers": [{"index": 0, "z": 0.2, "height": 0.2, "offset": 0, "count": 812}],
  "bounding_box": {"min": [90, 90, 0.2], "max": [110, 110, 20]}
}
```

Role ids are the engine's extrusion roles and their names are the same names
the G-code writes after `;TYPE:`. A layer is the engine's layer, which is also
one `;LAYER_CHANGE` block in the G-code. `offset` and `count` locate a layer's
records, and consecutive layers are contiguous.

## Paging

The API never loads a complete preview or G-code into memory.
`GET /jobs/{job_id}/preview/layers?start=a&end=b` streams the records of layers
`a` through `b`, inclusive, as one contiguous byte range after checking the
index against the segment file. A range outside the index is
`invalid_preview_range` (416), and a page over 256 MiB is
`preview_page_too_large` (413). The browser requests pages of about 8 MiB, so
the first layers draw while later ones are still arriving.

## Rendering

The preview tab draws each segment as a line coloured with the desktop viewer's
default role colours. A slider sets the highest visible layer, and each used
role can be hidden from the legend.

## Verification

`tests/libslic3r/test_plate_artifacts.cpp` slices a placed plate and checks the
preview's layer count against the G-code's `;LAYER_CHANGE` count, its outer
walls against the submitted placement, and its byte layout against the index.
The Playwright suite checks the layer count, Z range, and placement through the
browser, and `src/preview/preview.test.ts` covers decoding and paging.
