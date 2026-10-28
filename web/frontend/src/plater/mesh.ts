/** Decoders for the worker's display mesh, documented in `docs/web/plater.md`. */

export interface InspectedObject {
  index: number;
  name: string;
  offset: number;
  vertex_count: number;
  triangle_count: number;
  bounding_box: { min: [number, number, number]; max: [number, number, number] };
  /**
   * Placements the file declared. A project's are relative to the bed of the
   * 1-based `plate` holding each instance, which is null for an instance on no
   * plate.
   */
  instances: { transform: number[]; plate?: number | null }[];
}

export interface Inspection {
  format: "orca-web-mesh";
  version: number;
  model_format: string;
  declares_plate: boolean;
  /** The plates a project declares, empty for a mesh. */
  plates?: { index: number; name: string }[];
  objects: InspectedObject[];
}

export interface ObjectMesh {
  /** xyz per vertex, in object coordinates and millimetres. */
  positions: Float32Array;
  /** Vertex index triple per triangle. */
  indices: Uint32Array;
}

const MAGIC = "OWMESH01";

/** Slice one object's geometry out of the mesh artifact without copying it. */
export function decodeObjectMesh(buffer: ArrayBuffer, object: InspectedObject): ObjectMesh {
  const header = new TextDecoder().decode(new Uint8Array(buffer, 0, MAGIC.length));
  if (header !== MAGIC) throw new Error("The display mesh has an unknown format.");
  const view = new DataView(buffer);
  const vertices = view.getUint32(object.offset, true);
  const triangles = view.getUint32(object.offset + 4, true);
  if (vertices !== object.vertex_count || triangles !== object.triangle_count) {
    throw new Error("The display mesh does not match its inspection index.");
  }
  const start = object.offset + 8;
  const end = start + vertices * 12 + triangles * 12;
  if (end > buffer.byteLength) throw new Error("The display mesh is truncated.");
  // The record is 4-byte aligned by construction: an 8-byte magic, 4-byte
  // counts, and 4-byte elements.
  return {
    positions: new Float32Array(buffer, start, vertices * 3),
    indices: new Uint32Array(buffer, start + vertices * 12, triangles * 3),
  };
}
