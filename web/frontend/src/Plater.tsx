import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { readJob, readScene, sceneObject, submitScene, type ProfileSelection } from "./api";
import {
  ISOMETRIC_VIEW,
  PlaterCanvas,
  TOP_VIEW,
  type Camera,
  type DrawItem,
} from "./PlaterCanvas";
import {
  boxMesh,
  multiply,
  rotationZ,
  transformBox,
  uniformScale,
  withTranslation,
  withoutTranslation,
  type Box,
  type Matrix,
} from "./geometry";
import { ApiError, TERMINAL_STATES, type ObjectPlacement, type SceneIndex } from "./types";

const POLL_INTERVAL_MS = 300;
/**
 * How many triangles the canvas draws before objects fall back to their
 * bounding boxes. A 2D canvas paints one path per triangle, so this is a
 * budget on frame time; the ceiling the worker enforces is a million.
 */
const TRIANGLE_BUDGET = 20_000;
/**
 * The spacing the browser's own arrange leaves between footprints. It is not
 * the engine's `min_object_distance` — the browser cannot evaluate that
 * without the resolved configuration — so it is deliberately generous.
 */
const ARRANGE_GAP_MM = 6;

/** One placed copy. `source` indexes the scene's `objects` array. */
interface Placement {
  id: number;
  source: number;
  x: number;
  y: number;
  /** Degrees about Z. */
  rotation: number;
  /** Uniform, 1 being the imported size. */
  scale: number;
}

interface Scene {
  index: SceneIndex;
  /** One local-frame triangle soup per scene object, in the scene's order. */
  meshes: Float32Array[];
}

const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** Little-endian quantized triangle soup, as docs/web/scene-format.md declares. */
function decodeMesh(buffer: ArrayBuffer, quantum: number): Float32Array {
  const view = new DataView(buffer);
  const vertices = new Float32Array(Math.floor(buffer.byteLength / 12) * 3);
  for (let vertex = 0; vertex * 3 < vertices.length; vertex += 1) {
    for (let axis = 0; axis < 3; axis += 1) {
      vertices[vertex * 3 + axis] = view.getInt32(vertex * 12 + axis * 4, true) * quantum;
    }
  }
  return vertices;
}

/**
 * Runs the upload's inspect job and reads the scene it published, one object
 * at a time — the API serves the geometry that way so a plate is never loaded
 * whole into one response.
 */
async function loadScene(uploadId: string, profiles: ProfileSelection): Promise<Scene> {
  let job = await submitScene(uploadId, profiles);
  while (!TERMINAL_STATES.includes(job.state)) {
    await delay(POLL_INTERVAL_MS);
    job = await readJob(job.job_id);
  }
  if (job.state !== "succeeded") {
    throw new ApiError(
      job.error?.code ?? "scene_failed",
      job.error?.message ?? "The model could not be inspected.",
      0,
    );
  }
  const index = await readScene(job.job_id);
  const meshes: Float32Array[] = [];
  for (const object of index.objects) {
    meshes.push(decodeMesh(await sceneObject(job.job_id, object.index), index.quantum_mm));
  }
  return { index, meshes };
}

/** The placement's rotation and scale, without the translation it will get. */
function orientation(scene: Scene, placement: Placement): Matrix {
  const imported = scene.index.objects[placement.source].transform;
  return multiply(
    rotationZ(placement.rotation),
    multiply(uniformScale(placement.scale), withoutTranslation(imported)),
  );
}

/**
 * The matrix a slice request carries for one placement, and the world box it
 * puts on the bed.
 *
 * Every object is sat on the bed rather than keeping the Z it was imported at:
 * the worker only lifts an object that is *entirely* below the bed, so a
 * plater that let one hang would slice exactly the hanging placement.
 */
function place(scene: Scene, placement: Placement): { matrix: Matrix; box: Box } {
  const oriented = orientation(scene, placement);
  const local = scene.index.objects[placement.source].bounding_box;
  const resting = transformBox(oriented, local);
  const matrix = withTranslation(oriented, placement.x, placement.y, -resting.min[2]);
  return { matrix, box: transformBox(matrix, local) };
}

