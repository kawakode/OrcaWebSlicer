import { useCallback, useEffect, useRef, type PointerEvent } from "react";

import { type Matrix, type Vector, transformPoint } from "./geometry";

/**
 * Draws the bed and the placed objects on a 2D canvas.
 *
 * The projection is a real orthographic camera with painter's-algorithm depth
 * sorting and flat shading, not a flat top-down trace — but it is painted with
 * the Canvas 2D API rather than WebGL. That is deliberate: headless Firefox,
 * one of the three release-blocking browsers in docs/web/mvp.md, has no WebGL
 * context at all in the container the Playwright matrix runs in, so a WebGL
 * plater could not be verified in a browser the release depends on. Canvas 2D
 * renders identically everywhere and needs no dependency.
 */

export interface Camera {
  /** Radians. The direction the camera looks from, around the bed's up axis. */
  azimuth: number;
  /** Radians, clamped away from the horizon so the ground plane stays usable. */
  elevation: number;
  /** Pixels per millimetre. */
  zoom: number;
}

export interface DrawItem {
  /** Local-frame triangle soup: three floats per vertex, three vertices per triangle. */
  vertices: Float32Array;
  matrix: Matrix;
  /** The object's own filament color — see filamentColors.ts. */
  color: Vector;
  selected: boolean;
  /** True when the mesh was replaced by its bounding box to stay inside the budget. */
  simplified: boolean;
}

const MIN_ELEVATION = (12 * Math.PI) / 180;
const MAX_ELEVATION = (89 * Math.PI) / 180;
export const TOP_VIEW: Camera = { azimuth: -Math.PI / 2, elevation: MAX_ELEVATION, zoom: 1 };
export const ISOMETRIC_VIEW: Camera = { azimuth: -Math.PI / 2.6, elevation: Math.PI / 5, zoom: 1 };

const BED_FILL = "#8892a022";
const BED_LINE = "#8892a0";
const GRID_LINE = "#8892a044";
const GRID_MM = 50;
const SIMPLIFIED_ALPHA = 0.55;
const LIGHT: Vector = [0.35, -0.5, 0.79];
const AMBIENT = 0.38;
/**
 * Selection is drawn as a two-tone wireframe halo over the item's own fill
 * rather than a color swap: a fill now carries meaning (which filament), so
 * swapping it away on selection would hide that and could also collide with
 * another slot's own color. Stroking every edge in both a near-black and a
 * near-white line guarantees at least one of the two contrasts with any fill
 * color underneath, light or dark.
 */
const SELECTION_HALO_OUTER = "#10131a";
const SELECTION_HALO_INNER = "#ffffff";

interface Basis {
  right: Vector;
  up: Vector;
  /** From the scene towards the camera, so a larger dot product is nearer. */
  towards: Vector;
}

function basisOf(camera: Camera): Basis {
  const ca = Math.cos(camera.azimuth);
  const sa = Math.sin(camera.azimuth);
  const ce = Math.cos(camera.elevation);
  const se = Math.sin(camera.elevation);
  return {
    right: [-sa, ca, 0],
    up: [-se * ca, -se * sa, ce],
    towards: [ce * ca, ce * sa, se],
  };
}

const dot = (a: Vector, b: Vector): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/**
 * The world-space XY movement a pointer drag means on the bed plane.
 *
 * Inverting the projection for a fixed z is a 2x2 solve; the matrix happens to
 * be its own inverse, which is why this is three lines rather than a solver.
 */
function dragToBed(camera: Camera, dx: number, dy: number): [number, number] {
  const ca = Math.cos(camera.azimuth);
  const sa = Math.sin(camera.azimuth);
  const along = dx / camera.zoom;
  const into = dy / (camera.zoom * Math.sin(camera.elevation));
  return [-sa * along + ca * into, ca * along + sa * into];
}

/** The zoom that fits the bed's own corners into the canvas. */
function fitZoom(bed: [number, number][], camera: Camera, width: number, height: number): number {
  const basis = basisOf(camera);
  let spanX = 1;
  let spanY = 1;
  const xs: number[] = [];
  const ys: number[] = [];
  for (const [x, y] of bed) {
    const point: Vector = [x, y, 0];
    xs.push(dot(point, basis.right));
    ys.push(dot(point, basis.up));
  }
  if (xs.length > 0) {
    spanX = Math.max(Math.max(...xs) - Math.min(...xs), 1);
    spanY = Math.max(Math.max(...ys) - Math.min(...ys), 1);
  }
  return Math.min(width / spanX, height / spanY) * 0.82;
}

interface Projected {
  /** Six screen coordinates per triangle, in painter's order. */
  points: Float32Array;
  /** The item each triangle belongs to, so a click can name what it hit. */
  owner: Int32Array;
  fill: string[];
}

