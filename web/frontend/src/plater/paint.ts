/**
 * Face painting: support and seam enforcers and blockers, filaments, and fuzzy
 * skin, stored per triangle of an object's display mesh in the desktop's own
 * per-triangle codes, so a project's painting survives a round trip untouched.
 * The wire form is documented in `docs/web/worker-protocol.md#painting`.
 */

import type { PaintSubmission } from "../api";
import type { InspectedVolume, ObjectMesh } from "./mesh";

export type PaintKind = "supports" | "seam" | "color" | "fuzzy_skin";

export const PAINT_KINDS: PaintKind[] = ["supports", "seam", "color", "fuzzy_skin"];

/** States of the enforcer kinds: 1 enforces, 2 blocks. Colours use the filament number. */
export const ENFORCE = 1;
export const BLOCK = 2;

/**
 * An item's painting: for each kind it holds, one code per display triangle,
 * empty for an unpainted one. `id` changes with every edit, so items that
 * share a painting stay instances of one object.
 */
export interface ItemPaint {
  id: string;
  layers: Partial<Record<PaintKind, string[]>>;
}

let paintSerial = 0;
export const newPaintId = () => `paint-${++paintSerial}`;

const HEX = "0123456789ABCDEF";

/** The code of an unsplit triangle in one state, as the desktop's CONST_FILAMENTS table writes it. */
export function leafCode(state: number): string {
  if (state <= 0) return "";
  if (state < 3) return HEX[state << 2];
  if (state <= 17) return `${HEX[state - 3]}C`;
  return `${HEX[state - 18]}FC`;
}

/**
 * The state that covers most of a triangle. A split triangle's children share
 * their parent's area equally, which is close enough to show a desktop
 * painting whose triangles the browser does not subdivide. Codes are read from
 * the last character, as the engine reads them.
 */
export function dominantState(code: string): number {
  if (code === "") return 0;
  let position = code.length;
  const next = () => (position > 0 ? HEX.indexOf(code[--position]) : -1);
  const area = new Map<number, number>();
  const visit = (weight: number): boolean => {
    const nibble = next();
    if (nibble < 0) return false;
    const split = nibble & 0b11;
    if (split !== 0) {
      for (let child = 0; child <= split; child++) if (!visit(weight / (split + 1))) return false;
      return true;
    }
    let state = nibble >> 2;
    if (state === 0b11) {
      const low = next();
      if (low < 0) return false;
      state = low === 0b1111 ? next() + 18 : low + 3;
    }
    area.set(state, (area.get(state) ?? 0) + weight);
    return true;
  };
  if (!visit(1)) return 0;
  let best = 0;
  let bestArea = -1;
  for (const [state, covered] of area) if (covered > bestArea) [best, bestArea] = [state, covered];
  return best;
}

/** Parse the wire form into one code per triangle. */
export function decodeLayer(encoded: string, triangles: number): string[] {
  const codes = new Array<string>(triangles).fill("");
  if (!encoded) return codes;
  for (const run of encoded.split(";")) {
    const colon = run.indexOf(":");
    let triangle = Number(run.slice(0, colon));
    for (const code of run.slice(colon + 1).split(",")) {
      if (triangle < triangles) codes[triangle] = code;
      triangle++;
    }
  }
  return codes;
}

/**
 * The wire form of one code per triangle: runs of consecutive codes, each
 * opened by its first triangle's index. Short gaps stay inside a run.
 */
export function encodeLayer(codes: string[], start = 0, end = codes.length): string {
  const runs: string[] = [];
  let run: string[] = [];
  let open = -1;
  let gap = 0;
  for (let triangle = start; triangle < end; triangle++) {
    const code = codes[triangle];
    if (!code || code === "0") {
      gap++;
      continue;
    }
    if (open >= 0 && gap <= 3) {
      for (; gap > 0; gap--) run.push("");
    } else {
      if (open >= 0) runs.push(`${open - start}:${run.join(",")}`);
      run = [];
      open = triangle;
    }
    gap = 0;
    run.push(code);
  }
  if (open >= 0) runs.push(`${open - start}:${run.join(",")}`);
  return runs.join(";");
}

