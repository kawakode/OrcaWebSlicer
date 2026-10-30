/**
 * Object tools that need no worker: laying an object on a face and measuring
 * only change or read a placement. Cut and repair change geometry and run in
 * a worker; this module builds their requests and places what comes back.
 */

import { Euler, Quaternion, Vector3 } from "three";

import { ARRANGE_SPACING, itemBounds, itemMatrix, rowMajor, type PlateItem } from "./plate";

const DOWN = new Vector3(0, 0, -1);
const degrees = (radians: number) => Math.round(((radians * 180) / Math.PI) * 1e6) / 1e6;

/**
 * The rotation that turns the face with this plate-space normal straight down,
 * so the item rests on it. Uniform scale leaves normals unchanged, so the
 * current rotation alone is composed with the turn.
 */
export function layOnFace(item: PlateItem, normal: Vector3): [number, number, number] {
  const [rx, ry, rz] = item.rotation.map((value) => (value * Math.PI) / 180);
  const current = new Quaternion().setFromEuler(new Euler(rx, ry, rz, "XYZ"));
  const turn = new Quaternion().setFromUnitVectors(normal.clone().normalize(), DOWN);
  const euler = new Euler().setFromQuaternion(turn.multiply(current), "XYZ");
  return [degrees(euler.x), degrees(euler.y), degrees(euler.z)];
}

export interface Measurement {
  distance: number;
  dx: number;
  dy: number;
  dz: number;
}

export function measure(from: Vector3, to: Vector3): Measurement {
  return { distance: from.distanceTo(to), dx: Math.abs(to.x - from.x), dy: Math.abs(to.y - from.y), dz: Math.abs(to.z - from.z) };
}

export type CutKeep = "both" | "upper" | "lower";

export interface EditSubmission {
  tool: "cut" | "repair";
  object: number;
  /** The item's row-major placement, so the worker edits it where it stands. */
  transform: number[];
  cut?: { point: number[]; normal: number[]; keep: CutKeep; flip_upper: boolean };
}

/** A horizontal cut of an item at `height` millimetres above the bed. */
export function cutSubmission(item: PlateItem, height: number, keep: CutKeep, flipUpper: boolean): EditSubmission {
  const box = itemBounds(item);
  return {
    tool: "cut",
    object: item.objectIndex,
    transform: rowMajor(itemMatrix(item)),
    cut: { point: [(box.minX + box.maxX) / 2, (box.minY + box.maxY) / 2, height], normal: [0, 0, 1], keep, flip_upper: flipUpper },
  };
}

export function repairSubmission(item: PlateItem): EditSubmission {
  return { tool: "repair", object: item.objectIndex, transform: rowMajor(itemMatrix(item)) };
}

/** Whether a horizontal cut at `height` crosses the item. */
export function cutsItem(item: PlateItem, height: number): boolean {
  const { minZ, maxZ } = itemBounds(item);
  return Number.isFinite(height) && height > minZ && height < maxZ;
}

/**
 * Lay edited parts side by side: the first keeps its place and each next one
 * starts past the previous, so a cut's upper half no longer sits inside its
 * lower half once both rest on the bed.
 */
export function spreadParts(parts: PlateItem[]): PlateItem[] {
  const placed: PlateItem[] = [];
  for (const part of parts) {
    const previous = placed[placed.length - 1];
    if (!previous) {
      placed.push(part);
      continue;
    }
    const left = itemBounds(part).minX;
    const right = itemBounds(previous).maxX;
    placed.push({ ...part, x: part.x + right + ARRANGE_SPACING - left });
  }
  return placed;
}

/**
 * Give edited parts the overrides of the object they came from. Height ranges
 * are measured from the object's bottom, so only a repair keeps them.
 */
export function inheritOverrides(item: PlateItem, tool: EditSubmission["tool"], parts: PlateItem[]): PlateItem[] {
  return parts.map((part) => ({
    ...part,
    settings: item.settings,
    layerRanges: tool === "repair" ? item.layerRanges : undefined,
  }));
}
