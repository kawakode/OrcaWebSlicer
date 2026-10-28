import { useEffect, useRef } from "react";
import {
  BufferAttribute,
  BufferGeometry,
  GridHelper,
  Group,
  Line,
  LineBasicMaterial,
  Mesh,
  MeshStandardMaterial,
  Plane,
  Raycaster,
  Vector2,
  Vector3,
} from "three";

import { createViewport, type Viewport } from "../viewport";
import { bedBounds, fitsBed, itemMatrix, type Bed, type PlateItem } from "./plate";

const COLORS = { idle: 0x8fb3c9, selected: 0x2fc6cc, outside: 0xe0525c };

/**
 * Draws the bed and every item, and turns pointer gestures into selection and
 * moves. Dragging a selected item slides it across the bed plane; any other
 * drag orbits the camera.
 */
export function PlaterView(props: {
  bed: Bed;
  items: PlateItem[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onMove: (id: string, x: number, y: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const viewport = useRef<Viewport | null>(null);
  const objects = useRef(new Group());
  const bedGroup = useRef(new Group());
  const geometries = useRef(new Map<string, BufferGeometry>());
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    const created = createViewport(host.current!);
    viewport.current = created;
    created.scene.add(bedGroup.current, objects.current);

    const raycaster = new Raycaster();
    const pointer = new Vector2();
    const bedPlane = new Plane(new Vector3(0, 0, 1), 0);
    let drag: { id: string; offset: Vector3 } | null = null;
    let downAt: { x: number; y: number } | null = null;

    const cast = (event: PointerEvent) => {
      const rect = created.renderer.domElement.getBoundingClientRect();
      pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(pointer, created.camera);
    };
    const hitBed = () => raycaster.ray.intersectPlane(bedPlane, new Vector3());

    const down = (event: PointerEvent) => {
      downAt = { x: event.clientX, y: event.clientY };
      cast(event);
      const hit = raycaster.intersectObjects(objects.current.children, false)[0];
      const id = hit?.object.userData.id as string | undefined;
      const { items, selectedId } = latest.current;
      if (id && id === selectedId) {
        const item = items.find((candidate) => candidate.id === id)!;
        const point = hitBed();
        if (point) {
          drag = { id, offset: new Vector3(item.x - point.x, item.y - point.y, 0) };
          created.controls.enabled = false;
        }
      }
    };
    const move = (event: PointerEvent) => {
      if (!drag) return;
      cast(event);
      const point = hitBed();
      if (point) latest.current.onMove(drag.id, point.x + drag.offset.x, point.y + drag.offset.y);
    };
    const up = (event: PointerEvent) => {
      const clicked = downAt && Math.hypot(event.clientX - downAt.x, event.clientY - downAt.y) < 4;
      if (clicked && !drag) {
        cast(event);
        const hit = raycaster.intersectObjects(objects.current.children, false)[0];
        latest.current.onSelect((hit?.object.userData.id as string | undefined) ?? null);
      }
      drag = null;
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
    objects.current.children.forEach((child) => ((child as Mesh).material as MeshStandardMaterial).dispose());
    objects.current.clear();
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
        item.id === props.selectedId ? COLORS.selected : fitsBed(item, props.bed) ? COLORS.idle : COLORS.outside;
      const mesh = new Mesh(geometry, new MeshStandardMaterial({ color, roughness: 0.7, metalness: 0.05 }));
      mesh.matrixAutoUpdate = false;
      mesh.matrix.copy(itemMatrix(item));
      mesh.userData.id = item.id;
      objects.current.add(mesh);
    }
    view.render();
  }, [props.items, props.selectedId, props.bed]);

  return <div className="viewport" ref={host} data-testid="plater-view" aria-label="Build plate" role="img" />;
}
