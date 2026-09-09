# Scene format version 1

Status: Active contract

The browser plater needs geometry to draw and a bed to draw it on, and the API
process must never parse an untrusted model. So the worker does it: the
`inspect` operation loads a model through the same importer a slice uses and
publishes what it found as two files — `scene.json`, a small index, and
`scene.bin`, the geometry. Splitting them is what lets the API serve one object
at a time.

This is the read side of the plater. The write side is the `objects` array on a
slice request, described at the end.

## Where the data comes from

`inspect` shares its importer with `slice`: STL and OBJ through their format
loaders, a 3MF's selected plate through the core project importer, with the same
archive-containment and single-plate rules and the same triangle and input-size
ceilings. The shared entry points live in `SinglePlateSlice.hpp` so the worker
never has two importers to keep in sync.

`inspect` never arranges and never sinks an object onto the bed. It reports
placement exactly as imported, because the plater is what edits placement — and
a slice that is given explicit placement uses that instead of arranging, so what
the user saw is what gets sliced.

## `scene.json`

```json
{
  "scene_version": 1,
  "units": "mm",
  "quantum_mm": 0.001,
  "bed": {"shape": [[0, 0], [220, 0], [220, 220], [0, 220]], "printable_height": 250.0},
  "data": "output/scene.bin",
  "data_bytes": 442368,
  "objects": [
    {"index": 0, "name": "20mm_cube", "triangle_count": 12,
     "bounding_box": {"min": [x, y, z], "max": [x, y, z]},
     "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 110, 110, 10, 1],
     "offset": 0, "length": 432, "vertex_count": 36}
  ]
}
```

`bed.shape` is the printer's own `printable_area` polygon in millimetres, taken
from the machine profile the request named; without one, the engine's defaults
answer instead.

`objects` is in import order, which is what a slice request's `source_object`
indexes. `offset` and `length` are that object's byte range in `scene.bin`; the
ranges are contiguous and cover the file exactly.

`bounding_box` is in the object's own local frame, the same frame its vertices
are in. `transform` is a 16-number column-major 4x4 matrix that places that
frame on the bed. Volume transforms are already baked into the vertices, so this
is the instance transform and nothing else.

## `scene.bin`

Little-endian throughout. Each object's range is `vertex_count` vertices of
three `int32` coordinates each, in units of `quantum_mm`:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | `int32 x 3` | x, y, z of one vertex, in units of `quantum_mm` |

It is a triangle soup, not an indexed mesh: every triangle contributes its own
three vertices, so `vertex_count` is always three times `triangle_count` and the
browser groups the blob by three and derives a flat normal per triangle without
an index buffer of its own. That costs some size and buys a decoder with no
index-validation surface at all.

Coordinates are micrometres in a signed 32-bit integer — the same quantum the
[layer preview](preview-format.md) uses, for the same reason.

## Serving it

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/uploads/{upload_id}/scene` | Start, or return, the inspect job for one upload |
| `GET` | `/scenes/{job_id}` | The index |
| `GET` | `/scenes/{job_id}/objects/{n}` | One object's bytes |

The API parses the index once per job and keeps it, then answers an object
request by seeking to that object's offset and streaming its bytes. The blob is
never read whole. See [api.md](api.md).

## Slicing what the plater shows

A slice request may carry the placement the browser is displaying:

```json
"objects": [
  {"source_object": 0, "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 5, 0, 1], "filament": 1},
  {"source_object": 0, "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 60, 5, 0, 1], "filament": 2}
]
```

`source_object` indexes the scene's `objects` array, and the transform uses the
same column-major millimetre layout the scene publishes. The same
`source_object` may repeat, which is how a duplicate is expressed: the example
above slices two copies of one imported object.

`filament` is optional and 1-based, naming which of the request's filament
profiles that copy prints in; the example above prints the two copies in
different filaments. Omitting it, or sending `0`, leaves the object's own
assignment alone. See [worker-protocol.md](worker-protocol.md) for the filament
list it indexes and the per-filament state the worker derives from it.

When `objects` is present the worker replaces the imported objects with exactly
one placed copy per entry and does not arrange. When it is absent, behaviour is
unchanged: loose meshes are arranged and a project plate keeps its own
placement. Every `source_object` and `filament` is validated before the model is
touched, so a bad index fails the job instead of half-applying it.

## Limits

`ORCA_WEB_MAX_SCENE_BYTES` bounds `scene.bin` and defaults to 128 MiB; a request
may tighten it through `limits.max_scene_bytes`. Unlike the layer preview, which
is auxiliary and is skipped with a warning when it would be too big, a scene the
browser asked for and did not get is a failure: the job ends with
`scene_size_limit_exceeded` in the `resource_limit` category and publishes
nothing. `limits.max_input_bytes` and `limits.max_triangles` apply exactly as
they do to a slice.

## Verifying it

`scripts/test_web_scene.py` runs the worker's own `inspect` over one fixture of
each supported format — OBJ, STL, and 3MF — and checks that the published scene
agrees with the blob it describes and reports the size that fixture actually is.
That is `docs/web/mvp.md`'s first acceptance criterion: every supported fixture
shows the correct model bounds. `worker-smoke` runs it.

`tests/libslic3r/test_scene_export.cpp` (`[SceneExport]`) exports a real mesh
and checks the index against the blob it describes: the ranges tile the file
exactly, every range holds whole triangles, `vertex_count` is three times
`triangle_count`, and each declared bounding box matches the vertices actually
decoded from that object's bytes. It also covers the path, format, plate-index,
and envelope rejections and the over-limit failure.
