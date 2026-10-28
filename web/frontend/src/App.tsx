import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Vector3 } from "three";

import {
  artifactUrl,
  cancelJob,
  listProfiles,
  readEffectiveProfile,
  readInspection,
  readJob,
  readMesh,
  readSettingsCatalog,
  retryJob,
  submitJob,
  uploadModel,
} from "./api";
import { decodeObjectMesh } from "./plater/mesh";
import {
  arrange,
  bedBounds,
  fitsBed,
  itemBounds,
  parseBed,
  placementFromMatrix,
  plateObjects,
  type Bed,
  type PlateItem,
} from "./plater/plate";
import { PlaterView } from "./plater/PlaterView";
import { PreviewView } from "./preview/PreviewView";
import { SettingsForm } from "./settings/SettingsForm";
import {
  ApiError,
  TERMINAL_STATES,
  type Job,
  type ProfileCatalog,
  type ProfileEntry,
  type ProfileValue,
  type SettingsCatalog,
} from "./types";

const POLL_INTERVAL_MS = 400;
const NUDGE_MM = 1;
const NUDGE_LARGE_MM = 10;
const DUPLICATE_OFFSET_MM = 10;

const EMPTY_CATALOG: ProfileCatalog = { machine: [], process: [], filament: [] };
const DEFAULT_BED: Bed = parseBed(["0x0", "200x0", "200x200", "0x200"], "200");

type Failure = { code: string; message: string; setting?: string };

function describe(error: unknown): Failure {
  if (error instanceof ApiError) return { code: error.code, message: error.message, setting: error.setting };
  return { code: "unexpected_error", message: String(error) };
}

function firstId(entries: ProfileEntry[], preferred?: string): string {
  const match = preferred ? entries.find((entry) => entry.name === preferred) : undefined;
  return (match ?? entries[0])?.profile_id ?? "";
}

