# Browser plater

Status: Active contract for G5

The plater lets a user compose one build plate in the browser and submit it as
explicit placements. It never parses a model itself: every uploaded file is
first [inspected](worker-protocol.md#inspection-operation) by a disposable
worker, and the browser draws the geometry the worker exported.

## Display geometry

An inspection publishes two artifacts.

`mesh` is little-endian binary. It starts with the 8-byte magic `OWMESH01` and
a `uint32` object count, followed by one record per object:

| Field | Type |
| --- | --- |
| vertex count | `uint32` |
| triangle count | `uint32` |
| vertices | `float32` x, y, z per vertex |
| triangles | `uint32` vertex index triple per triangle |

The vertices are the object's printable parts in object coordinates, with each
part's own transform applied and no instance transform.

`inspection` is the JSON index:

```json
{
  "format": "orca-web-mesh",
  "version": 1,
  "model_format": "obj",
  "declares_plate": false,
  "objects": [{
    "index": 0,
    "name": "20mm_cube",
    "offset": 12,
    "vertex_count": 8,
    "triangle_count": 12,
    "bounding_box": {"min": [0, 0, 0], "max": [20, 20, 20]},
    "instances": [{"transform": [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]}]
  }]
}
```

`offset` is the byte offset of the object's record, so the browser reads each
object without copying the file. `instances` are the placements the file
declared. When `declares_plate` is true, a project archive placed its objects
and the plater keeps those placements; otherwise the model is a loose mesh and
the plater centres it on the bed and arranges the plate. A project that names
its presets also carries a `project` object, described in
[the projects contract](projects.md).

## Placement

Every length is in millimetres. An item's transform is built from its user
controls in this order: move the object's bounding-box centre to the origin,
scale uniformly, rotate about X, then Y, then Z, move the centre to the chosen
X and Y, and finally lower or raise the item so its lowest vertex rests at Z 0.
The resting step reproduces what the worker does to a floating object, so the
plater and the engine agree on height.

The plate is submitted as `objects`, grouping the items that share an upload
and object index into one entry with one instance each. Each transform is the
item's matrix in row-major order. The worker applies it unchanged.

Arrangement is the browser's own: it shelf-packs every item's footprint inside
the bed's bounding rectangle with 6 mm spacing and centres the block. Because
the result is submitted as explicit transforms, what is shown is what is sliced;
the engine's own arrangement is used only for a plain `upload_id` request.

The printable volume comes from the selected printer's `printable_area` and
`printable_height`, served by `GET /profiles/effective`. An item outside it is
drawn in red, listed as outside, and slicing is disabled. The worker checks the
same volume against the object's convex hull and refuses the plate with
`object_outside_plate`, so a non-rectangular bed is still enforced by the
engine even where the browser's rectangle check is looser.

## Placement tolerance

The end-to-end suite compares the plater's bounds with the outer walls of the
sliced preview and accepts a difference of 0.5 mm. Outer walls are inset from
the model surface by half an extrusion width, and the displayed mesh is the
unsliced surface, so the two are not expected to coincide exactly.

## Editing

- Click an object to select it; drag the selected object to move it on the bed.
- Edit X, Y, rotation about each axis, and uniform scale in the object panel.
- Duplicate, delete, and arrange from the panel.
- From the keyboard: select an object in the object list with Enter, then focus
  the plate. Arrow keys move it by 1 mm (10 mm with Shift), R rotates it by
  90 degrees, Ctrl+D duplicates it, Delete removes it, Escape clears the
  selection.

## Tests

`src/plater/plate.test.ts` covers the transform order, row-major
serialization, resting on the bed, round-tripping a declared placement, the
printable-volume check, non-overlapping arrangement, and request grouping.
`tests/libslic3r/test_plate_artifacts.cpp` covers inspection output, request
validation, explicit placement, and the outside-the-plate refusal in the
engine. The Playwright suite checks displayed bounds against the sliced result.
