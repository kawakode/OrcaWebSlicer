import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Vector3 } from "three";

import { editUpload, readEditReport } from "../api";
import { ApiError, type Job, type Upload } from "../types";
import { itemBounds, type PlateItem } from "./plate";
import {
  cutsItem,
  cutSubmission,
  inheritOverrides,
  layOnFace,
  measure,
  repairSubmission,
  spreadParts,
  type CutKeep,
  type EditSubmission,
} from "./tools";

/** What a click on the plate does while a tool waits for one. */
export type PickMode = "lay" | "measure" | null;

export interface PickHit {
  id: string;
  /** Plate-space point and outward face normal under the pointer. */
  point: Vector3;
  normal: Vector3;
}

/** What the plate draws for the open tool. */
export interface ToolOverlay {
  cut: { itemId: string; height: number } | null;
  points: Vector3[];
}

/**
 * State for the object tools. Laying on a face and measuring stay in the
 * browser; cutting and repairing submit an edit job and replace the item with
 * the parts the worker produced, each loaded like a freshly uploaded model.
 */
export function useObjectTools(options: {
  selected: PlateItem | null;
  update: (id: string, change: Partial<PlateItem>) => void;
  replace: (id: string, parts: PlateItem[]) => void;
  load: (upload: Upload) => Promise<PlateItem[]>;
  wait: (jobId: string) => Promise<Job>;
  onFailure: (error: unknown) => void;
}) {
  const [pickMode, setPickMode] = useState<PickMode>(null);
  const [points, setPoints] = useState<Vector3[]>([]);
  // The cut tool is open while a height is set.
  const [cutHeight, setCutHeight] = useState<number | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const latest = useRef(options);
  latest.current = options;

  // Laying on a face and cutting both apply to the selected item only.
  const selectedId = options.selected?.id;
  useEffect(() => {
    setPickMode((mode) => (mode === "lay" ? null : mode));
    setCutHeight(null);
  }, [selectedId]);

  const onPick = useCallback(
    (hit: PickHit) => {
      const { selected, update } = latest.current;
      if (pickMode === "lay" && selected && hit.id === selected.id) {
        update(selected.id, { rotation: layOnFace(selected, hit.normal) });
        setPickMode(null);
      } else if (pickMode === "measure") {
        setPoints((current) => (current.length >= 2 ? [hit.point] : [...current, hit.point]));
      }
    },
    [pickMode],
  );

  const choose = useCallback((mode: PickMode) => {
    setPickMode((current) => (current === mode ? null : mode));
    setPoints([]);
  }, []);

  const apply = useCallback(async (item: PlateItem, submission: EditSubmission) => {
    const { replace, load, wait, onFailure } = latest.current;
    setRunning(submission.tool === "cut" ? "Cutting…" : "Repairing…");
    setNotice(null);
    try {
      const job = await wait((await editUpload(item.uploadId, submission)).job_id);
      if (job.state !== "succeeded") {
        throw new ApiError(job.error?.code ?? "edit_failed", job.error?.message ?? "The object could not be edited.", 0);
      }
      const loaded = (await Promise.all((job.derived_uploads ?? []).map(load))).flat();
      const parts = inheritOverrides(item, submission.tool, loaded);
      replace(item.id, spreadParts(parts));
      setCutHeight(null);
      setNotice(await describeEdit(job, parts.length));
    } catch (error) {
      onFailure(error);
    } finally {
      setRunning(null);
    }
  }, []);

  const overlay = useMemo<ToolOverlay>(
    () => ({ cut: selectedId && cutHeight !== null ? { itemId: selectedId, height: cutHeight } : null, points }),
    [selectedId, cutHeight, points],
  );
  return { pickMode, choose, onPick, overlay, points, cutHeight, setCutHeight, running, notice, apply };
}