const bedBounds = (bed: [number, number][]) => ({
  minX: Math.min(...bed.map((point) => point[0])),
  maxX: Math.max(...bed.map((point) => point[0])),
  minY: Math.min(...bed.map((point) => point[1])),
  maxY: Math.max(...bed.map((point) => point[1])),
});

/**
 * Shelf-packs the placements onto the bed in insertion order and centres the
 * result. Insertion order rather than largest-first keeps the layout stable
 * and predictable as objects are duplicated and deleted.
 */
function arrange(scene: Scene, placements: Placement[]): Placement[] {
  if (placements.length === 0) return placements;
  const bed = bedBounds(scene.index.bed.shape);
  const available = Math.max(bed.maxX - bed.minX - ARRANGE_GAP_MM * 2, 1);
  const sizes = placements.map((placement) => {
    const { box } = place(scene, { ...placement, x: 0, y: 0 });
    return { box, width: box.max[0] - box.min[0], depth: box.max[1] - box.min[1] };
  });

  const rows: { members: number[]; width: number; depth: number }[] = [{ members: [], width: 0, depth: 0 }];
  const relative: [number, number][] = [];
  let cursorY = 0;
  placements.forEach((_, index) => {
    let row = rows[rows.length - 1];
    const spacing = row.members.length === 0 ? 0 : ARRANGE_GAP_MM;
    if (row.members.length > 0 && row.width + spacing + sizes[index].width > available) {
      cursorY += row.depth + ARRANGE_GAP_MM;
      rows.push({ members: [], width: 0, depth: 0 });
      row = rows[rows.length - 1];
    }
    relative.push([row.width + (row.members.length === 0 ? 0 : ARRANGE_GAP_MM), cursorY]);
    row.width += (row.members.length === 0 ? 0 : ARRANGE_GAP_MM) + sizes[index].width;
    row.depth = Math.max(row.depth, sizes[index].depth);
    row.members.push(index);
  });

  const blockWidth = Math.max(...rows.map((row) => row.width));
  const blockDepth = rows.reduce((total, row) => total + row.depth, 0) + ARRANGE_GAP_MM * (rows.length - 1);
  const originX = (bed.minX + bed.maxX - blockWidth) / 2;
  const originY = (bed.minY + bed.maxY - blockDepth) / 2;
  return placements.map((placement, index) => ({
    ...placement,
    x: originX + relative[index][0] - sizes[index].box.min[0],
    y: originY + relative[index][1] - sizes[index].box.min[1],
  }));
}

