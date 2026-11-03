import { useEffect, useMemo, useRef, useState } from "react";
import { BufferAttribute, BufferGeometry, Group, LineBasicMaterial, LineSegments, Vector3 } from "three";

import { readPreviewIndex, readPreviewPage } from "../api";
import { createViewport, type Viewport } from "../viewport";
import { decodeSegments, pageLayers, roleColor, toolColor, type PreviewIndex } from "./preview";

type ColourMode = "role" | "filament";

const hex = ([r, g, b]: [number, number, number]) => (r << 16) | (g << 8) | b;

// Small enough that the first layers draw quickly on a large print.
const PAGE_BYTES = 8 * 1024 * 1024;

/**
 * Pages the job's preview layer by layer and draws each extrusion segment as a
 * line coloured by its role. The slider limits the visible layers, so the
 * current one is the top of the stack, as in the desktop preview.
 */
export function PreviewView({ jobId }: { jobId: string }) {
  const host = useRef<HTMLDivElement>(null);
  const viewport = useRef<Viewport | null>(null);
  const layers = useRef(new Group());
  const [index, setIndex] = useState<PreviewIndex | null>(null);
  const [loaded, setLoaded] = useState(0);
  const [top, setTop] = useState(0);
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  // A multi-material slice opens coloured by filament, as the desktop does.
  const [mode, setMode] = useState<ColourMode>("role");

  useEffect(() => {
    const created = createViewport(host.current!);
    viewport.current = created;
    created.scene.add(layers.current);
    return () => {
      layers.current.children.forEach((child) => {
        (child as LineSegments).geometry.dispose();
        ((child as LineSegments).material as LineBasicMaterial).dispose();
      });
      layers.current.clear();
      created.dispose();
      viewport.current = null;
    };
  }, []);

  useEffect(() => {
    let stale = false;
    layers.current.clear();
    setIndex(null);
    setLoaded(0);
    setError(null);
    (async () => {
      const read = await readPreviewIndex(jobId);
      if (stale) return;
      setIndex(read);
      setMode(read.tools.length > 1 ? "filament" : "role");
      setTop(read.layers.length - 1);
      const box = read.bounding_box;
      if (box && viewport.current) {
        const center = new Vector3(...box.min).add(new Vector3(...box.max)).multiplyScalar(0.5);
        const size = Math.max(box.max[0] - box.min[0], box.max[1] - box.min[1], box.max[2] - box.min[2], 20);
        viewport.current.frame(center, size * 1.4);
      }
      const colors = new Map(read.roles.map((role) => [role.id, roleColor(role.name)]));
      for (const [start, end] of pageLayers(read.layers, read.segment_bytes, PAGE_BYTES)) {
        const page = decodeSegments(await readPreviewPage(jobId, start, end), read.segment_bytes);
        if (stale) return;
        let segment = 0;
        for (let layer = start; layer <= end; layer++) {
          const count = read.layers[layer].count;
          // One draw per role, tool, and layer, so a role or layer is hidden
          // and the colouring switched without rebuilding buffers.
          const groups = new Map<number, number[]>();
          for (let i = segment; i < segment + count; i++) {
            const key = page.roles[i] * 256 + page.tools[i];
            const list = groups.get(key) ?? [];
            list.push(i);
            groups.set(key, list);
          }
          for (const [key, members] of groups) {
            const role = Math.floor(key / 256);
            const tool = key % 256;
            const positions = new Float32Array(members.length * 6);
            members.forEach((member, i) => positions.set(page.positions.subarray(member * 6, member * 6 + 6), i * 6));
            const geometry = new BufferGeometry();
            geometry.setAttribute("position", new BufferAttribute(positions, 3));
            const roleHex = hex(colors.get(role) ?? [128, 128, 128]);
            const toolHex = hex(toolColor(read, tool));
            const lines = new LineSegments(
              geometry,
              new LineBasicMaterial({ color: read.tools.length > 1 ? toolHex : roleHex }),
            );
            lines.userData = { layer, role, tool, roleHex, toolHex };
            layers.current.add(lines);
          }
          segment += count;
        }
        setLoaded(end + 1);
        viewport.current?.render();
      }
    })().catch((failure) => !stale && setError(String(failure)));
    return () => {
      stale = true;
    };
  }, [jobId]);

  // Visibility and colour follow the slider, the role toggles, and the
  // colouring without refetching.
  useEffect(() => {
    for (const child of layers.current.children) {
      child.visible = child.userData.layer <= top && !hidden.has(child.userData.role);
      ((child as LineSegments).material as LineBasicMaterial).color.setHex(
        mode === "filament" ? child.userData.toolHex : child.userData.roleHex,
      );
    }
    viewport.current?.render();
  }, [top, hidden, loaded, mode]);

  const used = useMemo(() => index?.roles.filter((role) => role.used) ?? [], [index]);
  const total = index?.layers.length ?? 0;

  return (
    <div className="preview">
      <div className="viewport" ref={host} data-testid="preview-view" aria-label="Layer preview" role="img" />
      {error && (
        <p className="failure" role="alert" data-testid="preview-error">
          {error}
        </p>
      )}
      {index && (
        <div className="preview-controls">
          <label className="layer-slider">
            <span>
              Layer <strong data-testid="preview-layer">{total === 0 ? 0 : top + 1}</strong> of{" "}
              <span data-testid="preview-layer-count">{total}</span>
              {total > 0 && <> — Z {index.layers[top].z.toFixed(2)} mm</>}
            </span>
            <input
              type="range"
              min={0}
              max={Math.max(0, total - 1)}
              value={top}
              data-testid="preview-slider"
              aria-label="Highest visible layer"
              onChange={(event) => setTop(Number(event.target.value))}
            />
          </label>
          {loaded < total && <p data-testid="preview-loading">Loading layers {loaded} of {total}…</p>}
          <ul className="legend" data-testid="preview-legend">
            {used.map((role) => {
              const [r, g, b] = roleColor(role.name);
              return (
                <li key={role.id}>
                  <label>
                    <input
                      type="checkbox"
                      checked={!hidden.has(role.id)}
                      onChange={() =>
                        setHidden((current) => {
                          const next = new Set(current);
                          if (next.has(role.id)) next.delete(role.id);
                          else next.add(role.id);
                          return next;
                        })
                      }
                    />
                    <span className="swatch" style={{ background: `rgb(${r}, ${g}, ${b})` }} />
                    {role.name}
                  </label>
                </li>
              );
            })}
          </ul>
          {index.tools.length > 1 && (
            <>
              <div className="field wide">
                <label htmlFor="preview-colour">Colour by</label>
                <select
                  id="preview-colour"
                  data-testid="preview-colour"
                  value={mode}
                  onChange={(event) => setMode(event.target.value as ColourMode)}
                >
                  <option value="filament">Filament</option>
                  <option value="role">Line type</option>
                </select>
              </div>
              <ul className="legend" data-testid="preview-tools">
                {index.tools.map((tool) => {
                  const [r, g, b] = toolColor(index, tool);
                  return (
                    <li key={tool} data-testid="preview-tool" data-tool={tool}>
                      <span className="swatch" style={{ background: `rgb(${r}, ${g}, ${b})` }} />
                      Filament {tool + 1} (T{tool})
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
