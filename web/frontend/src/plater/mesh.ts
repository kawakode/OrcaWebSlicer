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
  /** The 1-based filament the file assigned the object, 0 for none. */
  extruder?: number;
  /** Every part of the object, with the filament the file gave it (0 for none). */
  volumes?: InspectedVolume[];
}

export interface InspectedVolume {
  index: number;
  name: string;
  type: "part" | "negative" | "modifier" | "support_blocker" | "support_enforcer" | "other";
  extruder: number;
  /** A part's first triangle in the object's display mesh, and how many it has. */
  triangle_offset?: number;
  triangle_count?: number;
  /** The painting the file carries, per kind, in the worker's wire form. */
  paint?: Partial<Record<"supports" | "seam" | "color" | "fuzzy_skin", string>>;
}

/** The preset selection and overrides a project archive records. */
export interface ProjectSelection {
  printer: string;
  process: string;
  filaments: string[];
  /** Each filament's colour, `#RRGGBB`, in the same order. */
  filament_colours?: string[];
  /** Overridden settings in their serialized form, keyed by setting. */
  settings: Record<string, string>;
}

export interface Inspection {
  format: "orca-web-mesh";
  version: number;
  model_format: string;
  declares_plate: boolean;
  /** The plates a project declares, empty for a mesh. */
  plates?: { index: number; name: string }[];
  objects: InspectedObject[];
  /** Present when the model is a project that names its presets. */
  project?: ProjectSelection;
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

/** A display mesh ready to draw: its own vertex normals, triangles in the source order. */
export interface ShadedMesh {
  positions: Float32Array;
  normals: Float32Array;
  indices: Uint32Array;
}

/**
 * Give each triangle corner the area-weighted normal of the faces around its
 * vertex that bend less than `creaseDegrees` from the triangle itself, and
 * split a vertex wherever its corners end up with different normals. Flat
 * faces then shade flat and sharp edges stay sharp, while curved surfaces
 * keep smooth shading. Averaging across every edge, as a shared-vertex mesh
 * otherwise does, smears dark streaks over flat faces next to sharp edges.
 * Triangle `t` stays triangle `t`, so picks and painting are unaffected.
 */
export function shadeMesh(mesh: ObjectMesh, creaseDegrees = 30): ShadedMesh {
  const { positions, indices } = mesh;
  const triangles = indices.length / 3;
  const vertices = positions.length / 3;
  // Per face: the cross product (twice the area, so already area-weighted) and its unit normal.
  const weighted = new Float32Array(triangles * 3);
  const unit = new Float32Array(triangles * 3);
  for (let t = 0; t < triangles; t++) {
    const a = indices[3 * t] * 3, b = indices[3 * t + 1] * 3, c = indices[3 * t + 2] * 3;
    const ux = positions[b] - positions[a], uy = positions[b + 1] - positions[a + 1], uz = positions[b + 2] - positions[a + 2];
    const vx = positions[c] - positions[a], vy = positions[c + 1] - positions[a + 1], vz = positions[c + 2] - positions[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    weighted[3 * t] = nx;
    weighted[3 * t + 1] = ny;
    weighted[3 * t + 2] = nz;
    const length = Math.hypot(nx, ny, nz);
    if (length > 0) {
      unit[3 * t] = nx / length;
      unit[3 * t + 1] = ny / length;
      unit[3 * t + 2] = nz / length;
    }
  }

  // The faces around each vertex, as a compressed adjacency list.
  const start = new Uint32Array(vertices + 1);
  for (let i = 0; i < indices.length; i++) start[indices[i] + 1]++;
  for (let v = 0; v < vertices; v++) start[v + 1] += start[v];
  const faces = new Uint32Array(indices.length);
  const fill = start.slice(0, vertices);
  for (let i = 0; i < indices.length; i++) faces[fill[indices[i]]++] = (i / 3) | 0;

  const cosine = Math.cos((creaseDegrees * Math.PI) / 180);
  const outPositions: number[] = [];
  const outNormals: number[] = [];
  const outIndices = new Uint32Array(indices.length);
  // Corners of one vertex sharing a normal share an output vertex.
  const split = new Map<number, number[]>();
  for (let t = 0; t < triangles; t++) {
    const tx = unit[3 * t], ty = unit[3 * t + 1], tz = unit[3 * t + 2];
    for (let corner = 0; corner < 3; corner++) {
      const v = indices[3 * t + corner];
      let nx = 0, ny = 0, nz = 0;
      for (let k = start[v]; k < start[v + 1]; k++) {
        const f = faces[k];
        if (f === t || unit[3 * f] * tx + unit[3 * f + 1] * ty + unit[3 * f + 2] * tz >= cosine) {
          nx += weighted[3 * f];
          ny += weighted[3 * f + 1];
          nz += weighted[3 * f + 2];
        }
      }
      const length = Math.hypot(nx, ny, nz);
      if (length > 0) {
        nx /= length;
        ny /= length;
        nz /= length;
      } else {
        nz = 1; // A degenerate sliver with no area: any normal will do.
      }
      let candidates = split.get(v);
      if (!candidates) split.set(v, (candidates = []));
      let target = -1;
      for (const candidate of candidates) {
        const dot = outNormals[3 * candidate] * nx + outNormals[3 * candidate + 1] * ny + outNormals[3 * candidate + 2] * nz;
        if (dot > 0.9999) {
          target = candidate;
          break;
        }
      }
      if (target < 0) {
        target = outPositions.length / 3;
        outPositions.push(positions[3 * v], positions[3 * v + 1], positions[3 * v + 2]);
        outNormals.push(nx, ny, nz);
        candidates.push(target);
      }
      outIndices[3 * t + corner] = target;
    }
  }
  return { positions: new Float32Array(outPositions), normals: new Float32Array(outNormals), indices: outIndices };
}
