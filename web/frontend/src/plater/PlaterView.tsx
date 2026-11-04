import { useEffect, useRef } from "react";
import {
  BufferAttribute,
  BufferGeometry,
  GridHelper,
  Group,
  DoubleSide,
  Line,
  LineBasicMaterial,
  Matrix3,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  Plane,
  PlaneGeometry,
  Raycaster,
  SphereGeometry,
  Vector2,
  Vector3,
} from "three";

import { createViewport, type Viewport } from "../viewport";
import type { PickHit, ToolOverlay } from "./ObjectTools";
import { layerStates, type PaintKind } from "./paint";
import { bedBounds, fitsBed, itemBounds, itemMatrix, type Bed, type PlateItem } from "./plate";

const COLORS = { idle: 0x8fb3c9, selected: 0x2fc6cc, outside: 0xe0525c, tool: 0xf2b134, tower: 0xb3e3ab };

/** Enforcers, blockers, and fuzzy skin, drawn as the desktop's painting gizmos draw them. */
const PAINT_COLOURS: Record<Exclude<PaintKind, "color">, Record<number, number>> = {
  supports: { 1: 0x3fbf5f, 2: 0xe0525c },
  seam: { 1: 0x3f8fff, 2: 0xe0525c },
  fuzzy_skin: { 1: 0xc77dff },
};

/** A brush or fill gesture on an item, in the item's object coordinates. */
export interface PaintHit {
  id: string;
  /** The display triangle under the pointer. */
  triangle: number;
  point: [number, number, number];
}

/** Which painting the plate shows, and the colour of each state of it. */
export interface PaintDisplay {
  kind: PaintKind;
  /** A filament's colour for colour painting. */
  colour?: (state: number) => number | undefined;
}

/**
 * The item's geometry with its triangles grouped by painted state, so each
 * state draws with its own material. `order` maps a drawn triangle back to the
 * display mesh's own index, which raycasts report.
 */
function paintedGeometry(base: BufferGeometry, states: Uint8Array): BufferGeometry {
  const source = base.getIndex()!.array as Uint32Array;
  const count = states.length;
  const buckets = new Map<number, number[]>();
  for (let triangle = 0; triangle < count; triangle++) {
    const state = states[triangle];
    let bucket = buckets.get(state);
    if (!bucket) buckets.set(state, (bucket = []));
    bucket.push(triangle);
  }
  const order = new Uint32Array(count);
  const index = new Uint32Array(count * 3);
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", base.getAttribute("position"));
  geometry.setAttribute("normal", base.getAttribute("normal"));
  let at = 0;
  for (const [state, triangles] of [...buckets].sort(([a], [b]) => a - b)) {
    geometry.addGroup(at * 3, triangles.length * 3, state);
    for (const triangle of triangles) {
      order[at] = triangle;
      index.set(source.subarray(triangle * 3, triangle * 3 + 3), at * 3);
      at++;
    }
  }
  geometry.setIndex(new BufferAttribute(index, 1));
  geometry.userData.order = order;
  return geometry;
}

/**
 * Draws the bed and every item, and turns pointer gestures into selection and
 * moves. Dragging a selected item slides it across the bed plane; any other
 * drag orbits the camera. While a tool is picking, a click reports the point
 * and face under the pointer instead of changing the selection.
 */
