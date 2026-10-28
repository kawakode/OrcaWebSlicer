import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Vector3 } from "three";

import {
  artifactUrl,
  cancelBatch,
  cancelJob,
  listProfiles,
  readEffectiveProfile,
  readInspection,
  readJob,
  readMesh,
  readSettingsCatalog,
  retryJob,
  submitBatch,
  submitJob,
  uploadModel,
} from "./api";
import { decodeObjectMesh } from "./plater/mesh";
import {
  addToPlates,
  arrange,
  bedBounds,
  fitsBed,
  itemBounds,
  parseBed,
  placementFromMatrix,
  plateLabel,
  plateObjects,
  type Bed,
  type LoadedItem,
  type Plate,
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

const isActive = (job: Job | null) => job !== null && !TERMINAL_STATES.includes(job.state);
const hasPreview = (job: Job | null) =>
  job?.state === "succeeded" && job.artifacts.some((artifact) => artifact.name === "preview");

let nextItemId = 0;
const newItemId = () => `item-${++nextItemId}`;
let nextPlateId = 0;
const newPlate = (): Plate => ({
  id: `plate-${++nextPlateId}`,
  name: "",
  items: [],
  job: null,
});

/**
 * Load an upload's display geometry and turn each object instance into a plate
 * item, on the plate of the file that holds it.
 */
async function loadItems(
  uploadId: string,
  inspectionJob: Job,
  bed: Bed,
): Promise<{ items: LoadedItem[]; names: string[]; loose: boolean }> {
  const job = await waitForJob(inspectionJob.job_id);
  if (job.state !== "succeeded") {
    throw new ApiError(job.error?.code ?? "inspection_failed", job.error?.message ?? "The model could not be read.", 0);
  }
  const [inspection, buffer] = await Promise.all([readInspection(job.job_id), readMesh(job.job_id)]);
  const { minX, maxX, minY, maxY } = bedBounds(bed);
  const items: LoadedItem[] = [];
  for (const object of inspection.objects) {
    const mesh = decodeObjectMesh(buffer, object);
    const { min, max } = object.bounding_box;
    const center = new Vector3(...min).add(new Vector3(...max)).multiplyScalar(0.5);
    const base = {
      uploadId,
      objectIndex: object.index,
      name: object.name || "Object",
      mesh,
      center,
    };
    const centred = {
      x: (minX + maxX) / 2,
      y: (minY + maxY) / 2,
      rotation: [0, 0, 0] as [number, number, number],
      scale: 1,
    };
    if (inspection.declares_plate) {
      // A project keeps the placement its author chose, plate by plate. An
      // instance the project left off every plate joins the first one.
      for (const instance of object.instances) {
        const placement = instance.plate === null ? centred : placementFromMatrix(instance.transform, center);
        items.push({
          item: { ...base, id: newItemId(), ...placement },
          plate: instance.plate ?? 1,
        });
      }
    } else {
      items.push({ item: { ...base, id: newItemId(), ...centred }, plate: 1 });
    }
  }
  const names = (inspection.plates ?? []).map((plate) => plate.name);
  return { items, names, loose: !inspection.declares_plate };
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
  const [plates, setPlates] = useState<Plate[]>(() => [newPlate()]);
  const [currentId, setCurrentId] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState<string[]>([]);
  const [view, setView] = useState<"prepare" | "preview">("prepare");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const bedRef = useRef(bed);
  bedRef.current = bed;

  // A removed plate falls back to the first one.
  const currentIndex = Math.max(
    0,
    plates.findIndex((plate) => plate.id === currentId),
  );
  const plate = plates[currentIndex];
  const items = plate.items;
  const job = plate.job;
  const plateRef = useRef(plate.id);
  plateRef.current = plate.id;
  const jobRef = useRef(job?.job_id);
  jobRef.current = job?.job_id;

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

  const setPlateJob = useCallback((plateId: string, assigned: Job) => {
    setPlates((current) => current.map((entry) => (entry.id === plateId ? { ...entry, job: assigned } : entry)));
  }, []);

  // Every plate's running job is followed, so a whole project sliced at once
  // reports each plate as it finishes.
  const activeJobs = plates.flatMap((entry) => (isActive(entry.job) ? [entry.job!.job_id] : [])).join(",");
  useEffect(() => {
    if (!activeJobs) return;
    const timer = window.setInterval(() => {
      for (const jobId of activeJobs.split(",")) {
        readJob(jobId)
          .then((read) => {
            setPlates((current) =>
              current.map((entry) => (entry.job?.job_id === read.job_id ? { ...entry, job: read } : entry)),
            );
            if (read.job_id === jobRef.current && hasPreview(read)) setView("preview");
          })
          .catch((error) => setFailure(describe(error)));
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [activeJobs]);

  const addFiles = useCallback(async (files: File[]) => {
    setFailure(null);
    for (const file of files) {
      setLoading((current) => [...current, file.name]);
      try {
        const stored = await uploadModel(file);
        const { items: added, names, loose } = await loadItems(stored.upload_id, stored.inspection, bedRef.current);
        // A file lands on the plate shown now, and a project's later plates on
        // the plates after it.
        setPlates((current) => {
          const start = Math.max(
            0,
            current.findIndex((entry) => entry.id === plateRef.current),
          );
          const placed = addToPlates(current, start, added, names, newPlate);
          return loose
            ? placed.map((entry, index) =>
                index === start ? { ...entry, items: arrange(entry.items, bedRef.current) } : entry,
              )
            : placed;
        });
        setSelectedId(added[0]?.item.id ?? null);
      } catch (error) {
        setFailure(describe(error));
      } finally {
        setLoading((current) => current.filter((name) => name !== file.name));
      }
    }
  }, []);

  /** Change the items of the plate shown now. */
  const editItems = useCallback((change: (current: PlateItem[]) => PlateItem[]) => {
    setPlates((current) =>
      current.map((entry) => (entry.id === plateRef.current ? { ...entry, items: change(entry.items) } : entry)),
    );
  }, []);

  const update = useCallback(
    (id: string, change: Partial<PlateItem>) => {
      editItems((current) => current.map((item) => (item.id === id ? { ...item, ...change } : item)));
    },
    [editItems],
  );

  const remove = useCallback(
    (id: string) => {
      editItems((current) => current.filter((item) => item.id !== id));
      setSelectedId(null);
    },
    [editItems],
  );

  const duplicate = useCallback(
    (id: string) => {
      const source = items.find((item) => item.id === id);
      if (!source) return;
      const copy = {
        ...source,
        id: newItemId(),
        x: source.x + DUPLICATE_OFFSET_MM,
        y: source.y + DUPLICATE_OFFSET_MM,
      };
      editItems((current) => [...current, copy]);
      setSelectedId(copy.id);
    },
    [items, editItems],
  );

  /** Move an item to another plate, at the same place on that plate's bed. */
  const moveToPlate = useCallback((id: string, targetId: string) => {
    setPlates((current) => {
      const moved = current.find((entry) => entry.id === plateRef.current)?.items.find((item) => item.id === id);
      if (!moved || targetId === plateRef.current) return current;
      return current.map((entry) =>
        entry.id === plateRef.current
          ? { ...entry, items: entry.items.filter((item) => item.id !== id) }
          : entry.id === targetId
            ? { ...entry, items: [...entry.items, moved] }
            : entry,
      );
    });
    setSelectedId(null);
  }, []);

  const showPlate = useCallback((plateId: string) => {
    setCurrentId(plateId);
    setSelectedId(null);
  }, []);

  const addPlate = useCallback(() => {
    const added = newPlate();
    setPlates((current) => [...current, added]);
    showPlate(added.id);
  }, [showPlate]);

  const removePlate = useCallback(() => {
    const neighbour = plates[currentIndex - 1] ?? plates[currentIndex + 1];
    if (!neighbour) return;
    setPlates((current) => current.filter((entry) => entry.id !== plate.id));
    showPlate(neighbour.id);
  }, [plates, plate, currentIndex, showPlate]);

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
  const outsideAnywhere = useMemo(
    () => plates.some((entry) => entry.items.some((item) => !fitsBed(item, bed))),
    [plates, bed],
  );

  const submission = () => ({
    machine_profile: machine,
    process_profile: process,
    filament_profile: filament,
    settings: Object.fromEntries(Object.entries(settings).filter(([, value]) => value.trim() !== "")),
  });

  const run = useCallback(async (action: () => Promise<void>) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const slice = () =>
    run(async () => {
      const target = plate;
      setPlateJob(
        target.id,
        await submitJob({
          ...submission(),
          objects: plateObjects(target.items),
          plate: currentIndex + 1,
          plate_name: target.name || undefined,
          plate_index: target.sourcePlate,
        }),
      );
    });

  // Empty plates are left out, and the rest keep their numbers.
  const sliceAll = () =>
    run(async () => {
      const numbered = plates
        .map((entry, index) => ({ entry, number: index + 1 }))
        .filter(({ entry }) => entry.items.length > 0);
      const batch = await submitBatch({
        ...submission(),
        plates: numbered.map(({ entry, number }) => ({
          objects: plateObjects(entry.items),
          plate: number,
          plate_index: entry.sourcePlate,
          name: entry.name || undefined,
        })),
      });
      for (const { entry, number } of numbered) {
        const assigned = batch.jobs.find((candidate) => candidate.request.plate === number);
        if (assigned) setPlateJob(entry.id, assigned);
      }
    });

  // A whole batch is canceled at once; a plate sliced on its own by itself.
  const cancelAll = () =>
    run(async () => {
      const active = plates.flatMap((entry) => (isActive(entry.job) ? [entry.job!] : []));
      const batches = new Set(active.flatMap((entry) => (entry.batch_id ? [entry.batch_id] : [])));
      await Promise.all([
        ...[...batches].map((batchId) => cancelBatch(batchId)),
        ...active.filter((entry) => !entry.batch_id).map((entry) => cancelJob(entry.job_id)),
      ]);
    });

  const active = isActive(job);
  const anyActive = activeJobs !== "";
  const profilesReady = Boolean(machine && process && filament) && loading.length === 0 && !busy;
  const ready = items.length > 0 && outside.length === 0 && profilesReady && !active;
  const readyAll = plates.some((entry) => entry.items.length > 0) && !outsideAnywhere && profilesReady && !anyActive;
  const previewable = hasPreview(job);

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
        <div className="plates">
          <nav className="tabs" role="tablist" aria-label="Plates">
            {plates.map((entry, index) => (
              <button
                key={entry.id}
                type="button"
                role="tab"
                aria-selected={entry.id === plate.id}
                data-testid="plate-tab"
                onClick={() => showPlate(entry.id)}
              >
                {plateLabel(entry, index)}
                {entry.items.length > 0 && <span className="count"> ({entry.items.length})</span>}
              </button>
            ))}
          </nav>
          <button
            type="button"
            className="secondary"
            data-testid="add-plate"
            aria-label="Add a plate"
            onClick={addPlate}
          >
            +
          </button>
        </div>
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
            <PreviewView key={job!.job_id} jobId={job!.job_id} />
          )}
        </section>

        <aside className="sidebar">
          <section>
            <h2 data-testid="plate-heading">{plateLabel(plate, currentIndex)}</h2>
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
              <button
                type="button"
                data-testid="arrange"
                disabled={items.length === 0}
                onClick={() => editItems((current) => arrange(current, bed))}
              >
                Arrange
              </button>
              <button
                type="button"
                className="secondary"
                data-testid="remove-plate"
                disabled={plates.length < 2}
                onClick={removePlate}
              >
                Remove plate
              </button>
            </div>
            {selected && (
              <ObjectPanel
                item={selected}
                plates={plates}
                plateId={plate.id}
                onChange={(change) => update(selected.id, change)}
                onMove={(targetId) => moveToPlate(selected.id, targetId)}
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
            {plates.length > 1 && (
              <button type="button" data-testid="slice-all" disabled={!readyAll} onClick={sliceAll}>
                Slice all plates
              </button>
            )}
            <button
              type="button"
              data-testid="cancel"
              disabled={!active || busy}
              onClick={() => job && run(async () => setPlateJob(plate.id, await cancelJob(job.job_id)))}
            >
              Cancel
            </button>
            {plates.length > 1 && (
              <button
                type="button"
                className="secondary"
                data-testid="cancel-all"
                disabled={!anyActive || busy}
                onClick={cancelAll}
              >
                Cancel all
              </button>
            )}
            <button
              type="button"
              data-testid="retry"
              disabled={!job || active || busy}
              onClick={() => job && run(async () => setPlateJob(plate.id, await retryJob(job.job_id)))}
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

          {plates.length > 1 && <PlateSummary plates={plates} onShow={showPlate} />}

          {job && <JobPanel job={job} />}
        </aside>
      </main>
    </div>
  );
}

/** Every plate's latest slice, so a project sliced at once can be followed and downloaded. */
function PlateSummary(props: { plates: Plate[]; onShow: (plateId: string) => void }) {
  return (
    <section className="plate-summary">
      <h2>Plates</h2>
      <ul data-testid="plate-summary">
        {props.plates.map((entry, index) => (
          <li key={entry.id} data-testid="plate-status" data-state={entry.job?.state ?? "unsliced"}>
            <button type="button" className="link" onClick={() => props.onShow(entry.id)}>
              {plateLabel(entry, index)}
            </button>{" "}
            {entry.items.length === 0 ? "empty" : (entry.job?.state ?? "not sliced")}
            {entry.job?.state === "succeeded" && (
              <>
                {" "}
                <a data-testid="plate-download" href={artifactUrl(entry.job.job_id, "gcode")} download>
                  G-code
                </a>
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
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
  plates: Plate[];
  plateId: string;
  onChange: (change: Partial<PlateItem>) => void;
  onMove: (plateId: string) => void;
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
      {props.plates.length > 1 && (
        <div className="field wide">
          <label htmlFor="object-plate">Plate</label>
          <select
            id="object-plate"
            data-testid="object-plate"
            value={props.plateId}
            onChange={(event) => props.onMove(event.target.value)}
          >
            {props.plates.map((entry, index) => (
              <option key={entry.id} value={entry.id}>
                {plateLabel(entry, index)}
              </option>
            ))}
          </select>
        </div>
      )}
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
        {job.request.plate && <span data-testid="job-plate">Plate {job.request.plate}: </span>}
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