function project(
  items: DrawItem[],
  camera: Camera,
  center: [number, number],
  width: number,
  height: number,
): Projected {
  const basis = basisOf(camera);
  const triangles: { depth: number; owner: number; screen: number[]; fill: string }[] = [];
  const originX = width / 2;
  const originY = height / 2;

  items.forEach((item, owner) => {
    for (let vertex = 0; vertex + 8 < item.vertices.length; vertex += 9) {
      const world: Vector[] = [];
      for (let corner = 0; corner < 3; corner += 1) {
        const offset = vertex + corner * 3;
        world.push(
          transformPoint(
            item.matrix,
            item.vertices[offset],
            item.vertices[offset + 1],
            item.vertices[offset + 2],
          ),
        );
      }
      const edgeA: Vector = [
        world[1][0] - world[0][0],
        world[1][1] - world[0][1],
        world[1][2] - world[0][2],
      ];
      const edgeB: Vector = [
        world[2][0] - world[0][0],
        world[2][1] - world[0][1],
        world[2][2] - world[0][2],
      ];
      const normal: Vector = [
        edgeA[1] * edgeB[2] - edgeA[2] * edgeB[1],
        edgeA[2] * edgeB[0] - edgeA[0] * edgeB[2],
        edgeA[0] * edgeB[1] - edgeA[1] * edgeB[0],
      ];
      const length = Math.hypot(normal[0], normal[1], normal[2]) || 1;
      const lit = AMBIENT + (1 - AMBIENT) * Math.abs(dot(normal, LIGHT) / length);
      const screen: number[] = [];
      let depth = 0;
      for (const point of world) {
        const shifted: Vector = [point[0] - center[0], point[1] - center[1], point[2]];
        screen.push(
          originX + dot(shifted, basis.right) * camera.zoom,
          originY - dot(shifted, basis.up) * camera.zoom,
        );
        depth += dot(shifted, basis.towards);
      }
      triangles.push({
        depth: depth / 3,
        owner,
        screen,
        fill: `rgb(${item.color.map((channel) => Math.round(channel * lit)).join(",")})`,
      });
    }
  });

  // Farthest first: the painter's algorithm is what gives a 2D canvas correct
  // occlusion without a depth buffer.
  triangles.sort((left, right) => left.depth - right.depth);
  const points = new Float32Array(triangles.length * 6);
  const owner = new Int32Array(triangles.length);
  const fill: string[] = [];
  triangles.forEach((triangle, index) => {
    points.set(triangle.screen, index * 6);
    owner[index] = triangle.owner;
    fill.push(triangle.fill);
  });
  return { points, owner, fill };
}

function contains(points: Float32Array, triangle: number, x: number, y: number): boolean {
  const base = triangle * 6;
  const ax = points[base];
  const ay = points[base + 1];
  const bx = points[base + 2];
  const by = points[base + 3];
  const cx = points[base + 4];
  const cy = points[base + 5];
  const area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  if (area === 0) return false;
  const u = ((bx - x) * (cy - y) - (by - y) * (cx - x)) / area;
  const v = ((cx - x) * (ay - y) - (cy - y) * (ax - x)) / area;
  return u >= 0 && v >= 0 && u + v <= 1;
}