export function PlaterView(props: {
  bed: Bed;
  items: PlateItem[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onMove: (id: string, x: number, y: number) => void;
  picking?: boolean;
  onPick?: (hit: PickHit) => void;
  overlay?: ToolOverlay;
  /** The colour an unselected item that fits is drawn in: its filament's, when there are several. */
  itemColour?: (item: PlateItem) => number | undefined;
  /** A multi-material plate's prime tower footprint, front left corner and width in millimetres. */
  wipeTower?: { x: number; y: number; width: number };
  /** The painting drawn on every item, if any. */
  paintDisplay?: PaintDisplay;
  /** While set, dragging across the selected item paints it instead of moving it. */
  onPaint?: (hit: PaintHit) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const viewport = useRef<Viewport | null>(null);
  const objects = useRef(new Group());
  const bedGroup = useRef(new Group());
  const overlayGroup = useRef(new Group());
  const towerGroup = useRef(new Group());
  const geometries = useRef(new Map<string, BufferGeometry>());
  const painted = useRef(new Map<string, BufferGeometry>());
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    const created = createViewport(host.current!);
    viewport.current = created;
    created.scene.add(bedGroup.current, objects.current, overlayGroup.current, towerGroup.current);

    const raycaster = new Raycaster();
    const pointer = new Vector2();
    const bedPlane = new Plane(new Vector3(0, 0, 1), 0);
    let drag: { id: string; offset: Vector3 } | null = null;
    let stroke: string | null = null;
    let downAt: { x: number; y: number } | null = null;

    const cast = (event: PointerEvent) => {
      const rect = created.renderer.domElement.getBoundingClientRect();
      pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(pointer, created.camera);
    };
    const hitBed = () => raycaster.ray.intersectPlane(bedPlane, new Vector3());
    // Report the triangle under the pointer, in the item's own coordinates.
    const paintAt = (id: string) => {
      const hit = raycaster
        .intersectObjects(objects.current.children, false)
        .find((candidate) => candidate.object.userData.id === id);
      const onPaint = latest.current.onPaint;
      if (!hit || hit.faceIndex == null || !onPaint) return;
      const order = (hit.object as Mesh).geometry.userData.order as Uint32Array | undefined;
      const local = hit.point.clone().applyMatrix4(hit.object.matrixWorld.clone().invert());
      onPaint({ id, triangle: order ? order[hit.faceIndex] : hit.faceIndex, point: [local.x, local.y, local.z] });
    };

    const down = (event: PointerEvent) => {
      downAt = { x: event.clientX, y: event.clientY };
      cast(event);
      const hit = raycaster.intersectObjects(objects.current.children, false)[0];
      const id = hit?.object.userData.id as string | undefined;
      const { items, selectedId, picking, onPaint } = latest.current;
      if (id && id === selectedId && onPaint) {
        stroke = id;
        created.controls.enabled = false;
        paintAt(id);
      } else if (id && id === selectedId && !picking) {
        const item = items.find((candidate) => candidate.id === id)!;
        const point = hitBed();
        if (point) {
          drag = { id, offset: new Vector3(item.x - point.x, item.y - point.y, 0) };
          created.controls.enabled = false;
        }
      }
    };
    const move = (event: PointerEvent) => {
      if (stroke) {
        cast(event);
        paintAt(stroke);
        return;
      }
      if (!drag) return;
      cast(event);
      const point = hitBed();
      if (point) latest.current.onMove(drag.id, point.x + drag.offset.x, point.y + drag.offset.y);
    };
    const up = (event: PointerEvent) => {
      const clicked = downAt && Math.hypot(event.clientX - downAt.x, event.clientY - downAt.y) < 4;
      if (clicked && !drag && !stroke) {
        cast(event);
        const hit = raycaster.intersectObjects(objects.current.children, false)[0];
        const { picking, onPick, onSelect } = latest.current;
        if (picking) {
          if (hit?.face && onPick) {
            const normal = hit.face.normal.clone().applyMatrix3(new Matrix3().getNormalMatrix(hit.object.matrixWorld));
            onPick({ id: hit.object.userData.id as string, point: hit.point.clone(), normal: normal.normalize() });
          }
        } else {
          onSelect((hit?.object.userData.id as string | undefined) ?? null);
        }
      }
      drag = null;
      stroke = null;
      downAt = null;
      created.controls.enabled = true;
    };
    const canvas = created.renderer.domElement;
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      geometries.current.forEach((geometry) => geometry.dispose());
      geometries.current.clear();
      painted.current.forEach((geometry) => geometry.dispose());
      painted.current.clear();
      created.dispose();
      viewport.current = null;
    };
  }, []);

  // The bed only changes with the printer, and reframes the camera when it does.
  useEffect(() => {
    const view = viewport.current;
    if (!view) return;
    bedGroup.current.clear();
    const { minX, maxX, minY, maxY } = bedBounds(props.bed);
    const size = Math.max(maxX - minX, maxY - minY);
    const grid = new GridHelper(size, Math.max(1, Math.round(size / 10)), 0x55606c, 0x39414b);
    grid.rotation.x = Math.PI / 2;
    grid.position.set((minX + maxX) / 2, (minY + maxY) / 2, -0.01);
    const outline = new BufferGeometry().setFromPoints(
      [...props.bed.outline, props.bed.outline[0]].map(([x, y]) => new Vector3(x, y, 0)),
    );
    bedGroup.current.add(grid, new Line(outline, new LineBasicMaterial({ color: 0xd8dee6 })));
    view.frame(new Vector3((minX + maxX) / 2, (minY + maxY) / 2, 0), size);
  }, [props.bed]);

  useEffect(() => {
    const view = viewport.current;
    if (!view) return;
    objects.current.children.forEach((child) => {
      const material = (child as Mesh).material;
      (Array.isArray(material) ? material : [material]).forEach((entry) => entry.dispose());
    });
    objects.current.clear();
    const display = props.paintDisplay;
    const kept = new Map<string, BufferGeometry>();
    for (const item of props.items) {
      // Instances of one upload object share their geometry.
      const key = `${item.uploadId}:${item.objectIndex}`;
      let geometry = geometries.current.get(key);
      if (!geometry) {
        geometry = new BufferGeometry();
        geometry.setAttribute("position", new BufferAttribute(item.mesh.positions, 3));
        geometry.setIndex(new BufferAttribute(item.mesh.indices, 1));
        geometry.computeVertexNormals();
        geometries.current.set(key, geometry);
      }
      const color =
        item.id === props.selectedId
          ? COLORS.selected
          : fitsBed(item, props.bed)
            ? (props.itemColour?.(item) ?? COLORS.idle)
            : COLORS.outside;
      const surface = (colour: number) => new MeshStandardMaterial({ color: colour, roughness: 0.7, metalness: 0.05 });
      const states = display ? layerStates(item.paint, display.kind, item.mesh.indices.length / 3) : null;
      let mesh: Mesh;
      if (states && display) {
        // Painted geometry is the item's own, rebuilt only when its painting changes.
        const paintKey = `${key}|${item.paint!.id}|${display.kind}`;
        const shaded = painted.current.get(paintKey) ?? paintedGeometry(geometry, states);
        kept.set(paintKey, shaded);
        const materials = [];
        const highest = states.reduce((top, state) => (state > top ? state : top), 0);
        for (let state = 0; state <= highest; state++) {
          const paintColour =
            state === 0
              ? color
              : display.kind === "color"
                ? (display.colour?.(state) ?? COLORS.tool)
                : (PAINT_COLOURS[display.kind][state] ?? COLORS.tool);
          materials.push(surface(paintColour));
        }
        mesh = new Mesh(shaded, materials);
      } else {
        mesh = new Mesh(geometry, surface(color));
      }
      mesh.matrixAutoUpdate = false;
      mesh.matrix.copy(itemMatrix(item));
      mesh.userData.id = item.id;
      objects.current.add(mesh);
    }
    painted.current.forEach((geometry, key) => {
      if (!kept.has(key)) geometry.dispose();
    });
    painted.current = kept;
    view.render();
  }, [props.items, props.selectedId, props.bed, props.itemColour, props.paintDisplay]);

  // The tower's depth depends on the purge volumes the engine computes, so
  // only its square base is drawn, and it is never picked.
  useEffect(() => {
    const view = viewport.current;
    if (!view) return;
    towerGroup.current.children.forEach((child) => {
      (child as Mesh).geometry.dispose();
      ((child as Mesh).material as MeshBasicMaterial).dispose();
    });
    towerGroup.current.clear();
    const tower = props.wipeTower;
    if (tower) {
      const base = new Mesh(
        new PlaneGeometry(tower.width, tower.width),
        new MeshBasicMaterial({ color: COLORS.tower, transparent: true, opacity: 0.6, side: DoubleSide }),
      );
      base.position.set(tower.x + tower.width / 2, tower.y + tower.width / 2, 0.05);
      towerGroup.current.add(base);
    }
    view.render();
  }, [props.wipeTower?.x, props.wipeTower?.y, props.wipeTower?.width]);

  // The open tool's guides: the cut plane across its item, and measured points.
  useEffect(() => {
    const view = viewport.current;
    if (!view) return;
    overlayGroup.current.children.forEach((child) => {
      const shape = child as Mesh;
      shape.geometry.dispose();
      (shape.material as MeshBasicMaterial).dispose();
    });
    overlayGroup.current.clear();
    const material = () => new MeshBasicMaterial({ color: COLORS.tool, transparent: true, opacity: 0.45, side: DoubleSide });
    const cut = props.overlay?.cut;
    const item = cut ? props.items.find((candidate) => candidate.id === cut.itemId) : undefined;
    if (cut && item) {
      const box = itemBounds(item);
      const margin = 4;
      const plane = new Mesh(new PlaneGeometry(box.maxX - box.minX + 2 * margin, box.maxY - box.minY + 2 * margin), material());
      plane.position.set((box.minX + box.maxX) / 2, (box.minY + box.maxY) / 2, cut.height);
      overlayGroup.current.add(plane);
    }
    const points = props.overlay?.points ?? [];
    for (const point of points) {
      const marker = new Mesh(new SphereGeometry(0.8, 12, 8), material());
      marker.position.copy(point);
      overlayGroup.current.add(marker);
    }
    if (points.length === 2) {
      overlayGroup.current.add(new Line(new BufferGeometry().setFromPoints(points), new LineBasicMaterial({ color: COLORS.tool })));
    }
    view.render();
  }, [props.overlay, props.items]);

  return <div className="viewport" ref={host} data-testid="plater-view" aria-label="Build plate" role="img" />;
}
