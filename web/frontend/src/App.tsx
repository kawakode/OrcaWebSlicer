import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  artifactUrl,
  cancelJob,
  listProfiles,
  listSettings,
  readJob,
  retryJob,
  submitJob,
  uploadModel,
} from "./api";
import { filamentCss } from "./filamentColors";
import { LayerPreviewPanel } from "./LayerPreview";
import { Plater } from "./Plater";
import { SettingsForm } from "./SettingsForm";
import {
  ApiError,
  TERMINAL_STATES,
  type Job,
  type ObjectPlacement,
  type ProfileCatalog,
  type ProfileEntry,
  type SettingsCatalog,
  type Upload,
} from "./types";

const POLL_INTERVAL_MS = 400;
/** The API accepts 1-16 filament slots. */
const MAX_FILAMENTS = 16;

const EMPTY_CATALOG: ProfileCatalog = { machine: [], process: [], filament: [] };

function describe(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) return { code: error.code, message: error.message };
  return { code: "unexpected_error", message: String(error) };
}

function firstId(entries: ProfileEntry[], preferred?: string): string {
  const match = preferred ? entries.find((entry) => entry.name === preferred) : undefined;
  return (match ?? entries[0])?.profile_id ?? "";
}

export default function App() {
  const [catalog, setCatalog] = useState<ProfileCatalog>(EMPTY_CATALOG);
  const [settingsCatalog, setSettingsCatalog] = useState<SettingsCatalog | null>(null);
  const [printers, setPrinters] = useState<ProfileEntry[]>([]);
  const [machine, setMachine] = useState("");
  const [process, setProcess] = useState("");
  // One bundled filament profile id per slot, in order. Always at least one.
  const [filaments, setFilaments] = useState<string[]>([""]);
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  // Null means the plater has no scene, which is the pre-plater behaviour: the
  // worker arranges whatever the model held.
  const [placements, setPlacements] = useState<ObjectPlacement[] | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // The printer list is the unnarrowed one; the rest is filtered per printer.
  useEffect(() => {
    listProfiles()
      .then((loaded) => {
        setPrinters(loaded.machine);
        setMachine((current) => current || firstId(loaded.machine));
      })
      .catch((error) => setFailure(describe(error)));
  }, []);

  // The overrides form is generated from the engine's own definitions. A
  // deployment whose worker cannot describe them still slices; it just offers
  // no overrides, so this failure is reported without blocking the screen.
  useEffect(() => {
    listSettings().then(setSettingsCatalog).catch(() => setSettingsCatalog(null));
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
        // A printer change resets every profile choice, filament slots
        // included, back down to one — the same reset the printer's other
        // profiles already get, and predictable rather than trying to guess
        // which of several prior slots the new printer's list can still fill.
        setFilaments([firstId(narrowed.filament)]);
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
        .then(setJob)
        .catch((error) => setFailure(describe(error)));
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [jobId, active]);

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

  // The model is stored as soon as it is chosen, because the plater has to
  // inspect it before anything is sliced. A retry then reuses the same
  // immutable upload.
  const chooseFile = useCallback(async (chosen: File | null) => {
    setFile(chosen);
    setUpload(null);
    setPlacements(null);
    setJob(null);
    setFailure(null);
    if (!chosen) return;
    setBusy(true);
    try {
      setUpload(await uploadModel(chosen));
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const slice = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setFailure(null);
    try {
      const stored = upload ?? (await uploadModel(file));
      setUpload(stored);
      const declared = Object.fromEntries(
        Object.entries(settings).filter(([, value]) => value.trim() !== ""),
      );
      setJob(
        await submitJob({
          upload_id: stored.upload_id,
          machine_profile: machine,
          process_profile: process,
          filament_profiles: filaments,
          settings: declared,
          // Slice exactly what the plater is showing, when it is showing one.
          ...(placements ? { objects: placements } : {}),
        }),
      );
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, [file, upload, settings, machine, process, filaments, placements]);

  const filamentsChosen = filaments.length > 0 && filaments.every(Boolean);
  // Names, not ids, are what the plate's per-object select and swatches show.
  const filamentNames = useMemo(
    () => filaments.map((id) => catalog.filament.find((entry) => entry.profile_id === id)?.name ?? id),
    [filaments, catalog.filament],
  );
  const addFilament = useCallback(() => {
    setFilaments((current) =>
      current.length >= MAX_FILAMENTS ? current : [...current, firstId(catalog.filament)],
    );
  }, [catalog.filament]);
  const removeFilament = useCallback((index: number) => {
    // Bounded at one slot: nothing is left to assign objects to below that.
    setFilaments((current) => (current.length <= 1 ? current : current.filter((_, i) => i !== index)));
  }, []);
  const updateFilament = useCallback((index: number, value: string) => {
    setFilaments((current) => current.map((entry, i) => (i === index ? value : entry)));
  }, []);

  const emptyPlate = placements !== null && placements.length === 0;
  const ready = Boolean(file && machine && process) && filamentsChosen && !busy && !active && !emptyPlate;
  const succeeded = job?.state === "succeeded";

  // Each disabled action button is explained, not just dimmed: the reason
  // covers exactly the button's own disabled condition, so the two can never
  // drift apart, and each is only true while its button is actually disabled.
  const sliceHint = busy
    ? "A request is already in progress."
    : active
      ? "A job is already running."
      : !file
        ? "Choose a model file first."
        : !machine || !process || !filamentsChosen
          ? "Choose a printer, process, and filament first."
          : emptyPlate
            ? "The plate is empty. Add an object back before slicing."
            : undefined;
  const cancelHint = busy
    ? "A request is already in progress."
    : !active
      ? "No job is currently running."
      : undefined;
  const retryHint = busy
    ? "A request is already in progress."
    : !job
      ? "There is no job to retry yet."
      : active
        ? "Wait for the current job to finish before retrying."
        : undefined;

  return (
    <main>
      <header>
        <h1>OrcaWebSlicer</h1>
        <p className="subtitle">
          Upload one model, pick bundled profiles, and slice it in an isolated native worker.
        </p>
      </header>

      <section>
        <h2>Model</h2>
        {/* Wrapped in its own label like every other control here: a bare file
            input has no accessible name at all, which the axe pass flags. */}
        <label>
          <span>Model file</span>
          <input
            type="file"
            accept=".stl,.obj,.3mf"
            data-testid="file-input"
            onChange={(event) => void chooseFile(event.target.files?.[0] ?? null)}
          />
        </label>
        {file && (
          <p data-testid="selected-file">
            {file.name} — {file.size.toLocaleString()} bytes
          </p>
        )}
      </section>

      <section>
        <h2>Profiles</h2>
        <ProfileSelect
          label="Printer"
          testId="printer-select"
          value={machine}
          entries={printers}
          onChange={setMachine}
        />
        <ProfileSelect
          label="Process"
          testId="process-select"
          value={process}
          entries={catalog.process}
          onChange={setProcess}
        />
        <div className="filament-slots" data-testid="filament-slots">
          {filaments.map((value, index) => (
            <div className="filament-slot" key={index}>
              <FilamentSlotSelect
                index={index}
                total={filaments.length}
                value={value}
                entries={catalog.filament}
                onChange={(next) => updateFilament(index, next)}
              />
              {filaments.length > 1 && (
                <button
                  type="button"
                  data-testid={`filament-remove-${index}`}
                  onClick={() => removeFilament(index)}
                >
                  Remove
                </button>
              )}
            </div>
          ))}
          <button
            type="button"
            data-testid="filament-add"
            disabled={filaments.length >= MAX_FILAMENTS || catalog.filament.length === 0}
            onClick={addFilament}
          >
            Add filament
          </button>
        </div>
      </section>

      {upload && machine && process && filamentsChosen && (
        <section>
          <h2>Plate</h2>
          <Plater
            uploadId={upload.upload_id}
            profiles={{
              machine_profile: machine,
              process_profile: process,
              // Scene inspection names one filament regardless of how many
              // slots the slice request will carry; the first slot answers.
              filament_profile: filaments[0],
            }}
            disabled={active}
            filamentSlots={filamentNames}
            onPlacements={setPlacements}
          />
        </section>
      )}

      <section>
        <h2>Overrides</h2>
        {settingsCatalog ? (
          <SettingsForm
            catalog={settingsCatalog}
            values={settings}
            disabled={active}
            onChange={(key, value) => setSettings((current) => ({ ...current, [key]: value }))}
          />
        ) : (
          <p data-testid="settings-unavailable" role="status">
            This deployment&rsquo;s slicing engine did not describe its settings, so the selected
            profiles are used unchanged.
          </p>
        )}
      </section>

      <section className="actions">
        <button
          type="button"
          data-testid="slice"
          disabled={!ready}
          aria-describedby={sliceHint ? "slice-hint" : undefined}
          onClick={slice}
        >
          Slice
        </button>
        <button
          type="button"
          data-testid="cancel"
          disabled={!active || busy}
          aria-describedby={cancelHint ? "cancel-hint" : undefined}
          onClick={() => job && run(() => cancelJob(job.job_id))}
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="retry"
          disabled={!job || active || busy}
          aria-describedby={retryHint ? "retry-hint" : undefined}
          onClick={() => job && run(() => retryJob(job.job_id))}
        >
          Retry
        </button>
        {succeeded && (
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
      {sliceHint && (
        <p className="hint" id="slice-hint">
          {sliceHint}
        </p>
      )}
      {cancelHint && (
        <p className="hint" id="cancel-hint">
          {cancelHint}
        </p>
      )}
      {retryHint && (
        <p className="hint" id="retry-hint">
          {retryHint}
        </p>
      )}

      {failure && (
        <p className="failure" data-testid="failure" role="alert">
          <strong>{failure.code}</strong> {failure.message}
        </p>
      )}

      {job && <JobPanel job={job} />}

      {job?.artifacts.some((artifact) => artifact.name === "preview") && (
        <section>
          <h2>Layer preview</h2>
          <LayerPreviewPanel jobId={job.job_id} />
        </section>
      )}
    </main>
  );
}

function ProfileSelect(props: {
  label: string;
  testId: string;
  value: string;
  entries: ProfileEntry[];
  onChange: (value: string) => void;
}) {
  const empty = props.entries.length === 0;
  const hintId = empty ? `${props.testId}-hint` : undefined;
  return (
    <label>
      <span>{props.label}</span>
      <select
        data-testid={props.testId}
        value={props.value}
        disabled={empty}
        aria-describedby={hintId}
        onChange={(event) => props.onChange(event.target.value)}
      >
        {props.entries.map((entry) => (
          <option key={entry.profile_id} value={entry.profile_id}>
            {entry.name}
          </option>
        ))}
      </select>
      {hintId && (
        <span className="hint" id={hintId}>
          No {props.label.toLowerCase()} profiles are available yet.
        </span>
      )}
    </label>
  );
}

/**
 * One filament slot's own select. With a single slot this renders exactly
 * like the old plain "Filament" select, test id included — a user who never
 * adds a slot sees no difference. A second slot onward is numbered and
 * swatched in the same color the plate paints that slot's objects, so the
 * two stay legible together.
 */
function FilamentSlotSelect(props: {
  index: number;
  total: number;
  value: string;
  entries: ProfileEntry[];
  onChange: (value: string) => void;
}) {
  const { index, total, value, entries, onChange } = props;
  const label = total > 1 ? `Filament ${index + 1}` : "Filament";
  const testId = index === 0 ? "filament-select" : `filament-select-${index}`;
  const empty = entries.length === 0;
  const hintId = empty ? `${testId}-hint` : undefined;
  return (
    <label>
      <span>
        {total > 1 && (
          <span
            className="swatch filament-swatch"
            style={{ backgroundColor: filamentCss(index) }}
            aria-hidden="true"
          />
        )}
        {label}
      </span>
      <select
        data-testid={testId}
        value={value}
        disabled={empty}
        aria-describedby={hintId}
        onChange={(event) => onChange(event.target.value)}
      >
        {entries.map((entry) => (
          <option key={entry.profile_id} value={entry.profile_id}>
            {entry.name}
          </option>
        ))}
      </select>
      {hintId && (
        <span className="hint" id={hintId}>
          No filament profiles are available yet.
        </span>
      )}
    </label>
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
      {/* role="status" (implicit polite, atomic live region) is what lets a
          screen reader hear "succeeded" — or "failed" or "canceled" — without
          the user having to go looking for it once the job settles. */}
      <p role="status">
        <code data-testid="job-id">{job.job_id}</code> —{" "}
        <strong data-testid="job-state">{job.state}</strong>
        {job.retry_of && <span data-testid="retry-of"> (retry of {job.retry_of})</span>}
      </p>
      <progress data-testid="job-progress" value={peak.current} max={100} aria-label="Job progress" />
      <p data-testid="job-stage" role="status">
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
      {job.timing && (
        <p data-testid="job-timing">Sliced in {job.timing.duration_ms} ms</p>
      )}
      <JobReport job={job} />
    </section>
  );
}

/** What was actually sliced: the flattened profile chain and what displaced it. */
function JobReport({ job }: { job: Job }) {
  const kinds = ["machine", "process"] as const;
  const filaments = job.profiles.filaments ?? [];
  return (
    <details data-testid="job-report">
      <summary>Effective configuration</summary>
      <dl>
        {kinds.map((kind) => {
          const profile = job.profiles[kind];
          if (!profile) return null;
          return (
            <div key={kind}>
              <dt>{kind}</dt>
              <dd data-testid={`job-profile-${kind}`}>
                {profile.name}
                {profile.inherits_chain.length > 1 && (
                  <span className="chain"> — {profile.inherits_chain.join(" → ")}</span>
                )}
              </dd>
            </div>
          );
        })}
        {filaments.map((profile, index) => (
          <div key={`filament-${index}`}>
            <dt>{filaments.length > 1 ? `filament ${index + 1}` : "filament"}</dt>
            <dd data-testid={`job-profile-filament-${index}`}>
              {profile.name}
              {profile.inherits_chain.length > 1 && (
                <span className="chain"> — {profile.inherits_chain.join(" → ")}</span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      {job.overrides.length > 0 ? (
        <ul data-testid="job-overrides">
          {job.overrides.map((override) => (
            <li key={override.key}>
              {override.label || override.key}: {override.value}
              {override.unit} {override.scope && <em>({override.scope})</em>}
            </li>
          ))}
        </ul>
      ) : (
        <p data-testid="job-overrides-none">No settings were overridden.</p>
      )}
    </details>
  );
}
