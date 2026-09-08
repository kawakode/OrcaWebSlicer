import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { previewLayer, readPreview } from "./api";
import type { PreviewIndex, PreviewLayer } from "./types";

/** Batch header: kind, role, tool, reserved, point count, width, height. */
const HEADER_BYTES = 16;
const POINT_BYTES = 12;
const KIND_TRAVEL = 1;

/**
 * Colors are presentation, so they live here rather than in the engine's
 * preview document. A role the engine adds later still draws, in the fallback.
 */
const ROLE_COLORS: Record<string, string> = {
  outer_wall: "#e8722a",
  inner_wall: "#f0c419",
  overhang_wall: "#3d7de0",
  sparse_infill: "#b74ac0",
  internal_solid_infill: "#c8531f",
  top_surface: "#e04f4f",
  bottom_surface: "#3fa7a0",
  ironing: "#8bd0c8",
  bridge: "#5f8ee0",
  internal_bridge: "#7fa4e8",
  gap_infill: "#c0c0c0",
  skirt: "#3fa05a",
  brim: "#5cc47a",
  support: "#7a7f88",
  support_interface: "#9aa0aa",
  support_transition: "#adb3bd",
  prime_tower: "#b08d57",
};
const FALLBACK_COLOR = "#8892a0";
const TRAVEL_COLOR = "#5a6472";

interface Segment {
  kind: number;
  role: number;
  tool: number;
  width: number;
  points: Float64Array;
}

/**
 * Decodes one layer of the binary preview: a run of batches, each a polyline of
 * quantized points. The format is documented in docs/web/preview-format.md.
 */
function decodeLayer(buffer: ArrayBuffer, quantum: number): Segment[] {
  const view = new DataView(buffer);
  const batches: Segment[] = [];
  let offset = 0;
  while (offset + HEADER_BYTES <= buffer.byteLength) {
    const kind = view.getUint8(offset);
    const role = view.getUint8(offset + 1);
    const tool = view.getUint8(offset + 2);
    const count = view.getUint32(offset + 4, true);
    const width = view.getFloat32(offset + 8, true);
    offset += HEADER_BYTES;
    if (count < 2 || offset + count * POINT_BYTES > buffer.byteLength) break;
    const points = new Float64Array(count * 2);
    for (let index = 0; index < count; index += 1) {
      points[index * 2] = view.getInt32(offset + index * POINT_BYTES, true) * quantum;
      points[index * 2 + 1] = view.getInt32(offset + index * POINT_BYTES + 4, true) * quantum;
    }
    offset += count * POINT_BYTES;
    batches.push({ kind, role, tool, width, points });
  }
  return batches;
}