async function waitForJob(jobId: string): Promise<Job> {
  for (;;) {
    const job = await readJob(jobId);
    if (TERMINAL_STATES.includes(job.state)) return job;
    await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

let nextItemId = 0;
const newItemId = () => `item-${++nextItemId}`;

/** Load an upload's display geometry and turn each object instance into a plate item. */
async function loadItems(uploadId: string, inspectionJob: Job, bed: Bed): Promise<{ items: PlateItem[]; loose: boolean }> {
  const job = await waitForJob(inspectionJob.job_id);
  if (job.state !== "succeeded") {
    throw new ApiError(job.error?.code ?? "inspection_failed", job.error?.message ?? "The model could not be read.", 0);
  }
  const [inspection, buffer] = await Promise.all([readInspection(job.job_id), readMesh(job.job_id)]);
  const { minX, maxX, minY, maxY } = bedBounds(bed);
  const items: PlateItem[] = [];
  for (const object of inspection.objects) {
    const mesh = decodeObjectMesh(buffer, object);
    const { min, max } = object.bounding_box;
    const center = new Vector3(...min).add(new Vector3(...max)).multiplyScalar(0.5);
    const base = { uploadId, objectIndex: object.index, name: object.name || "Object", mesh, center };
    if (inspection.declares_plate) {
      // A project keeps the placement its author chose.
      for (const instance of object.instances) {
        items.push({ ...base, id: newItemId(), ...placementFromMatrix(instance.transform, center) });
      }
    } else {
      items.push({ ...base, id: newItemId(), x: (minX + maxX) / 2, y: (minY + maxY) / 2, rotation: [0, 0, 0], scale: 1 });
    }
  }
  return { items, loose: !inspection.declares_plate };
}

export default function App() {
  const [catalog, setCatalog] = useState<ProfileCatalog>(EMPTY_CATALOG);
  const [printers, setPrinters] = useState<ProfileEntry[]>([]);
  const [machine, setMachine] = useState("");
  const [process, setProcess] = useState("");
  const [filament, setFilament] = useState("");
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [settingsCatalog, setSettingsCatalog] = useState<SettingsCatalog | null>(null);
  const [profileValues, setProfileValues] = useState<Record<string, ProfileValue>>({});
  const [bed, setBed] = useState<Bed>(DEFAULT_BED);
  const [items, setItems] = useState<PlateItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState<string[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [view, setView] = useState<"prepare" | "preview">("prepare");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const bedRef = useRef(bed);
  bedRef.current = bed;

  useEffect(() => {
    readSettingsCatalog()
      .then(setSettingsCatalog)
      .catch((error) => setFailure(describe(error)));
  }, []);

  // The selected chain's own values are what a blank override keeps, and its
  // printer defines the bed the plater draws and checks against.
  useEffect(() => {
    if (!machine || !process || !filament) return;
    let stale = false;
    readEffectiveProfile(machine, process, filament)
      .then((effective) => {
        if (stale) return;
        setProfileValues(effective.values);
        setBed(parseBed(effective.bed.printable_area, effective.bed.printable_height));
      })
      .catch((error) => !stale && setFailure(describe(error)));
    return () => {
      stale = true;
    };
  }, [machine, process, filament]);

  // The printer list is the unnarrowed one; the rest is filtered per printer.
  useEffect(() => {
    listProfiles()
      .then((loaded) => {
        setPrinters(loaded.machine);
        setMachine((current) => current || firstId(loaded.machine));
      })
      .catch((error) => setFailure(describe(error)));
  }, []);

  useEffect(() => {
    if (!machine) return;
    let stale = false;
    listProfiles(machine)
      .then((narrowed) => {
        if (stale) return;
        setCatalog(narrowed);
        const printer = narrowed.machine.find((entry) => entry.profile_id === machine);
        setProcess(firstId(narrowed.process, printer?.default_process));
        setFilament(firstId(narrowed.filament));
      })
      .catch((error) => !stale && setFailure(describe(error)));
    return () => {
      stale = true;
    };
  }, [machine]);

  const active = job !== null && !TERMINAL_STATES.includes(job.state);
  const jobId = job?.job_id;
  useEffect(() => {
    if (!jobId || !active) return;
    const timer = window.setInterval(() => {
      readJob(jobId)
        .then((read) => {
          setJob(read);
          if (read.state === "succeeded" && read.artifacts.some((artifact) => artifact.name === "preview")) {
            setView("preview");
          }
        })
        .catch((error) => setFailure(describe(error)));
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [jobId, active]);

  const addFiles = useCallback(async (files: File[]) => {
    setFailure(null);
    for (const file of files) {
      setLoading((current) => [...current, file.name]);
      try {
        const stored = await uploadModel(file);
        const { items: added, loose } = await loadItems(stored.upload_id, stored.inspection, bedRef.current);
        setItems((current) => (loose ? arrange([...current, ...added], bedRef.current) : [...current, ...added]));
        setSelectedId(added[0]?.id ?? null);
      } catch (error) {
        setFailure(describe(error));
      } finally {
        setLoading((current) => current.filter((name) => name !== file.name));
      }
    }
  }, []);

  const update = useCallback((id: string, change: Partial<PlateItem>) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...change } : item)));
  }, []);

  const remove = useCallback((id: string) => {
    setItems((current) => current.filter((item) => item.id !== id));
    setSelectedId(null);
  }, []);

  const duplicate = useCallback((id: string) => {
    setItems((current) => {
      const source = current.find((item) => item.id === id);
      if (!source) return current;
      const copy = { ...source, id: newItemId(), x: source.x + DUPLICATE_OFFSET_MM, y: source.y + DUPLICATE_OFFSET_MM };
      setSelectedId(copy.id);
      return [...current, copy];
    });
  }, []);

  const selected = items.find((item) => item.id === selectedId) ?? null;

  const onPlateKey = useCallback(
    (event: React.KeyboardEvent) => {
      if (!selected || (event.target as HTMLElement).closest("input, select, textarea")) return;
      const step = event.shiftKey ? NUDGE_LARGE_MM : NUDGE_MM;
      const moves: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      };
      if (event.key in moves) {
        const [dx, dy] = moves[event.key];
        update(selected.id, { x: selected.x + dx, y: selected.y + dy });
      } else if (event.key === "Delete" || event.key === "Backspace") {
        remove(selected.id);
      } else if (event.key.toLowerCase() === "d" && (event.ctrlKey || event.metaKey)) {
        duplicate(selected.id);
      } else if (event.key.toLowerCase() === "r") {
        update(selected.id, { rotation: [selected.rotation[0], selected.rotation[1], (selected.rotation[2] + 90) % 360] });
      } else if (event.key === "Escape") {
        setSelectedId(null);
      } else {
        return;
      }
      event.preventDefault();
    },
    [selected, update, remove, duplicate],
  );

  const outside = useMemo(() => items.filter((item) => !fitsBed(item, bed)), [items, bed]);

  const slice = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      const declared = Object.fromEntries(Object.entries(settings).filter(([, value]) => value.trim() !== ""));
      setJob(
        await submitJob({
          objects: plateObjects(items),
          machine_profile: machine,
          process_profile: process,
          filament_profile: filament,
          settings: declared,
        }),
      );
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, [items, settings, machine, process, filament]);

  const run = useCallback(async (action: () => Promise<Job>) => {
    setBusy(true);
    setFailure(null);
    try {
      setJob(await action());
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const ready =
    items.length > 0 && outside.length === 0 && loading.length === 0 && Boolean(machine && process && filament) && !busy && !active;
  const previewable = job?.state === "succeeded" && job.artifacts.some((artifact) => artifact.name === "preview");

  return (
    <div className="app">
      <header className="topbar">
        <h1>OrcaWebSlicer</h1>
        <nav className="tabs" role="tablist" aria-label="Workspace">
          <button
            type="button"
            role="tab"
            aria-selected={view === "prepare"}
            data-testid="tab-prepare"
            onClick={() => setView("prepare")}
          >
            Prepare
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "preview"}
            data-testid="tab-preview"
            disabled={!previewable}
            onClick={() => setView("preview")}
          >
            Preview
          </button>
        </nav>
      </header>

      <main className="workspace">
        <section className="stage" onKeyDown={onPlateKey} tabIndex={0} aria-label="Plate workspace">
          {view === "prepare" || !previewable ? (
            <PlaterView
              bed={bed}
              items={items}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onMove={(id, x, y) => update(id, { x, y })}
            />
          ) : (
            <PreviewView jobId={job!.job_id} />
          )}
        </section>

        <aside className="sidebar">
          <section>
            <h2>Plate</h2>
            <input
              type="file"
              accept=".stl,.obj,.3mf"
              multiple
              data-testid="file-input"
              aria-label="Add models"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                event.target.value = "";
                if (files.length > 0) void addFiles(files);
              }}
            />
            {loading.length > 0 && <p data-testid="loading-models">Reading {loading.join(", ")}…</p>}
            <ul className="objects" role="listbox" aria-label="Objects on the plate" data-testid="object-list">
              {items.map((item) => {
                const box = itemBounds(item);
                return (
                  <li
                    key={item.id}
                    role="option"
                    aria-selected={item.id === selectedId}
                    tabIndex={0}
                    data-testid="plate-item"
                    data-bounds={[box.minX, box.minY, box.minZ, box.maxX, box.maxY, box.maxZ].map((v) => v.toFixed(3)).join(",")}
                    onClick={() => setSelectedId(item.id)}
                    onKeyDown={(event) => event.key === "Enter" && setSelectedId(item.id)}
                  >
                    <span>{item.name}</span>
                    <span className="dimensions">
                      {(box.maxX - box.minX).toFixed(1)} × {(box.maxY - box.minY).toFixed(1)} × {(box.maxZ - box.minZ).toFixed(1)} mm
                    </span>
                    {!fitsBed(item, bed) && <span className="badge">outside</span>}
                  </li>
                );
              })}
            </ul>
            <div className="row">
              <button type="button" data-testid="arrange" disabled={items.length === 0} onClick={() => setItems(arrange(items, bed))}>
                Arrange
              </button>
            </div>
            {selected && (
              <ObjectPanel
                item={selected}
                onChange={(change) => update(selected.id, change)}
                onDuplicate={() => duplicate(selected.id)}
                onDelete={() => remove(selected.id)}
              />
            )}
            {outside.length > 0 && (
              <p className="failure" data-testid="outside-warning" role="status">
                {outside.length === 1 ? "One object is" : `${outside.length} objects are`} outside the printable volume.
              </p>
            )}
          </section>

          <section>
            <h2>Profiles</h2>
            <ProfileSelect label="Printer" testId="printer-select" value={machine} entries={printers} onChange={setMachine} />
            <ProfileSelect label="Process" testId="process-select" value={process} entries={catalog.process} onChange={setProcess} />
            <ProfileSelect label="Filament" testId="filament-select" value={filament} entries={catalog.filament} onChange={setFilament} />
          </section>

          <section>
            <details open>
              <summary>
                <h2>Settings</h2>
              </summary>
              {settingsCatalog && (
                <SettingsForm
                  catalog={settingsCatalog}
                  profileValues={profileValues}
                  overrides={settings}
                  invalidKey={failure?.setting ?? (job?.state === "failed" ? job.error?.setting : undefined)}
                  onChange={(key, value) => setSettings((current) => ({ ...current, [key]: value }))}
                />
              )}
            </details>
          </section>

          <section className="actions">
            <button type="button" data-testid="slice" disabled={!ready} onClick={slice}>
              Slice plate
            </button>
            <button
              type="button"
              data-testid="cancel"
              disabled={!active || busy}
              onClick={() => job && run(() => cancelJob(job.job_id))}
            >
              Cancel
            </button>
            <button
              type="button"
              data-testid="retry"
              disabled={!job || active || busy}
              onClick={() => job && run(() => retryJob(job.job_id))}
            >
              Retry
            </button>
            {job?.state === "succeeded" && (
              <a data-testid="download-gcode" href={artifactUrl(job.job_id, "gcode")} download>
                Download G-code
              </a>
            )}
            {job?.artifacts.some((artifact) => artifact.name === "result") && (
              <a data-testid="download-result" href={artifactUrl(job.job_id, "result")} download>
                Download report
              </a>
            )}
          </section>

          {failure && (
            <p className="failure" data-testid="failure" role="alert">
              <strong>{failure.code}</strong> {failure.message}
            </p>
          )}

          {job && <JobPanel job={job} />}
        </aside>
      </main>
    </div>
  );
}

