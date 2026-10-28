/** Decoders for the worker's layer preview, documented in `docs/web/preview.md`. */

export interface PreviewLayer {
  index: number;
  z: number;
  height: number;
  offset: number;
  count: number;
}

export interface PreviewIndex {
  format: "orca-web-preview";
  version: number;
  segment_bytes: number;
  roles: { id: number; name: string; used: boolean }[];
  tools: number[];
  layers: PreviewLayer[];
  bounding_box?: { min: [number, number, number]; max: [number, number, number] };
}

/** One decoded page: line endpoints plus each segment's role and tool. */
export interface PreviewSegments {
  /** x0 y0 z0 x1 y1 z1 per segment. */
  positions: Float32Array;
  roles: Uint8Array;
  tools: Uint8Array;
  widths: Float32Array;
}

// The desktop viewer's default role colours, keyed by the engine's role name.
const ROLE_COLORS: Record<string, [number, number, number]> = {
  Undefined: [230, 179, 179],
  "Inner wall": [255, 230, 77],
  "Outer wall": [255, 125, 56],
  "Overhang wall": [31, 31, 255],
  "Sparse infill": [176, 48, 41],
  "Internal solid infill": [150, 84, 204],
  "Top surface": [240, 64, 64],
  "Bottom surface": [102, 92, 199],
  Ironing: [255, 140, 105],
  Bridge: [77, 128, 186],
  "Internal Bridge": [77, 128, 186],
  "Gap infill": [255, 255, 255],
  Skirt: [0, 135, 110],
  Brim: [0, 59, 110],
  Support: [0, 255, 0],
  "Support interface": [0, 128, 0],
  "Support transition": [0, 64, 0],
  "Prime tower": [179, 227, 171],
  Custom: [94, 209, 148],
  Multiple: [128, 128, 128],
};

export function roleColor(name: string): [number, number, number] {
  return ROLE_COLORS[name] ?? [128, 128, 128];
}

/** Decode one page of 36-byte records, as streamed for a layer range. */
export function decodeSegments(buffer: ArrayBuffer, segmentBytes: number): PreviewSegments {
  if (segmentBytes !== 36 || buffer.byteLength % segmentBytes !== 0) {
    throw new Error("The preview page does not hold whole segments.");
  }
  const count = buffer.byteLength / segmentBytes;
  const view = new DataView(buffer);
  const positions = new Float32Array(count * 6);
  const widths = new Float32Array(count);
  const roles = new Uint8Array(count);
  const tools = new Uint8Array(count);
  for (let i = 0; i < count; i++) {
    const base = i * segmentBytes;
    for (let k = 0; k < 6; k++) positions[i * 6 + k] = view.getFloat32(base + k * 4, true);
    widths[i] = view.getFloat32(base + 24, true);
    roles[i] = view.getUint8(base + 32);
    tools[i] = view.getUint8(base + 33);
  }
  return { positions, roles, tools, widths };
}

/**
 * Split a layer range into requests of at most `maxBytes`, so a large preview
 * is fetched page by page and the first layers render before the last arrive.
 */
export function pageLayers(layers: PreviewLayer[], segmentBytes: number, maxBytes: number): [number, number][] {
  const pages: [number, number][] = [];
  let start = 0;
  let bytes = 0;
  layers.forEach((layer, index) => {
    const size = layer.count * segmentBytes;
    if (index > start && bytes + size > maxBytes) {
      pages.push([start, index - 1]);
      start = index;
      bytes = 0;
    }
    bytes += size;
  });
  if (layers.length > 0) pages.push([start, layers.length - 1]);
  return pages;
}