export function LayerPreviewPanel({ jobId }: { jobId: string }) {
  const [index, setIndex] = useState<PreviewIndex | null>(null);
  const [layer, setLayer] = useState(0);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [showTravels, setShowTravels] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    let stale = false;
    readPreview(jobId)
      .then((loaded) => {
        if (stale) return;
        setIndex(loaded);
        // Open on the top layer, which is what a user checks first.
        setLayer(Math.max(0, loaded.layers.length - 1));
      })
      .catch((error) => !stale && setFailure(String(error)));
    return () => {
      stale = true;
    };
  }, [jobId]);

  // Only the selected layer is fetched, so a tall print costs one layer of
  // bandwidth and memory rather than the whole preview.
  useEffect(() => {
    if (!index || index.layers.length === 0) return;
    let stale = false;
    previewLayer(jobId, layer)
      .then((buffer) => !stale && setSegments(decodeLayer(buffer, index.quantum_mm)))
      .catch((error) => !stale && setFailure(String(error)));
    return () => {
      stale = true;
    };
  }, [jobId, layer, index]);

  const draw = useCallback(() => {
    const element = canvas.current;
    if (!element || !index) return;
    const context = element.getContext("2d");
    if (!context) return;

    const ratio = window.devicePixelRatio || 1;
    const size = element.clientWidth;
    element.width = size * ratio;
    element.height = size * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size, size);

    const { min, max } = index.bounding_box;
    const span = Math.max(max[0] - min[0], max[1] - min[1], 1);
    const scale = (size - 16) / span;
    // Y grows away from the viewer on the bed, so the canvas is flipped.
    const project = (x: number, y: number): [number, number] => [
      8 + (x - min[0]) * scale,
      size - 8 - (y - min[1]) * scale,
    ];

    for (const segment of segments) {
      if (segment.kind === KIND_TRAVEL && !showTravels) continue;
      context.strokeStyle =
        segment.kind === KIND_TRAVEL
          ? TRAVEL_COLOR
          : ROLE_COLORS[index.roles[segment.role]?.id ?? ""] ?? FALLBACK_COLOR;
      context.lineWidth =
        segment.kind === KIND_TRAVEL ? 0.5 : Math.max(1, segment.width * scale);
      context.lineCap = "round";
      context.lineJoin = "round";
      context.globalAlpha = segment.kind === KIND_TRAVEL ? 0.35 : 1;
      context.beginPath();
      for (let point = 0; point < segment.points.length; point += 2) {
        const [x, y] = project(segment.points[point], segment.points[point + 1]);
        if (point === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.stroke();
    }
    context.globalAlpha = 1;
  }, [index, segments, showTravels]);

  useEffect(() => {
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [draw]);

  const current: PreviewLayer | undefined = index?.layers[layer];
  const roles = useMemo(() => {
    if (!index || !current) return [];
    return current.roles.map((id) => ({
      id,
      label: index.roles.find((role) => role.id === id)?.label ?? id,
      color: ROLE_COLORS[id] ?? FALLBACK_COLOR,
    }));
  }, [index, current]);

  if (failure) {
    // Styled the same as every other error in the app (App.tsx's `failure`
    // and `job-error`), so it needs the same role="alert" to actually reach a
    // screen reader — without it the failure was visible but silent.
    return (
      <p className="failure" data-testid="preview-failure" role="alert">
        The layer preview could not be loaded.
      </p>
    );
  }
  if (!index || !current) {
    return (
      <p data-testid="preview-loading" role="status">
        Loading the layer preview…
      </p>
    );
  }

  return (
    <div className="preview">
      <canvas ref={canvas} data-testid="preview-canvas" role="img"
        aria-label={`Layer ${layer + 1} of ${index.layers.length} at ${current.z.toFixed(2)} mm`} />
      <label htmlFor="preview-layer">
        Layer{" "}
        <output data-testid="preview-layer-label" htmlFor="preview-layer">
          {layer + 1} / {index.layers.length} — {current.z.toFixed(2)} mm
        </output>
      </label>
      <input
        id="preview-layer"
        data-testid="preview-layer"
        type="range"
        min={0}
        max={index.layers.length - 1}
        value={layer}
        // The number alone ("3") is what a range input announces by default;
        // this gives the same layer-and-height sentence the visible label
        // shows, so arrowing through layers is announced, not just moved.
        aria-valuetext={`Layer ${layer + 1} of ${index.layers.length}, ${current.z.toFixed(2)} millimeters`}
        onChange={(event) => setLayer(Number(event.target.value))}
      />
      <label htmlFor="preview-travels">
        <input
          id="preview-travels"
          data-testid="preview-travels"
          type="checkbox"
          checked={showTravels}
          onChange={(event) => setShowTravels(event.target.checked)}
        />
        Show travel moves
      </label>
      <ul className="legend" data-testid="preview-legend" aria-label="Roles drawn in this layer">
        {roles.map((role) => (
          <li key={role.id}>
            <span className="swatch" style={{ background: role.color }} aria-hidden="true" />
            {role.label}
          </li>
        ))}
        {current.tools.length > 1 && (
          <li data-testid="preview-tools">Tools: {current.tools.join(", ")}</li>
        )}
      </ul>
    </div>
  );
}
