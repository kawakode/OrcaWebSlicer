/**
 * The plater's state, kept free of rendering so placement can be tested on
 * its own. Every length is in millimetres, matching what the worker applies.
 */

import { Euler, Matrix4, Quaternion, Vector3 } from "three";

import type { PlacedObjectSubmission } from "../api";
import { declared } from "../settings/catalog";
import type { Job } from "../types";
import type { ObjectMesh } from "./mesh";

/** Settings for part of an object, between two heights above its bottom. */
export interface LayerRange {
  minZ: number;
  maxZ: number;
  settings: Record<string, string>;
}

export interface Bed {
  /** Printable area outline, counter-clockwise, in millimetres. */
  outline: [number, number][];
  height: number;
}

export interface PlateItem {
  id: string;
  uploadId: string;
  objectIndex: number;
  name: string;
  mesh: ObjectMesh;
  /** Object-space bounds of the mesh, the pivot for rotation and scale. */
  center: Vector3;
  /** Where the pivot sits on the bed. Z is derived so the item rests on it. */
  x: number;
  y: number;
  /** Rotation in degrees about X, then Y, then Z. */
  rotation: [number, number, number];
  /** Uniform scale factor. */
  scale: number;
  /** Overrides of the plate's settings for this object only. */
  settings?: Record<string, string>;
  layerRanges?: LayerRange[];
}

/** Spacing the arrangement keeps between items, in millimetres. */
export const ARRANGE_SPACING = 6;

/** Parse a profile's `printable_area` ("0x0", "220x0", ...) and height. */
export function parseBed(area: string[] | string, height: string): Bed {
  const points = (Array.isArray(area) ? area : area.split(",")).map((point) => {
    const [x, y] = point.split("x").map(Number);
    return [x, y] as [number, number];
  });
  const outline = points.filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  return { outline: outline.length >= 3 ? outline : [[0, 0], [200, 0], [200, 200], [0, 200]], height: Number(height) || 0 };
}

export function bedBounds(bed: Bed) {
  const xs = bed.outline.map(([x]) => x);
  const ys = bed.outline.map(([, y]) => y);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
}

function rotationMatrix(item: PlateItem): Matrix4 {
  const [rx, ry, rz] = item.rotation.map((degrees) => (degrees * Math.PI) / 180);
  return new Matrix4().makeRotationFromQuaternion(new Quaternion().setFromEuler(new Euler(rx, ry, rz, "XYZ")));
}

/** The transform without the drop onto the bed: pivot to origin, scale, rotate, place. */
function unrestedMatrix(item: PlateItem): Matrix4 {
  return new Matrix4()
    .makeTranslation(item.x, item.y, 0)
    .multiply(rotationMatrix(item))
    .multiply(new Matrix4().makeScale(item.scale, item.scale, item.scale))
    .multiply(new Matrix4().makeTranslation(-item.center.x, -item.center.y, -item.center.z));
}

/** World-space bounds of an item under a transform, from its actual vertices. */
function transformedBounds(mesh: ObjectMesh, matrix: Matrix4) {
  const e = matrix.elements;
  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  const p = mesh.positions;
  for (let i = 0; i < p.length; i += 3) {
    const x = e[0] * p[i] + e[4] * p[i + 1] + e[8] * p[i + 2] + e[12];
    const y = e[1] * p[i] + e[5] * p[i + 1] + e[9] * p[i + 2] + e[13];
    const z = e[2] * p[i] + e[6] * p[i + 1] + e[10] * p[i + 2] + e[14];
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    if (z > maxZ) maxZ = z;
  }
  return { minX, minY, minZ, maxX, maxY, maxZ };
}

/**
 * The item's full transform, resting on the bed: the lowest vertex sits at
 * Z 0, which is also where the worker would drop a floating object.
 */
export function itemMatrix(item: PlateItem): Matrix4 {
  const matrix = unrestedMatrix(item);
  const { minZ } = transformedBounds(item.mesh, matrix);
  return new Matrix4().makeTranslation(0, 0, Number.isFinite(minZ) ? -minZ : 0).multiply(matrix);
}

export function itemBounds(item: PlateItem) {
  return transformedBounds(item.mesh, itemMatrix(item));
}

/** The row-major 4x4 matrix the worker expects. three.js stores column-major. */
export function rowMajor(matrix: Matrix4): number[] {
  return matrix.clone().transpose().toArray();
}

/** Recover placement from a matrix a project file declared. */
export function placementFromMatrix(
  matrix: number[],
  center: Vector3,
): Pick<PlateItem, "x" | "y" | "rotation" | "scale"> {
  const m = new Matrix4().fromArray(matrix).transpose();
  const position = new Vector3();
  const quaternion = new Quaternion();
  const scale = new Vector3();
  m.decompose(position, quaternion, scale);
  const euler = new Euler().setFromQuaternion(quaternion, "XYZ");
  const pivot = center.clone().applyMatrix4(m);
  const degrees = (radians: number) => Math.round(((radians * 180) / Math.PI) * 1e6) / 1e6;
  return {
    x: pivot.x,
    y: pivot.y,
    rotation: [degrees(euler.x), degrees(euler.y), degrees(euler.z)],
    // The plater offers uniform scale only, so a non-uniform file scale is
    // reduced to its mean; the file's own placement is kept by the worker only
    // if the item is left untouched.
    scale: (scale.x + scale.y + scale.z) / 3,
  };
}

