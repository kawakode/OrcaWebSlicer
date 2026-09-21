/**
 * The millimetre arithmetic the plater and the slice request share.
 *
 * Every matrix here is a column-major 4x4 in millimetres — the layout
 * `docs/web/scene-format.md` publishes and a slice request carries back, so a
 * transform never changes convention between being displayed and being sliced.
 */

export type Matrix = number[];

export const IDENTITY: Matrix = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

export type Vector = [number, number, number];

export interface Box {
  min: Vector;
  max: Vector;
}

/** `a` applied after `b`, i.e. the matrix for `a · b`. */
export function multiply(a: Matrix, b: Matrix): Matrix {
  const product = new Array<number>(16).fill(0);
  for (let column = 0; column < 4; column += 1) {
    for (let row = 0; row < 4; row += 1) {
      let sum = 0;
      for (let step = 0; step < 4; step += 1) sum += a[step * 4 + row] * b[column * 4 + step];
      product[column * 4 + row] = sum;
    }
  }
  return product;
}

export function transformPoint(matrix: Matrix, x: number, y: number, z: number): Vector {
  return [
    matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
    matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
    matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
  ];
}

export function rotationZ(degrees: number): Matrix {
  const radians = (degrees * Math.PI) / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return [cos, sin, 0, 0, -sin, cos, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
}

export function uniformScale(factor: number): Matrix {
  return [factor, 0, 0, 0, 0, factor, 0, 0, 0, 0, factor, 0, 0, 0, 0, 1];
}

/** The same matrix with its translation dropped, keeping rotation and scale. */
export function withoutTranslation(matrix: Matrix): Matrix {
  const copy = matrix.slice();
  copy[12] = copy[13] = copy[14] = 0;
  return copy;
}

export function withTranslation(matrix: Matrix, x: number, y: number, z: number): Matrix {
  const copy = matrix.slice();
  copy[12] = x;
  copy[13] = y;
  copy[14] = z;
  return copy;
}

/** The eight corners of a box, in the order a box mesh below expects. */
export function corners(box: Box): Vector[] {
  const points: Vector[] = [];
  for (const z of [box.min[2], box.max[2]])
    for (const y of [box.min[1], box.max[1]])
      for (const x of [box.min[0], box.max[0]]) points.push([x, y, z]);
  return points;
}

/** The axis-aligned box that contains `box` once `matrix` has placed it. */
export function transformBox(matrix: Matrix, box: Box): Box {
  const min: Vector = [Infinity, Infinity, Infinity];
  const max: Vector = [-Infinity, -Infinity, -Infinity];
  for (const corner of corners(box)) {
    const placed = transformPoint(matrix, corner[0], corner[1], corner[2]);
    for (let axis = 0; axis < 3; axis += 1) {
      min[axis] = Math.min(min[axis], placed[axis]);
      max[axis] = Math.max(max[axis], placed[axis]);
    }
  }
  return { min, max };
}

/** The 12 triangles of a box, as a flat local-frame vertex buffer. */
export function boxMesh(box: Box): Float32Array {
  const point = corners(box);
  // Two triangles per face. Winding is not meaningful here: the renderer
  // shades on the absolute normal, so a box reads the same from either side.
  const faces = [
    [0, 1, 3, 0, 3, 2], // bottom
    [4, 6, 7, 4, 7, 5], // top
    [0, 4, 5, 0, 5, 1], // front
    [2, 3, 7, 2, 7, 6], // back
    [0, 2, 6, 0, 6, 4], // left
    [1, 5, 7, 1, 7, 3], // right
  ].flat();
  const vertices = new Float32Array(faces.length * 3);
  faces.forEach((index, position) => {
    vertices.set(point[index], position * 3);
  });
  return vertices;
}

/** Merges the soup's vertices per cell of a `cells`-wide grid over `box`. */
function cluster(vertices: Float32Array, box: Box, cells: number): Float32Array {
  const vertexCount = vertices.length / 3;
  const size = Math.max(...[0, 1, 2].map((axis) => box.max[axis] - box.min[axis]), 1e-6) / cells;
  const span = cells + 1;
  const cellOf = new Int32Array(vertexCount);
  const clusters = new Map<number, number>();
  const sums: number[] = [];
  for (let vertex = 0; vertex < vertexCount; vertex += 1) {
    let key = 0;
    for (let axis = 2; axis >= 0; axis -= 1) {
      const offset = Math.floor((vertices[vertex * 3 + axis] - box.min[axis]) / size);
      key = key * span + Math.min(cells, Math.max(0, offset));
    }
    let index = clusters.get(key);
    if (index === undefined) {
      index = sums.length / 4;
      clusters.set(key, index);
      sums.push(0, 0, 0, 0);
    }
    cellOf[vertex] = index;
    for (let axis = 0; axis < 3; axis += 1) sums[index * 4 + axis] += vertices[vertex * 3 + axis];
    sums[index * 4 + 3] += 1;
  }

  const kept: number[] = [];
  const seen = new Set<string>();
  for (let vertex = 0; vertex < vertexCount; vertex += 3) {
    const [a, b, c] = [cellOf[vertex], cellOf[vertex + 1], cellOf[vertex + 2]];
    if (a === b || b === c || a === c) continue;
    const key = [a, b, c].sort((left, right) => left - right).join();
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(a, b, c);
  }
  const result = new Float32Array(kept.length * 3);
  kept.forEach((index, position) => {
    for (let axis = 0; axis < 3; axis += 1)
      result[position * 3 + axis] = sums[index * 4 + axis] / sums[index * 4 + 3];
  });
  return result;
}

/**
 * Vertex-clustering decimation of a local-frame triangle soup: vertices are
 * merged per cell of a uniform grid, each at its cell's mean, and triangles
 * that collapse or repeat are dropped. The finest grid that fits `budget` is
 * searched for, so an object over the draw budget keeps its shape instead of
 * turning into its bounding box. Null when no grid fits.
 */
export function decimate(vertices: Float32Array, box: Box, budget: number): Float32Array | null {
  const triangles = vertices.length / 9;
  if (triangles <= budget) return vertices;
  let best: Float32Array | null = null;
  // Fewer cells never keep more triangles, so the finest fitting grid is
  // bisected for; a surface's triangles grow with the square of the cells.
  let low = 2;
  let high = Math.ceil(4 * Math.sqrt(triangles));
  while (low <= high) {
    const cells = Math.floor((low + high) / 2);
    const result = cluster(vertices, box, cells);
    if (result.length / 9 <= budget) {
      best = result;
      low = cells + 1;
    } else {
      high = cells - 1;
    }
  }
  return best && best.length > 0 ? best : null;
}