async function describeEdit(job: Job, parts: number): Promise<string> {
  const report = await readEditReport(job.job_id);
  if (report.tool === "cut") return `Cut into ${parts} ${parts === 1 ? "part" : "parts"}.`;
  const repair = report.repair;
  if (!repair || (repair.open_edges_before === 0 && repair.facets_reversed === 0 && repair.facets_removed === 0)) {
    return "The mesh had nothing to repair.";
  }
  return (
    `Repaired: open edges ${repair.open_edges_before} → ${repair.open_edges_after}, ` +
    `${repair.facets_added} facets added, ${repair.facets_removed} removed, ${repair.facets_reversed} flipped.`
  );
}

export function ObjectTools(props: { item: PlateItem; tools: ReturnType<typeof useObjectTools>; disabled: boolean }) {
  const { item, tools } = props;
  const [keep, setKeep] = useState<CutKeep>("both");
  const [flipUpper, setFlipUpper] = useState(false);
  const box = itemBounds(item);
  const height = tools.cutHeight;
  const busy = props.disabled || tools.running !== null;
  const measured = tools.points.length === 2 ? measure(tools.points[0], tools.points[1]) : null;

  return (
    <fieldset className="object-tools" data-testid="object-tools">
      <legend>Tools</legend>
      <div className="row">
        <button
          type="button"
          data-testid="tool-cut"
          aria-pressed={height !== null}
          disabled={busy}
          onClick={() => tools.setCutHeight(height === null ? (box.minZ + box.maxZ) / 2 : null)}
        >
          Cut
        </button>
        <button type="button" data-testid="tool-repair" disabled={busy} onClick={() => tools.apply(item, repairSubmission(item))}>
          Repair
        </button>
        <button
          type="button"
          data-testid="tool-lay"
          aria-pressed={tools.pickMode === "lay"}
          disabled={busy}
          onClick={() => tools.choose("lay")}
        >
          Lay on face
        </button>
        <button
          type="button"
          data-testid="tool-measure"
          aria-pressed={tools.pickMode === "measure"}
          onClick={() => tools.choose("measure")}
        >
          Measure
        </button>
      </div>

      {height !== null && (
        <div className="cut-tool" data-testid="cut-panel">
          <div className="field">
            <label htmlFor="cut-height">Height</label>
            <input
              id="cut-height"
              type="number"
              step={0.5}
              min={box.minZ}
              max={box.maxZ}
              data-testid="cut-height"
              value={Math.round(height * 1000) / 1000}
              onChange={(event) => tools.setCutHeight(Number(event.target.value))}
            />
            <span className="unit">mm</span>
          </div>
          <div className="field">
            <label htmlFor="cut-keep">Keep</label>
            <select id="cut-keep" data-testid="cut-keep" value={keep} onChange={(event) => setKeep(event.target.value as CutKeep)}>
              <option value="both">Both parts</option>
              <option value="upper">Upper part</option>
              <option value="lower">Lower part</option>
            </select>
          </div>
          <label className="check">
            <input type="checkbox" data-testid="cut-flip" checked={flipUpper} onChange={(event) => setFlipUpper(event.target.checked)} />
            Turn the upper part upside down
          </label>
          <button
            type="button"
            data-testid="cut-apply"
            disabled={busy || !cutsItem(item, height)}
            onClick={() => tools.apply(item, cutSubmission(item, height, keep, flipUpper))}
          >
            Cut at {height.toFixed(2)} mm
          </button>
        </div>
      )}

      {tools.pickMode === "lay" && <p className="hint">Click the face of the object that should rest on the bed.</p>}
      {tools.pickMode === "measure" && (
        <p className="hint" data-testid="measure-result" aria-live="polite">
          {measured
            ? `Distance ${measured.distance.toFixed(2)} mm (X ${measured.dx.toFixed(2)}, Y ${measured.dy.toFixed(2)}, Z ${measured.dz.toFixed(2)})`
            : tools.points.length === 1
              ? "Click a second point."
              : "Click two points on the plate's objects."}
        </p>
      )}
      {tools.running && <p data-testid="tool-running">{tools.running}</p>}
      {tools.notice && (
        <p data-testid="tool-notice" role="status">
          {tools.notice}
        </p>
      )}
    </fieldset>
  );
}
