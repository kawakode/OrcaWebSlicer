import { describe, expect, it } from "vitest";
import { Vector3 } from "three";

import {
  addToPlates,
  arrange,
  fitsBed,
  itemBounds,
  itemMatrix,
  parseBed,
  placementFromMatrix,
  plateLabel,
  plateObjects,
  rowMajor,
  type Plate,
  type PlateItem,
} from "./plate";

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

  it("keeps items with their own overrides apart and drops blank ones", () => {
    const ranged: Partial<PlateItem> = { layerRanges: [{ minZ: 5, maxZ: 10, settings: { sparse_infill_density: "40%" } }, { minZ: 12, maxZ: 15, settings: { wall_loops: "" } }] };
    const objects = plateObjects([
      cube("plain"),
      cube("blank", { settings: { wall_loops: " " } }),
      cube("walls", { settings: { wall_loops: "5" } }),
      cube("walls-copy", { settings: { wall_loops: "5" } }),
      cube("ranged", ranged),
    ]);
    expect(objects).toHaveLength(3);
    expect(objects[0]).not.toHaveProperty("settings");
    expect(objects[0].instances).toHaveLength(2);
    expect(objects[1].settings).toEqual({ wall_loops: "5" });
    expect(objects[1].instances).toHaveLength(2);
    expect(objects[2].layer_ranges).toEqual([{ min_z: 5, max_z: 10, settings: { sparse_infill_density: "40%" } }]);
  });
});

describe("project plates", () => {
  let created = 0;
  const plate = (overrides: Partial<Plate> = {}): Plate => ({ id: `plate-${++created}`, name: "", items: [], job: null, ...overrides });

  it("lands a project's plates in order from the plate shown", () => {
    const start = [plate({ items: [cube("kept")] }), plate()];
    const placed = addToPlates(
      start,
      1,
      [
        { item: cube("a"), plate: 1 },
        { item: cube("b"), plate: 2 },
        { item: cube("c"), plate: 2 },
      ],
      ["Brackets", ""],
      () => plate(),
    );
    expect(placed).toHaveLength(3);
    expect(placed[0].items.map((item) => item.id)).toEqual(["kept"]);
    expect(placed[1].items.map((item) => item.id)).toEqual(["a"]);
    expect(placed[2].items.map((item) => item.id)).toEqual(["b", "c"]);
    // The file's plate names and numbers follow its plates.
    expect(placed[1].name).toBe("Brackets");
    expect(placed[1].sourcePlate).toBe(1);
    expect(placed[2].sourcePlate).toBe(2);
    expect(plateLabel(placed[2], 2)).toBe("Plate 3");
    // The input is left untouched.
    expect(start[1].items).toHaveLength(0);
  });

  it("puts a mesh on the plate shown without naming it", () => {
    const placed = addToPlates([plate({ name: "Mine" })], 0, [{ item: cube("a"), plate: 1 }], [], () => plate());
    expect(placed).toHaveLength(1);
    expect(placed[0].name).toBe("Mine");
    expect(placed[0].sourcePlate).toBeUndefined();
  });

  it("creates the empty plates a project declares", () => {
    const placed = addToPlates([plate()], 0, [{ item: cube("a"), plate: 1 }], ["", "", ""], () => plate());
    expect(placed.map((entry) => entry.items.length)).toEqual([1, 0, 0]);
  });
});
