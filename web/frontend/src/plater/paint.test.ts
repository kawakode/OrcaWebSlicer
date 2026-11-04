import { describe, expect, it } from "vitest";
import { Vector3 } from "three";

import type { InspectedVolume } from "./mesh";
import {
  applyStroke,
  BLOCK,
  clearLayer,
  decodeLayer,
  dominantState,
  encodeLayer,
  ENFORCE,
  highestPaintedFilament,
  leafCode,
  paintFromInspection,
  paintSubmission,
} from "./paint";
import { highestExtruder, plateObjects, type PlateItem } from "./plate";

// A 20 mm square in the XY plane, two triangles, next to a wall standing on
// its right edge, two more, so a fill can tell the faces apart.
const MESH = {
  positions: new Float32Array([0, 0, 0, 20, 0, 0, 20, 20, 0, 0, 20, 0, 20, 0, 20, 20, 20, 20]),
  indices: new Uint32Array([0, 1, 2, 0, 2, 3, 1, 4, 5, 1, 5, 2]),
};

const PARTS: InspectedVolume[] = [
  { index: 0, name: "floor", type: "part", extruder: 0, triangle_offset: 0, triangle_count: 2 },
  { index: 1, name: "modifier", type: "modifier", extruder: 0 },
  { index: 2, name: "wall", type: "part", extruder: 0, triangle_offset: 2, triangle_count: 2 },
];

function item(id: string, overrides: Partial<PlateItem> = {}): PlateItem {
  return {
    id,
    uploadId: "upload",
    objectIndex: 0,
    name: id,
    mesh: MESH,
    center: new Vector3(10, 10, 10),
    x: 100,
    y: 100,
    rotation: [0, 0, 0],
    scale: 1,
    volumes: PARTS,
    ...overrides,
  };
}

describe("paint codes", () => {
  it("writes the desktop's codes for unsplit triangles", () => {
    expect([0, 1, 2, 3, 4, 16, 17, 18].map(leafCode)).toEqual(["", "4", "8", "0C", "1C", "DC", "EC", "0FC"]);
    for (const state of [1, 2, 3, 9, 16, 18]) expect(dominantState(leafCode(state))).toBe(state);
  });

  it("reads the state covering most of a split triangle", () => {
    // Read from the end: a 3-side split ("3") into four leaves, three
    // enforcers ("4") and one blocker ("8").
    expect(dominantState("4484" + "3")).toBe(ENFORCE);
    expect(dominantState("8884" + "3")).toBe(BLOCK);
  });

  it("round-trips per-triangle codes through runs", () => {
    const codes = ["", "4", "4", "", "", "8", "", "", "", "", "", "0C", "44843"];
    const encoded = encodeLayer(codes);
    expect(encoded).toBe("1:4,4,,,8;11:0C,44843");
    expect(decodeLayer(encoded, codes.length)).toEqual(codes);
    expect(encodeLayer(codes, 5, 12)).toBe("0:8;6:0C");
    expect(encodeLayer(["", ""])).toBe("");
  });
});

describe("painting an item", () => {
  it("brushes the triangles inside the sphere and fills across flat faces", () => {
    const brushed = applyStroke(undefined, MESH, {
      kind: "supports",
      state: ENFORCE,
      triangle: 0,
      point: [15, 5, 0],
      radius: 1,
    });
    expect(brushed!.layers.supports).toEqual(["4", "", "", ""]);

    const filled = applyStroke(brushed, MESH, { kind: "seam", state: BLOCK, triangle: 0, point: [0, 0, 0], radius: 0, fill: true });
    expect(filled!.layers.seam).toEqual(["8", "8", "", ""]);
    expect(filled!.layers.supports).toBe(brushed!.layers.supports);
    expect(filled!.id).not.toBe(brushed!.id);

    // Painting what is already there keeps the same painting.
    expect(applyStroke(filled, MESH, { kind: "seam", state: BLOCK, triangle: 1, point: [0, 0, 0], radius: 0, fill: true })).toBe(filled);
    expect(clearLayer(filled, "seam", 4)!.layers.seam).toEqual(["", "", "", ""]);
  });

  it("loads a file's painting and sends it back per part", () => {
    const volumes: InspectedVolume[] = [
      { ...PARTS[0], paint: { color: "1:0C" } },
      PARTS[1],
      { ...PARTS[2], paint: { color: "0:44843", supports: "1:8" } },
    ];
    const paint = paintFromInspection(volumes, 4)!;
    expect(paint.layers.color).toEqual(["", "0C", "44843", ""]);
    expect(paint.layers.supports).toEqual(["", "", "", "8"]);
    expect(highestPaintedFilament(paint)).toBe(3);
    // Every part is sent with every kind the item holds, so the floor's
    // supports are cleared explicitly rather than left to the file.
    expect(paintSubmission(paint, volumes)).toEqual([
      { volume: 0, supports: "", color: "1:0C" },
      { volume: 2, supports: "1:8", color: "0:44843" },
    ]);
    expect(paintFromInspection(PARTS, 4)).toBeUndefined();
  });

  it("keeps instances with one painting together and counts painted filaments", () => {
    const paint = applyStroke(undefined, MESH, { kind: "color", state: 4, triangle: 0, point: [0, 0, 0], radius: 0 });
    const objects = plateObjects([item("a", { paint }), item("b", { paint, x: 60 }), item("c", { x: 30 })]);
    expect(objects).toHaveLength(2);
    expect(objects[0].instances).toHaveLength(2);
    expect(objects[0].paint).toEqual([
      { volume: 0, color: "0:1C" },
      { volume: 2, color: "" },
    ]);
    expect(objects[1].paint).toBeUndefined();
    expect(highestExtruder([item("a", { paint })])).toBe(4);
  });
});