function NumberField(props: {
  label: string;
  testId: string;
  value: number;
  unit: string;
  step?: number;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(props.value));
  useEffect(() => setDraft(String(Math.round(props.value * 1000) / 1000)), [props.value]);
  const commit = () => {
    const parsed = Number(draft);
    if (Number.isFinite(parsed)) props.onCommit(parsed);
    else setDraft(String(props.value));
  };
  const id = `field-${props.testId}`;
  return (
    <div className="field">
      <label htmlFor={id}>{props.label}</label>
      <input
        id={id}
        type="number"
        step={props.step ?? 1}
        data-testid={props.testId}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => event.key === "Enter" && commit()}
      />
      <span className="unit">{props.unit}</span>
    </div>
  );
}

function ObjectPanel(props: {
  item: PlateItem;
  onChange: (change: Partial<PlateItem>) => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const { item, onChange } = props;
  const rotate = (axis: 0 | 1 | 2, degrees: number) => {
    const rotation = [...item.rotation] as [number, number, number];
    rotation[axis] = degrees;
    onChange({ rotation });
  };
  return (
    <fieldset className="object-panel" data-testid="object-panel">
      <legend>{item.name}</legend>
      <NumberField label="X" testId="object-x" value={item.x} unit="mm" onCommit={(x) => onChange({ x })} />
      <NumberField label="Y" testId="object-y" value={item.y} unit="mm" onCommit={(y) => onChange({ y })} />
      <NumberField label="Rotate X" testId="object-rotate-x" value={item.rotation[0]} unit="°" step={15} onCommit={(v) => rotate(0, v)} />
      <NumberField label="Rotate Y" testId="object-rotate-y" value={item.rotation[1]} unit="°" step={15} onCommit={(v) => rotate(1, v)} />
      <NumberField label="Rotate Z" testId="object-rotate-z" value={item.rotation[2]} unit="°" step={15} onCommit={(v) => rotate(2, v)} />
      <NumberField
        label="Scale"
        testId="object-scale"
        value={item.scale * 100}
        unit="%"
        step={5}
        onCommit={(percent) => percent > 0 && onChange({ scale: percent / 100 })}
      />
      <div className="row">
        <button type="button" data-testid="duplicate" onClick={props.onDuplicate}>
          Duplicate
        </button>
        <button type="button" className="secondary" data-testid="delete" onClick={props.onDelete}>
          Delete
        </button>
      </div>
      <p className="hint">Drag the selected object on the plate, or use the arrow keys. R rotates by 90°.</p>
    </fieldset>
  );
}

function ProfileSelect(props: {
  label: string;
  testId: string;
  value: string;
  entries: ProfileEntry[];
  onChange: (value: string) => void;
}) {
  const id = `profile-${props.testId}`;
  return (
    <div className="field wide">
      <label htmlFor={id}>{props.label}</label>
      <select
        id={id}
        data-testid={props.testId}
        value={props.value}
        disabled={props.entries.length === 0}
        onChange={(event) => props.onChange(event.target.value)}
      >
        {props.entries.map((entry) => (
          <option key={entry.profile_id} value={entry.profile_id}>
            {entry.name}
          </option>
        ))}
      </select>
    </div>
  );
}

function JobPanel({ job }: { job: Job }) {
  const percent = useMemo(() => Math.min(100, Math.max(0, job.progress.percent)), [job]);
  // Progress is monotonic per the worker contract, so the highest value seen is
  // the one to show even if a poll lands after a terminal transition.
  const peak = useRef(0);
  peak.current = job.state === "queued" ? 0 : Math.max(peak.current, percent);

  return (
    <section className="job">
      <h2>Job</h2>
      <p>
        <code data-testid="job-id">{job.job_id}</code> — <strong data-testid="job-state">{job.state}</strong>
        {job.retry_of && <span data-testid="retry-of"> (retry of {job.retry_of})</span>}
      </p>
      <progress data-testid="job-progress" value={peak.current} max={100} aria-label="Slicing progress" />
      <p data-testid="job-stage" aria-live="polite">
        {job.progress.stage} {peak.current}% {job.progress.message}
      </p>
      {job.error && (
        <p className="failure" data-testid="job-error" role="alert">
          <strong>{job.error.code}</strong> {job.error.message}
        </p>
      )}
      {job.warnings.length > 0 && (
        <ul data-testid="job-warnings">
          {job.warnings.map((warning, index) => (
            <li key={`${warning.code}-${index}`}>
              <strong>{warning.code}</strong> {warning.message}
            </li>
          ))}
        </ul>
      )}
      {Object.keys(job.profiles ?? {}).length > 0 && (
        <dl className="chain" data-testid="job-profiles">
          {(["machine", "process", "filament"] as const).map((kind) =>
            job.profiles[kind] ? (
              <div key={kind}>
                <dt>{kind}</dt>
                <dd data-testid={`job-chain-${kind}`}>{job.profiles[kind]!.inherits_chain.join(" → ")}</dd>
              </div>
            ) : null,
          )}
        </dl>
      )}
      {Object.keys(job.request.settings ?? {}).length > 0 && (
        <p data-testid="job-overrides">
          Overrides:{" "}
          {Object.entries(job.request.settings)
            .map(([key, value]) => `${key} = ${value}`)
            .join(", ")}
        </p>
      )}
      {job.timing && <p data-testid="job-timing">Sliced in {job.timing.duration_ms} ms</p>}
    </section>
  );
}