/** An object's display mesh concatenates its parts; find the part a triangle belongs to. */
function paintedParts(volumes: InspectedVolume[] | undefined, triangles: number) {
  const parts = (volumes ?? []).filter((volume) => volume.triangle_offset !== undefined && volume.triangle_count !== undefined);
  // A mesh upload has one part and older inspections list none.
  return parts.length > 0 ? parts : [{ index: 0, triangle_offset: 0, triangle_count: triangles } as InspectedVolume];
}

/** Load the painting a model file carries, as the inspection listed it. */
export function paintFromInspection(volumes: InspectedVolume[] | undefined, triangles: number): ItemPaint | undefined {
  const layers: ItemPaint["layers"] = {};
  for (const volume of volumes ?? []) {
    if (!volume.paint || volume.triangle_offset === undefined) continue;
    for (const kind of PAINT_KINDS) {
      const encoded = volume.paint[kind];
      if (!encoded) continue;
      const codes = (layers[kind] ??= new Array<string>(triangles).fill(""));
      decodeLayer(encoded, volume.triangle_count ?? 0).forEach((code, triangle) => {
        codes[volume.triangle_offset! + triangle] = code;
      });
    }
  }
  return Object.keys(layers).length > 0 ? { id: newPaintId(), layers } : undefined;
}

const submitted = new WeakMap<ItemPaint, PaintSubmission[]>();

/**
 * The request's `paint` for an item: every part, with each kind the item
 * holds, so a cleared kind is sent empty and replaces the file's.
 */
export function paintSubmission(paint: ItemPaint | undefined, volumes: InspectedVolume[] | undefined): PaintSubmission[] {
  if (!paint) return [];
  const cached = submitted.get(paint);
  if (cached) return cached;
  const kinds = PAINT_KINDS.filter((kind) => paint.layers[kind]);
  const triangles = Math.max(0, ...kinds.map((kind) => paint.layers[kind]!.length));
  const result = kinds.length === 0 ? [] : paintedParts(volumes, triangles).map((volume) => {
    const entry: PaintSubmission = { volume: volume.index };
    const from = volume.triangle_offset!;
    const to = from + volume.triangle_count!;
    for (const kind of kinds) entry[kind] = encodeLayer(paint.layers[kind]!, from, to);
    return entry;
  });
  submitted.set(paint, result);
  return result;
}

/** Per-triangle display states of one kind. */
export function layerStates(paint: ItemPaint | undefined, kind: PaintKind, triangles: number): Uint8Array | null {
  const codes = paint?.layers[kind];
  if (!codes) return null;
  const states = new Uint8Array(triangles);
  let any = false;
  for (let triangle = 0; triangle < triangles; triangle++) {
    const state = dominantState(codes[triangle] ?? "");
    states[triangle] = state;
    any ||= state !== 0;
  }
  return any ? states : null;
}

interface MeshTopology {
  centroids: Float32Array;
  normals: Float32Array;
  /** Triangles sharing each edge, as a flat list of neighbour triangles per triangle. */
  neighbours: Int32Array[];
}

const topologies = new WeakMap<ObjectMesh, MeshTopology>();

function topology(mesh: ObjectMesh): MeshTopology {
  const known = topologies.get(mesh);
  if (known) return known;
  const { positions: p, indices } = mesh;
  const count = indices.length / 3;
  const centroids = new Float32Array(count * 3);
  const normals = new Float32Array(count * 3);
  const edges = new Map<string, number[]>();
  for (let t = 0; t < count; t++) {
    const [a, b, c] = [indices[3 * t], indices[3 * t + 1], indices[3 * t + 2]];
    for (let axis = 0; axis < 3; axis++) centroids[3 * t + axis] = (p[3 * a + axis] + p[3 * b + axis] + p[3 * c + axis]) / 3;
    const u = [p[3 * b] - p[3 * a], p[3 * b + 1] - p[3 * a + 1], p[3 * b + 2] - p[3 * a + 2]];
    const v = [p[3 * c] - p[3 * a], p[3 * c + 1] - p[3 * a + 1], p[3 * c + 2] - p[3 * a + 2]];
    const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    const length = Math.hypot(n[0], n[1], n[2]) || 1;
    for (let axis = 0; axis < 3; axis++) normals[3 * t + axis] = n[axis] / length;
    for (const [x, y] of [[a, b], [b, c], [c, a]]) {
      const key = x < y ? `${x},${y}` : `${y},${x}`;
      const shared = edges.get(key);
      if (shared) shared.push(t);
      else edges.set(key, [t]);
    }
  }
  const lists: number[][] = Array.from({ length: count }, () => []);
  for (const shared of edges.values())
    for (const t of shared) for (const other of shared) if (other !== t) lists[t].push(other);
  const built = { centroids, normals, neighbours: lists.map((list) => Int32Array.from(list)) };
  topologies.set(mesh, built);
  return built;
}

