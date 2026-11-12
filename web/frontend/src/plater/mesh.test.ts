import { describe, expect, it } from "vitest";

import { shadeMesh, type ObjectMesh } from "./mesh";

/** A unit cube sharing its eight corners, the way the worker sends meshes. */
function cube(): ObjectMesh {
  const positions = new Float32Array([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]);
  // prettier-ignore
  const indices = new Uint32Array([
    0, 2, 1, 0, 3, 2, // bottom
    4, 5, 6, 4, 6, 7, // top
    0, 1, 5, 0, 5, 4, // front
    1, 2, 6, 1, 6, 5, // right
    2, 3, 7, 2, 7, 6, // back
    3, 0, 4, 3, 4, 7, // left
  ]);
  return { positions, indices };
}

/** An n-sided prism side wall: a coarse cylinder with no caps. */
function tube(sides: number): ObjectMesh {
  const positions: number[] = [];
  const indices: number[] = [];
  for (let i = 0; i < sides; i++) {
    const angle = (2 * Math.PI * i) / sides;
    positions.push(Math.cos(angle), Math.sin(angle), 0, Math.cos(angle), Math.sin(angle), 1);
  }
  for (let i = 0; i < sides; i++) {
    const a = 2 * i, b = 2 * ((i + 1) % sides);
    indices.push(a, b, b + 1, a, b + 1, a + 1);
  }
  return { positions: new Float32Array(positions), indices: new Uint32Array(indices) };
}

describe("shadeMesh", () => {
  it("shades every face of a box flat", () => {
    const shaded = shadeMesh(cube());
    // Each corner splits into one vertex per face it touches: 3 x 8.
    expect(shaded.positions.length / 3).toBe(24);
    for (let t = 0; t < 12; t++) {
      const corners = [0, 1, 2].map((c) => shaded.indices[3 * t + c]);
      const normals = corners.map((v) => Array.from(shaded.normals.subarray(3 * v, 3 * v + 3)));
      expect(normals[1]).toEqual(normals[0]);
      expect(normals[2]).toEqual(normals[0]);
      expect(Math.max(...normals[0].map(Math.abs))).toBeCloseTo(1);
    }
  });

  it("keeps triangle order and positions, so picks map to the same triangle", () => {
    const source = cube();
    const shaded = shadeMesh(source);
    for (let i = 0; i < source.indices.length; i++) {
      const from = source.indices[i], to = shaded.indices[i];
      expect(Array.from(shaded.positions.subarray(3 * to, 3 * to + 3))).toEqual(Array.from(source.positions.subarray(3 * from, 3 * from + 3)));
    }
  });

  it("smooths a curved wall below the crease angle", () => {
    const shaded = shadeMesh(tube(32));
    // No splits: the wall is smooth, so each vertex keeps one radial normal.
    expect(shaded.positions.length / 3).toBe(64);
    expect(shaded.normals[0]).toBeCloseTo(1);
    expect(shaded.normals[2]).toBeCloseTo(0);
  });

  it("splits a coarse wall whose facets meet beyond the crease angle", () => {
    expect(shadeMesh(tube(6)).positions.length / 3).toBe(24);
  });
});
