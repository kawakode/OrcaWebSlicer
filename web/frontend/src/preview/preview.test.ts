import { describe, expect, it } from "vitest";

import { decodeSegments, pageLayers, roleColor, type PreviewLayer } from "./preview";

function record(values: number[], role: number, tool: number): ArrayBuffer {
  const buffer = new ArrayBuffer(36);
  const view = new DataView(buffer);
  values.forEach((value, index) => view.setFloat32(index * 4, value, true));
  view.setUint8(32, role);
  view.setUint8(33, tool);
  return buffer;
}

describe("preview decoding", () => {
  it("decodes little-endian segment records", () => {
    const first = new Uint8Array(record([1, 2, 0.2, 3, 4, 0.2, 0.45, 0.2], 2, 0));
    const second = new Uint8Array(record([3, 4, 0.2, 5, 6, 0.2, 0.42, 0.2], 1, 1));
    const page = new Uint8Array(72);
    page.set(first, 0);
    page.set(second, 36);
    const decoded = decodeSegments(page.buffer, 36);
    expect(Array.from(decoded.positions.slice(0, 6))).toEqual([1, 2, expect.closeTo(0.2), 3, 4, expect.closeTo(0.2)]);
    expect(Array.from(decoded.roles)).toEqual([2, 1]);
    expect(Array.from(decoded.tools)).toEqual([0, 1]);
    expect(decoded.widths[1]).toBeCloseTo(0.42);
  });

  it("refuses a page that is not whole segments", () => {
    expect(() => decodeSegments(new ArrayBuffer(40), 36)).toThrow();
  });

  it("pages layers under a byte budget without splitting a layer", () => {
    const layers: PreviewLayer[] = [10, 10, 30, 5].map((count, index) => ({
      index,
      z: 0.2 * (index + 1),
      height: 0.2,
      offset: 0,
      count,
    }));
    expect(pageLayers(layers, 36, 20 * 36)).toEqual([[0, 1], [2, 2], [3, 3]]);
    expect(pageLayers([], 36, 100)).toEqual([]);
  });

  it("colours roles like the desktop viewer", () => {
    expect(roleColor("Outer wall")).toEqual([255, 125, 56]);
    expect(roleColor("Something new")).toEqual([128, 128, 128]);
  });
});