/** A brush stroke or fill at one point of an item, in the item's own (object) coordinates. */
export interface PaintStroke {
  kind: PaintKind;
  /** The state painted; 0 erases. */
  state: number;
  triangle: number;
  point: [number, number, number];
  /** Brush radius in object millimetres; ignored by a fill. */
  radius: number;
  /** Fill instead of brushing: spread across faces within `angle` degrees of each other. */
  fill?: boolean;
  angle?: number;
}

/**
 * Apply a stroke and return the new painting, or the same one when nothing
 * changed. A brush paints every triangle whose centroid lies inside its
 * sphere, and always the one under the pointer; a fill spreads from that
 * triangle to its neighbours while the faces stay within the angle, as the
 * desktop's smart fill does. Painted triangles become unsplit leaves.
 */
export function applyStroke(paint: ItemPaint | undefined, mesh: ObjectMesh, stroke: PaintStroke): ItemPaint | undefined {
  const count = mesh.indices.length / 3;
  if (stroke.triangle < 0 || stroke.triangle >= count) return paint;
  const { centroids, normals, neighbours } = topology(mesh);
  const code = leafCode(stroke.state);
  const current = paint?.layers[stroke.kind];
  const touched: number[] = [];
  if (stroke.fill) {
    const limit = Math.cos(((stroke.angle ?? 30) * Math.PI) / 180);
    const seen = new Uint8Array(count);
    const queue = [stroke.triangle];
    seen[stroke.triangle] = 1;
    while (queue.length > 0) {
      const t = queue.pop()!;
      touched.push(t);
      for (const other of neighbours[t]) {
        if (seen[other]) continue;
        const dot = normals[3 * t] * normals[3 * other] + normals[3 * t + 1] * normals[3 * other + 1] + normals[3 * t + 2] * normals[3 * other + 2];
        if (dot >= limit) {
          seen[other] = 1;
          queue.push(other);
        }
      }
    }
  } else {
    const [x, y, z] = stroke.point;
    const squared = stroke.radius * stroke.radius;
    touched.push(stroke.triangle);
    for (let t = 0; t < count; t++) {
      if (t === stroke.triangle) continue;
      const dx = centroids[3 * t] - x, dy = centroids[3 * t + 1] - y, dz = centroids[3 * t + 2] - z;
      if (dx * dx + dy * dy + dz * dz <= squared) touched.push(t);
    }
  }
  if (touched.every((t) => (current?.[t] ?? "") === code)) return paint;
  const codes = current ? [...current] : new Array<string>(count).fill("");
  for (const t of touched) codes[t] = code;
  return { id: newPaintId(), layers: { ...paint?.layers, [stroke.kind]: codes } };
}

/** Remove one kind of painting from an item, keeping the kind so the file's own is cleared too. */
export function clearLayer(paint: ItemPaint | undefined, kind: PaintKind, triangles: number): ItemPaint | undefined {
  if (!paint?.layers[kind]?.some((code) => code !== "")) return paint;
  return { id: newPaintId(), layers: { ...paint.layers, [kind]: new Array<string>(triangles).fill("") } };
}

/** The highest filament any face of the item is painted with, 0 for none. */
export function highestPaintedFilament(paint: ItemPaint | undefined): number {
  const codes = paint?.layers.color;
  if (!codes) return 0;
  let highest = 0;
  for (const code of codes) if (code) highest = Math.max(highest, highestState(code));
  return highest;
}

/** The highest state a code holds anywhere, not only where it dominates. */
function highestState(code: string): number {
  let highest = 0;
  for (let position = code.length - 1; position >= 0; position--) {
    const nibble = HEX.indexOf(code[position]);
    if ((nibble & 0b11) !== 0) continue;
    let state = nibble >> 2;
    if (state === 0b11) {
      const low = HEX.indexOf(code[--position]);
      state = low === 0b1111 ? HEX.indexOf(code[--position]) + 18 : low + 3;
    }
    highest = Math.max(highest, state);
  }
  return highest;
}
