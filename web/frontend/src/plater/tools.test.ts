import { describe, expect, it } from "vitest";
import { Euler, Quaternion, Vector3 } from "three";

import { itemBounds, type PlateItem } from "./plate";
import { cutsItem, cutSubmission, inheritOverrides, layOnFace, measure, repairSubmission, spreadParts } from "./tools";

// A 20 mm cube from 0 to 20 on every axis, as tests/data/20mm_cube.obj is.
const CUBE = {
  positions: new Float32Array([0, 0, 0, 20, 0, 0, 20, 20, 0, 0, 20, 0, 0, 0, 20, 20, 0, 20, 20, 20, 20, 0, 20, 20]),
  indices: new Uint32Array([0, 1, 2]),
};

function cube(id: string, overrides: Partial<PlateItem> = {}): PlateItem {
  return {
    id,
    uploadId: "upload",
    objectIndex: 0,
    name: id,
    mesh: CUBE,
    center: new Vector3(10, 10, 10),
    x: 100,
    y: 100,
    rotation: [0, 0, 0],
    scale: 1,
    ...overrides,
  };
}

/** Where a plate-space direction points once the item takes this rotation. */
function rotated(rotation: [number, number, number], direction: Vector3) {
  const [rx, ry, rz] = rotation.map((value) => (value * Math.PI) / 180);
  return direction.clone().applyQuaternion(new Quaternion().setFromEuler(new Euler(rx, ry, rz, "XYZ")));
}

describe("object tools", () => {
  it("turns a picked face straight down", () => {
    const rotation = layOnFace(cube("a"), new Vector3(1, 0, 0));
    const normal = rotated(rotation, new Vector3(1, 0, 0));
    expect(normal.x).toBeCloseTo(0);
    expect(normal.y).toBeCloseTo(0);
    expect(normal.z).toBeCloseTo(-1);
  });

  it("composes the turn with the item's current rotation", () => {
    const item = cube("a", { rotation: [0, 0, 90] });
    // The object's local +Y face now points along plate -X.
    const picked = rotated(item.rotation, new Vector3(0, 1, 0));
    const rotation = layOnFace(item, picked);
    const normal = rotated(rotation, new Vector3(0, 1, 0));
    expect(normal.z).toBeCloseTo(-1);
  });

  it("leaves a face that already rests on the bed alone", () => {
    expect(layOnFace(cube("a"), new Vector3(0, 0, -1)).map((value) => value + 0)).toEqual([0, 0, 0]);
  });

  it("measures a distance and its components", () => {
    const result = measure(new Vector3(0, 0, 0), new Vector3(3, -4, 12));
    expect(result).toEqual({ distance: 13, dx: 3, dy: 4, dz: 12 });
  });

  it("cuts horizontally through the item where it stands", () => {
    const item = cube("a", { x: 60, y: 70 });
    const submission = cutSubmission(item, 5, "upper", true);
    expect(submission.tool).toBe("cut");
    expect(submission.cut).toEqual({ point: [60, 70, 5], normal: [0, 0, 1], keep: "upper", flip_upper: true });
    // The row-major placement puts the pivot at X 60, Y 70, resting on the bed.
    expect(submission.transform.slice(0, 4)).toEqual([1, 0, 0, 50]);
    expect(submission.transform[7]).toBe(60);
    expect(repairSubmission(item).cut).toBeUndefined();
    expect(cutsItem(item, 5)).toBe(true);
    expect(cutsItem(item, 0)).toBe(false);
    expect(cutsItem(item, 20)).toBe(false);
  });

  it("lays parts side by side without overlap", () => {
    const [first, second] = spreadParts([cube("upper"), cube("lower")]);
    expect(first.x).toBe(100);
    expect(itemBounds(second).minX).toBeCloseTo(itemBounds(first).maxX + 6);
  });

  it("passes an object's overrides on to its parts", () => {
    const layerRanges = [{ minZ: 5, maxZ: 10, settings: { sparse_infill_density: "50%" } }];
    const item = cube("a", { settings: { wall_loops: "5" }, layerRanges });
    const [cut] = inheritOverrides(item, "cut", [cube("upper")]);
    expect(cut.settings).toEqual({ wall_loops: "5" });
    expect(cut.layerRanges).toBeUndefined();
    expect(inheritOverrides(item, "repair", [cube("whole")])[0].layerRanges).toEqual(layerRanges);
  });
});
