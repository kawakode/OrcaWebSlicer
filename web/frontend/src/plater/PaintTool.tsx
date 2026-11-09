import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { PaintDisplay, PaintHit } from "./PlaterView";
import { applyStroke, BLOCK, clearLayer, ENFORCE, type ItemPaint, type PaintKind } from "./paint";
import type { PlateItem } from "./plate";

const KIND_LABELS: Record<PaintKind, string> = {
  supports: "Supports",
  seam: "Seam",
  color: "Colour",
  fuzzy_skin: "Fuzzy skin",
};

export interface PaintSettings {
  kind: PaintKind;
  /** The state painted: 1 enforces, 2 blocks, a filament number for colour; 0 erases. */
  state: number;
  fill: boolean;
  /** Brush radius in plate millimetres. */
  radius: number;
  /** Largest angle between neighbouring faces a fill crosses, in degrees. */
  angle: number;
}

/**
 * State for the paint tool. A stroke repaints the selected item and every
 * other instance of the same object with the same painting, so instances keep
 * sharing one painting as the desktop's do.
 */
export function usePaintTool(options: {
  selected: PlateItem | null;
  /** Replace the painting of the selected item's instances, from their current one. */
  repaint: (item: PlateItem, change: (paint: ItemPaint | undefined) => ItemPaint | undefined) => void;
  /** Colour of a filament, for colour painting. */
  filamentColour: (filament: number) => number | undefined;
  /** Whether the plate shows its filament painting while the tool is closed. */
  showColours: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<PaintSettings>({ kind: "supports", state: ENFORCE, fill: false, radius: 2, angle: 30 });
  const latest = useRef(options);
  latest.current = options;

  const selectedId = options.selected?.id;
  useEffect(() => setOpen(false), [selectedId]);

  const onPaint = useCallback(
    (hit: PaintHit) => {
      const { selected, repaint } = latest.current;
      if (!selected || hit.id !== selected.id) return;
      repaint(selected, (paint) =>
        applyStroke(paint, selected.mesh, {
          kind: settings.kind,
          state: settings.state,
          triangle: hit.triangle,
          point: hit.point,
          radius: settings.radius / selected.scale,
          fill: settings.fill,
          angle: settings.angle,
        }),
      );
    },
    [settings],
  );

  const clear = useCallback(() => {
    const { selected, repaint } = latest.current;
    if (selected) repaint(selected, (paint) => clearLayer(paint, settings.kind, selected.mesh.indices.length / 3));
  }, [settings.kind]);

  const { filamentColour, showColours } = options;
  const display = useMemo<PaintDisplay | undefined>(
    () =>
      open
        ? { kind: settings.kind, colour: filamentColour }
        : showColours
          ? { kind: "color", colour: filamentColour }
          : undefined,
    [open, settings.kind, filamentColour, showColours],
  );

  return { open, setOpen, settings, setSettings, onPaint: open ? onPaint : undefined, clear, display };
}

export function PaintPanel(props: { tools: ReturnType<typeof usePaintTool>; filaments: number; supportEnabled: boolean }) {
  const { settings, setSettings, clear } = props.tools;
  const change = (patch: Partial<PaintSettings>) => setSettings((current) => ({ ...current, ...patch }));
  const states: [number, string][] =
    settings.kind === "color"
      ? [...Array.from({ length: Math.max(1, props.filaments) }, (_, index) => [index + 1, `Filament ${index + 1}`] as [number, string]), [0, "Erase"]]
      : settings.kind === "fuzzy_skin"
        ? [[ENFORCE, "Fuzzy skin"], [0, "Erase"]]
        : [[ENFORCE, "Enforce"], [BLOCK, "Block"], [0, "Erase"]];

  return (
    <div className="paint-tool" data-testid="paint-panel">
      <div className="field">
        <label htmlFor="paint-kind">Paint</label>
        <select
          id="paint-kind"
          data-testid="paint-kind"
          value={settings.kind}
          onChange={(event) => {
            const kind = event.target.value as PaintKind;
            change({ kind, state: kind === "color" ? Math.min(2, Math.max(1, props.filaments)) : ENFORCE });
          }}
        >
          {(Object.keys(KIND_LABELS) as PaintKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {KIND_LABELS[kind]}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="paint-state">With</label>
        <select id="paint-state" data-testid="paint-state" value={settings.state} onChange={(event) => change({ state: Number(event.target.value) })}>
          {states.map(([state, label]) => (
            <option key={state} value={state}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="paint-mode">Tool</label>
        <select id="paint-mode" data-testid="paint-mode" value={settings.fill ? "fill" : "brush"} onChange={(event) => change({ fill: event.target.value === "fill" })}>
          <option value="brush">Brush</option>
          <option value="fill">Fill</option>
        </select>
      </div>
      {settings.fill ? (
        <div className="field">
          <label htmlFor="paint-angle">Angle</label>
          <input id="paint-angle" type="number" min={0} max={90} step={5} data-testid="paint-angle" value={settings.angle} onChange={(event) => change({ angle: Number(event.target.value) })} />
          <span className="unit">°</span>
        </div>
      ) : (
        <div className="field">
          <label htmlFor="paint-radius">Radius</label>
          <input id="paint-radius" type="number" min={0.1} max={50} step={0.5} data-testid="paint-radius" value={settings.radius} onChange={(event) => change({ radius: Number(event.target.value) })} />
          <span className="unit">mm</span>
        </div>
      )}
      <button type="button" data-testid="paint-clear" onClick={clear}>
        Clear {KIND_LABELS[settings.kind].toLowerCase()}
      </button>
      <p className="hint">
        {settings.fill
          ? "Click a face to fill it and its neighbours up to the angle."
          : "Drag across the object to paint; drag elsewhere to turn the view."}
        {settings.kind === "supports" && !props.supportEnabled && " Enforcers only take effect once support is enabled."}
        {settings.kind === "color" && props.filaments < 2 && " Add a filament to paint with more than one."}
      </p>
    </div>
  );
}