/** Whether an item lies inside the printable volume, as the worker checks it. */
export function fitsBed(item: PlateItem, bed: Bed): boolean {
  const bounds = itemBounds(item);
  const { minX, maxX, minY, maxY } = bedBounds(bed);
  const tolerance = 0.05;
  return (
    bounds.minX >= minX - tolerance &&
    bounds.maxX <= maxX + tolerance &&
    bounds.minY >= minY - tolerance &&
    bounds.maxY <= maxY + tolerance &&
    (bed.height <= 0 || bounds.maxZ <= bed.height + tolerance)
  );
}

/**
 * Shelf-pack every item's footprint into the bed, centred as a block. This is
 * the browser's own arrangement: the resulting placement is submitted as
 * explicit transforms, so what is shown is what is sliced.
 */
export function arrange(items: PlateItem[], bed: Bed): PlateItem[] {
  const bounds = bedBounds(bed);
  const width = bounds.maxX - bounds.minX;
  const footprints = items
    .map((item) => ({ item, box: itemBounds(item) }))
    .map(({ item, box }) => ({
      item,
      box,
      w: box.maxX - box.minX,
      h: box.maxY - box.minY,
    }))
    .sort((a, b) => b.h - a.h || b.w - a.w);

  type Placed = { item: PlateItem; box: ReturnType<typeof itemBounds>; left: number; bottom: number; w: number; h: number };
  const placed: Placed[] = [];
  let cursorX = 0;
  let cursorY = 0;
  let shelf = 0;
  let usedWidth = 0;
  for (const footprint of footprints) {
    if (cursorX > 0 && cursorX + footprint.w > width) {
      cursorX = 0;
      cursorY += shelf + ARRANGE_SPACING;
      shelf = 0;
    }
    placed.push({ ...footprint, left: cursorX, bottom: cursorY });
    usedWidth = Math.max(usedWidth, cursorX + footprint.w);
    cursorX += footprint.w + ARRANGE_SPACING;
    shelf = Math.max(shelf, footprint.h);
  }
  const usedHeight = cursorY + shelf;
  const originX = (bounds.minX + bounds.maxX - usedWidth) / 2;
  const originY = (bounds.minY + bounds.maxY - usedHeight) / 2;
  const moved = new Map<string, PlateItem>();
  for (const { item, box, left, bottom } of placed) {
    // Shift the pivot by the same amount the footprint moves.
    moved.set(item.id, {
      ...item,
      x: item.x + originX + left - box.minX,
      y: item.y + originY + bottom - box.minY,
    });
  }
  return items.map((item) => moved.get(item.id) ?? item);
}

/** Group items into the request's objects: one entry per upload object. */
/**
 * The plate as the API takes it. Items of one upload object with the same
 * overrides are instances of one object; different overrides make them
 * separate objects, since the engine configures a whole object at once.
 */
export function plateObjects(items: PlateItem[]): PlacedObjectSubmission[] {
  const grouped = new Map<string, PlacedObjectSubmission>();
  for (const item of items) {
    const settings = declared(item.settings);
    const layerRanges = (item.layerRanges ?? [])
      .map((range) => ({ min_z: range.minZ, max_z: range.maxZ, settings: declared(range.settings) }))
      .filter((range) => Object.keys(range.settings).length > 0);
    const key = JSON.stringify([item.uploadId, item.objectIndex, settings, layerRanges]);
    let entry = grouped.get(key);
    if (!entry) {
      entry = { upload_id: item.uploadId, object: item.objectIndex, instances: [] };
      if (Object.keys(settings).length > 0) entry.settings = settings;
      if (layerRanges.length > 0) entry.layer_ranges = layerRanges;
      grouped.set(key, entry);
    }
    entry.instances.push({ transform: rowMajor(itemMatrix(item)) });
  }
  return [...grouped.values()];
}

/** One plate of the project: what the plater draws and one job slices. */
export interface Plate {
  id: string;
  /** The name a project file gave the plate, or empty. */
  name: string;
  items: PlateItem[];
  /** The plate of the project file it came from, whose own settings apply. */
  sourcePlate?: number;
  /** The plate's latest slice. */
  job: Job | null;
}

export const plateLabel = (plate: Plate, position: number) => plate.name || `Plate ${position + 1}`;

/** An item a model file contributes, with the 1-based plate of that file it sits on. */
export interface LoadedItem {
  item: PlateItem;
  plate: number;
}

/**
 * Put a file's items onto the project's plates. The file's first plate lands on
 * the plate at `start` and each later plate on the next one, created with
 * `create` when the project runs out of plates, so a project archive keeps its
 * plates in order. `names` are the file's own plate names.
 */
export function addToPlates(
  plates: Plate[],
  start: number,
  added: LoadedItem[],
  names: string[],
  create: () => Plate,
): Plate[] {
  const next = plates.map((plate) => ({ ...plate, items: [...plate.items] }));
  const count = Math.max(1, names.length, ...added.map(({ plate }) => plate));
  for (let filePlate = 1; filePlate <= count; filePlate++) {
    const position = start + filePlate - 1;
    while (next.length <= position) next.push(create());
    const target = next[position];
    if (names.length > 0) {
      target.name ||= names[filePlate - 1] ?? "";
      target.sourcePlate ??= filePlate;
    }
    target.items.push(...added.filter(({ plate }) => plate === filePlate).map(({ item }) => item));
  }
  return next;
}