export function Plater(props: {
  uploadId: string;
  profiles: ProfileSelection;
  disabled: boolean;
  /** Null while there is no scene, so the worker keeps arranging as before. */
  onPlacements: (placements: ObjectPlacement[] | null) => void;
}) {
  const { uploadId, profiles, disabled, onPlacements } = props;
  const [scene, setScene] = useState<Scene | null>(null);
  const [placements, setPlacements] = useState<Placement[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);
  const [camera, setCamera] = useState<Camera>({ ...ISOMETRIC_VIEW, zoom: 0 });
  const nextId = useRef(0);

  const { machine_profile, process_profile, filament_profile } = profiles;
  useEffect(() => {
    let stale = false;
    setScene(null);
    setFailure(null);
    loadScene(uploadId, { machine_profile, process_profile, filament_profile })
      .then((loaded) => {
        if (stale) return;
        setScene(loaded);
        // Objects arrive placed exactly as imported, which for a loose mesh is
        // wherever its file put it. Arranging on load is what a slice without
        // explicit placement would have done anyway.
        const fresh = loaded.index.objects.map((object) => ({
          id: (nextId.current += 1),
          source: object.index,
          x: 0,
          y: 0,
          rotation: 0,
          scale: 1,
        }));
        setPlacements(arrange(loaded, fresh));
        setSelected(fresh.length === 1 ? fresh[0].id : null);
      })
      .catch((error) => {
        if (stale) return;
        setScene(null);
        setPlacements([]);
        setFailure(
          error instanceof ApiError
            ? { code: error.code, message: error.message }
            : { code: "unexpected_error", message: String(error) },
        );
      });
    return () => {
      stale = true;
    };
  }, [uploadId, machine_profile, process_profile, filament_profile]);

  const placed = useMemo(
    () => (scene ? placements.map((placement) => place(scene, placement)) : []),
    [scene, placements],
  );

  // The plate the slice request will carry. An empty list is the pre-plater
  // behaviour: the worker arranges whatever the model held.
  useEffect(() => {
    if (!scene) {
      onPlacements(null);
      return;
    }
    onPlacements(
      placements.map((placement, index) => ({
        source_object: placement.source,
        transform: placed[index].matrix,
      })),
    );
  }, [scene, placements, placed, onPlacements]);

  const items = useMemo<DrawItem[]>(() => {
    if (!scene) return [];
    const budget = Math.max(1, Math.floor(TRIANGLE_BUDGET / Math.max(1, placements.length)));
    return placements.map((placement, index) => {
      const object = scene.index.objects[placement.source];
      const simplified = object.triangle_count > budget;
      return {
        vertices: simplified ? boxMesh(object.bounding_box) : scene.meshes[placement.source],
        matrix: placed[index].matrix,
        selected: placement.id === selected,
        simplified,
      };
    });
  }, [scene, placements, placed, selected]);

  const update = useCallback(
    (id: number, change: Partial<Placement>) =>
      setPlacements((current) =>
        current.map((placement) => (placement.id === id ? { ...placement, ...change } : placement)),
      ),
    [],
  );

  if (failure) {
    return (
      <p className="failure" data-testid="plater-failure" role="alert">
        <strong>{failure.code}</strong> {failure.message}
      </p>
    );
  }
  if (!scene) {
    return (
      <p data-testid="plater-loading" role="status">
        Inspecting the model…
      </p>
    );
  }

  const bed = bedBounds(scene.index.bed.shape);
  const outside = placed.filter(
    ({ box }) =>
      box.min[0] < bed.minX ||
      box.max[0] > bed.maxX ||
      box.min[1] < bed.minY ||
      box.max[1] > bed.maxY ||
      box.max[2] > scene.index.bed.printable_height,
  ).length;
  const current = placements.findIndex((placement) => placement.id === selected);
  const selection = current < 0 ? null : placements[current];
  const size = current < 0 ? null : placed[current].box;

  return (
    <div className="plater">
      <PlaterCanvas
        bed={scene.index.bed.shape}
        items={items}
        camera={camera}
        disabled={disabled}
        label={`Build plate, ${placements.length} object${placements.length === 1 ? "" : "s"} on a ${
          bed.maxX - bed.minX
        } by ${bed.maxY - bed.minY} millimetre bed`}
        onCamera={setCamera}
        onSelect={(item) => setSelected(item === null ? null : placements[item].id)}
        onMove={(item, dx, dy) =>
          update(placements[item].id, { x: placements[item].x + dx, y: placements[item].y + dy })
        }
      />

      <div className="actions plater-views">
        <button type="button" data-testid="plater-view-top" onClick={() => setCamera({ ...TOP_VIEW, zoom: 0 })}>
          Top view
        </button>
        <button type="button" data-testid="plater-view-3d" onClick={() => setCamera({ ...ISOMETRIC_VIEW, zoom: 0 })}>
          3D view
        </button>
        <button type="button" data-testid="plater-fit" onClick={() => setCamera({ ...camera, zoom: 0 })}>
          Fit
        </button>
        <span data-testid="plater-summary">
          {placements.length} object{placements.length === 1 ? "" : "s"} on a{" "}
          {bed.maxX - bed.minX} × {bed.maxY - bed.minY} mm bed
        </span>
      </div>

      {outside > 0 && (
        <p className="failure" data-testid="plater-outside-bed" role="status">
          {outside} object{outside === 1 ? " reaches" : "s reach"} outside the build volume and will
          be refused when sliced.
        </p>
      )}

      <ul className="plater-objects" data-testid="plater-objects">
        {placements.map((placement, index) => (
          <li key={placement.id}>
            <button
              type="button"
              data-testid={`plater-select-${index}`}
              aria-pressed={placement.id === selected}
              onClick={() => setSelected(placement.id)}
            >
              {scene.index.objects[placement.source].name || `Object ${placement.source + 1}`}
              <span className="plater-position">
                {" "}
                {placed[index].box.min[0].toFixed(1)}, {placed[index].box.min[1].toFixed(1)} mm
              </span>
            </button>
          </li>
        ))}
      </ul>

      {/* Keyed by the selection, so switching objects starts the fields fresh
          rather than carrying a half-typed value across. */}
      {selection && size && (
        <div className="plater-transform" key={selection.id}>
          <p data-testid="plater-bounds">
            {(size.max[0] - size.min[0]).toFixed(2)} × {(size.max[1] - size.min[1]).toFixed(2)} ×{" "}
            {(size.max[2] - size.min[2]).toFixed(2)} mm
          </p>
          <Field
            label="X"
            testId="plater-x"
            value={selection.x}
            disabled={disabled}
            onChange={(value) => update(selection.id, { x: value })}
          />
          <Field
            label="Y"
            testId="plater-y"
            value={selection.y}
            disabled={disabled}
            onChange={(value) => update(selection.id, { y: value })}
          />
          <Field
            label="Rotation"
            unit="°"
            testId="plater-rotation"
            value={selection.rotation}
            disabled={disabled}
            onChange={(value) => update(selection.id, { rotation: value })}
          />
          <Field
            label="Scale"
            unit="%"
            testId="plater-scale"
            value={selection.scale * 100}
            disabled={disabled}
            min={1}
            onChange={(value) => update(selection.id, { scale: Math.max(0.01, value / 100) })}
          />
        </div>
      )}

      <div className="actions">
        <button
          type="button"
          data-testid="plater-duplicate"
          disabled={disabled || !selection}
          onClick={() => {
            if (!selection) return;
            const copy = { ...selection, id: (nextId.current += 1) };
            setPlacements(arrange(scene, [...placements, copy]));
            setSelected(copy.id);
          }}
        >
          Duplicate
        </button>
        <button
          type="button"
          data-testid="plater-delete"
          disabled={disabled || !selection}
          onClick={() => {
            if (!selection) return;
            const next = placements.filter((placement) => placement.id !== selection.id);
            setPlacements(next);
            setSelected(next.length ? next[0].id : null);
          }}
        >
          Delete
        </button>
        <button
          type="button"
          data-testid="plater-arrange"
          disabled={disabled || placements.length === 0}
          onClick={() => setPlacements(arrange(scene, placements))}
        >
          Arrange
        </button>
      </div>
    </div>
  );
}

function Field(props: {
  label: string;
  testId: string;
  value: number;
  disabled: boolean;
  unit?: string;
  min?: number;
  onChange: (value: number) => void;
}) {
  // While the field is being typed in it shows exactly what was typed. Echoing
  // the parsed number back instead would swallow a half-written "12." the
  // moment it parsed to 12.
  const [typed, setTyped] = useState<string | null>(null);
  return (
    <label>
      <span>
        {props.label}
        {props.unit ? ` (${props.unit})` : " (mm)"}
      </span>
      <input
        type="number"
        step="any"
        min={props.min}
        data-testid={props.testId}
        disabled={props.disabled}
        value={typed ?? Number(props.value.toFixed(3))}
        onBlur={() => setTyped(null)}
        onChange={(event) => {
          setTyped(event.target.value);
          const parsed = Number(event.target.value);
          if (event.target.value.trim() !== "" && Number.isFinite(parsed)) props.onChange(parsed);
        }}
      />
    </label>
  );
}