export function PlaterCanvas(props: {
  bed: [number, number][];
  items: DrawItem[];
  camera: Camera;
  label: string;
  disabled: boolean;
  onCamera: (camera: Camera) => void;
  onSelect: (item: number | null) => void;
  onMove: (item: number, dx: number, dy: number) => void;
}) {
  const { bed, items, camera, disabled, onCamera, onSelect, onMove } = props;
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const projected = useRef<Projected | null>(null);
  const gesture = useRef<{ pointer: number; item: number | null; x: number; y: number } | null>(null);

  const center = useRef<[number, number]>([0, 0]);
  center.current = bed.length
    ? [
        (Math.min(...bed.map((point) => point[0])) + Math.max(...bed.map((point) => point[0]))) / 2,
        (Math.min(...bed.map((point) => point[1])) + Math.max(...bed.map((point) => point[1]))) / 2,
      ]
    : [0, 0];

  const draw = useCallback(() => {
    const element = canvas.current;
    const context = element?.getContext("2d");
    if (!element || !context) return;

    const ratio = window.devicePixelRatio || 1;
    const width = element.clientWidth;
    const height = element.clientHeight;
    if (width <= 0 || height <= 0) return;
    // A zero zoom means "fit the bed", which only this component can answer
    // because only it knows how large the canvas ended up.
    if (camera.zoom <= 0) {
      onCamera({ ...camera, zoom: fitZoom(bed, camera, width, height) });
      return;
    }
    element.width = Math.max(1, Math.round(width * ratio));
    element.height = Math.max(1, Math.round(height * ratio));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);

    const basis = basisOf(camera);
    const toScreen = (x: number, y: number, z: number): [number, number] => {
      const shifted: Vector = [x - center.current[0], y - center.current[1], z];
      return [
        width / 2 + dot(shifted, basis.right) * camera.zoom,
        height / 2 - dot(shifted, basis.up) * camera.zoom,
      ];
    };

    if (bed.length > 2) {
      context.beginPath();
      bed.forEach(([x, y], index) => {
        const [screenX, screenY] = toScreen(x, y, 0);
        if (index === 0) context.moveTo(screenX, screenY);
        else context.lineTo(screenX, screenY);
      });
      context.closePath();
      context.fillStyle = BED_FILL;
      context.fill();
      context.strokeStyle = BED_LINE;
      context.lineWidth = 1.5;
      context.stroke();

      const minX = Math.min(...bed.map((point) => point[0]));
      const maxX = Math.max(...bed.map((point) => point[0]));
      const minY = Math.min(...bed.map((point) => point[1]));
      const maxY = Math.max(...bed.map((point) => point[1]));
      context.strokeStyle = GRID_LINE;
      context.lineWidth = 1;
      context.beginPath();
      for (let x = Math.ceil(minX / GRID_MM) * GRID_MM; x <= maxX; x += GRID_MM) {
        context.moveTo(...toScreen(x, minY, 0));
        context.lineTo(...toScreen(x, maxY, 0));
      }
      for (let y = Math.ceil(minY / GRID_MM) * GRID_MM; y <= maxY; y += GRID_MM) {
        context.moveTo(...toScreen(minX, y, 0));
        context.lineTo(...toScreen(maxX, y, 0));
      }
      context.stroke();
    }

    const scene = project(items, camera, center.current, width, height);
    projected.current = scene;
    for (let triangle = 0; triangle < scene.owner.length; triangle += 1) {
      const base = triangle * 6;
      context.globalAlpha = items[scene.owner[triangle]].simplified ? SIMPLIFIED_ALPHA : 1;
      context.fillStyle = scene.fill[triangle];
      context.beginPath();
      context.moveTo(scene.points[base], scene.points[base + 1]);
      context.lineTo(scene.points[base + 2], scene.points[base + 3]);
      context.lineTo(scene.points[base + 4], scene.points[base + 5]);
      context.closePath();
      context.fill();
    }
    context.globalAlpha = 1;

    // Selection halo: a second pass, on top of every fill, so it reads
    // clearly regardless of paint order or which filament color it sits on.
    for (let triangle = 0; triangle < scene.owner.length; triangle += 1) {
      if (!items[scene.owner[triangle]].selected) continue;
      const base = triangle * 6;
      context.beginPath();
      context.moveTo(scene.points[base], scene.points[base + 1]);
      context.lineTo(scene.points[base + 2], scene.points[base + 3]);
      context.lineTo(scene.points[base + 4], scene.points[base + 5]);
      context.closePath();
      context.strokeStyle = SELECTION_HALO_OUTER;
      context.lineWidth = 3;
      context.stroke();
      context.strokeStyle = SELECTION_HALO_INNER;
      context.lineWidth = 1.25;
      context.stroke();
    }
  }, [bed, items, camera, onCamera]);

  useEffect(() => {
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [draw]);

  // Zoom needs a non-passive listener to keep the wheel from scrolling the page
  // as well, which React's own onWheel cannot promise.
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const zoom = (event: WheelEvent) => {
      event.preventDefault();
      const factor = Math.exp(-event.deltaY / 500);
      onCamera({ ...camera, zoom: Math.min(40, Math.max(0.2, camera.zoom * factor)) });
    };
    element.addEventListener("wheel", zoom, { passive: false });
    return () => element.removeEventListener("wheel", zoom);
  }, [camera, onCamera]);

  const hit = (event: PointerEvent<HTMLCanvasElement>): number | null => {
    const scene = projected.current;
    const element = canvas.current;
    if (!scene || !element) return null;
    const bounds = element.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    // Nearest first, which is the reverse of the order they were painted in.
    for (let triangle = scene.owner.length - 1; triangle >= 0; triangle -= 1) {
      if (contains(scene.points, triangle, x, y)) return scene.owner[triangle];
    }
    return null;
  };

  return (
    <canvas
      ref={canvas}
      className="plater-canvas"
      data-testid="plater-canvas"
      role="img"
      aria-label={props.label}
      onPointerDown={(event) => {
        const item = hit(event);
        if (item !== null) onSelect(item);
        gesture.current = {
          pointer: event.pointerId,
          item: disabled ? null : item,
          x: event.clientX,
          y: event.clientY,
        };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const active = gesture.current;
        if (!active || active.pointer !== event.pointerId) return;
        const dx = event.clientX - active.x;
        const dy = event.clientY - active.y;
        active.x = event.clientX;
        active.y = event.clientY;
        if (active.item !== null) {
          const [worldX, worldY] = dragToBed(camera, dx, dy);
          onMove(active.item, worldX, worldY);
          return;
        }
        onCamera({
          azimuth: camera.azimuth + dx / 120,
          elevation: Math.min(MAX_ELEVATION, Math.max(MIN_ELEVATION, camera.elevation + dy / 160)),
          zoom: camera.zoom,
        });
      }}
      onPointerUp={(event) => {
        gesture.current = null;
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
    />
  );
}
