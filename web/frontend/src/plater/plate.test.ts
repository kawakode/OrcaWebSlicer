import { describe, expect, it } from "vitest";
import { Vector3 } from "three";

import { arrange, fitsBed, itemBounds, itemMatrix, parseBed, placementFromMatrix, plateObjects, rowMajor, type PlateItem } from "./plate";

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

const BED = parseBed(["0x0", "220x0", "220x220", "0x220"], "250");

describe("plate placement", () => {
  it("parses a bed outline and height", () => {
    expect(BED.outline).toEqual([[0, 0], [220, 0], [220, 220], [0, 220]]);
    expect(BED.height).toBe(250);
  });

  it("places the pivot and rests the item on the bed", () => {
    const bounds = itemBounds(cube("a"));
    expect(bounds.minX).toBeCloseTo(90);
    expect(bounds.maxX).toBeCloseTo(110);
    expect(bounds.minZ).toBeCloseTo(0);
    expect(bounds.maxZ).toBeCloseTo(20);
  });

  it("rotates and scales about the object's centre", () => {
    const bounds = itemBounds(cube("a", { rotation: [0, 0, 45], scale: 2 }));
    const half = 20 * Math.SQRT2;
    expect(bounds.minX).toBeCloseTo(100 - half);
    expect(bounds.maxY).toBeCloseTo(100 + half);
    expect(bounds.maxZ).toBeCloseTo(40);
  });

  it("serializes the transform row-major with translation in the last column", () => {
    const values = rowMajor(itemMatrix(cube("a")));
    expect(values.slice(12)).toEqual([0, 0, 0, 1]);
    expect(values[3]).toBeCloseTo(90);
    expect(values[7]).toBeCloseTo(90);
    expect(values[11]).toBeCloseTo(0);
  });

  it("round-trips a declared placement", () => {
    const original = cube("a", { x: 50, y: 70, rotation: [0, 0, 30], scale: 1.5 });
    const recovered = placementFromMatrix(rowMajor(itemMatrix(original)), original.center);
    expect(recovered.x).toBeCloseTo(50);
    expect(recovered.y).toBeCloseTo(70);
    expect(recovered.rotation[2]).toBeCloseTo(30);
    expect(recovered.scale).toBeCloseTo(1.5);
  });

  it("detects an item outside the printable volume", () => {
    expect(fitsBed(cube("a"), BED)).toBe(true);
    expect(fitsBed(cube("a", { x: 5 }), BED)).toBe(false);
    expect(fitsBed(cube("a", { scale: 20 }), BED)).toBe(false);
  });

  it("arranges items without overlap inside the bed", () => {
    const items = arrange([cube("a"), cube("b"), cube("c"), cube("d")], BED);
    const boxes = items.map(itemBounds);
    for (const item of items) expect(fitsBed(item, BED)).toBe(true);
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const apart =
          boxes[i].maxX <= boxes[j].minX || boxes[j].maxX <= boxes[i].minX ||
          boxes[i].maxY <= boxes[j].minY || boxes[j].maxY <= boxes[i].minY;
        expect(apart).toBe(true);
      }
    }
  });

  it("groups instances of the same upload object", () => {
    const objects = plateObjects([cube("a"), cube("b"), cube("c", { objectIndex: 1 })]);
    expect(objects).toHaveLength(2);
    expect(objects[0].instances).toHaveLength(2);
    expect(objects[1].object).toBe(1);
  });
});
