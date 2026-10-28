import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  artifactUrl,
  cancelJob,
  listProfiles,
  readEffectiveProfile,
  readJob,
  readSettingsCatalog,
  retryJob,
  submitJob,
  uploadModel,
} from "./api";
import {
  ApiError,
  TERMINAL_STATES,
  type Job,
  type ProfileCatalog,
  type ProfileEntry,
  type ProfileValue,
  type SettingsCatalog,
  type Upload,
} from "./types";
import { SettingsForm } from "./settings/SettingsForm";

const POLL_INTERVAL_MS = 400;

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
  const [printers, setPrinters] = useState<ProfileEntry[]>([]);
  const [machine, setMachine] = useState("");
  const [process, setProcess] = useState("");
  const [filament, setFilament] = useState("");
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [settingsCatalog, setSettingsCatalog] = useState<SettingsCatalog | null>(null);
  const [profileValues, setProfileValues] = useState<Record<string, ProfileValue>>({});
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    readSettingsCatalog()
      .then(setSettingsCatalog)
      .catch((error) => setFailure(describe(error)));
  }, []);

  // The selected chain's own values are what a blank override keeps.
  useEffect(() => {
    if (!machine || !process || !filament) return;
    let stale = false;
    readEffectiveProfile(machine, process, filament)
      .then((effective) => !stale && setProfileValues(effective.values))
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

  const slice = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setFailure(null);
    try {
      // One upload per selected file: a retry reuses the same immutable inputs.
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
          filament_profile: filament,
          settings: declared,
        }),
      );
    } catch (error) {
      setFailure(describe(error));
    } finally {
      setBusy(false);
    }
  }, [file, upload, settings, machine, process, filament]);

  const ready = Boolean(file && machine && process && filament) && !busy && !active;
  const succeeded = job?.state === "succeeded";

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
        <input
          type="file"
          accept=".stl,.obj,.3mf"
          data-testid="file-input"
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            setUpload(null);
            setJob(null);
            setFailure(null);
          }}
        />
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
        <ProfileSelect
          label="Filament"
          testId="filament-select"
          value={filament}
          entries={catalog.filament}
          onChange={setFilament}
        />
      </section>

      <section>
        <h2>Overrides</h2>
        {settingsCatalog && (
          <SettingsForm
            catalog={settingsCatalog}
            profileValues={profileValues}
            overrides={settings}
            onChange={(key, value) => setSettings((current) => ({ ...current, [key]: value }))}
          />
        )}
      </section>

      <section className="actions">
        <button type="button" data-testid="slice" disabled={!ready} onClick={slice}>
          Slice
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

      {failure && (
        <p className="failure" data-testid="failure" role="alert">
          <strong>{failure.code}</strong> {failure.message}
        </p>
      )}

      {job && <JobPanel job={job} />}
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
  return (
    <label>
      <span>{props.label}</span>
      <select
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
      <p>
        <code data-testid="job-id">{job.job_id}</code> —{" "}
        <strong data-testid="job-state">{job.state}</strong>
        {job.retry_of && <span data-testid="retry-of"> (retry of {job.retry_of})</span>}
      </p>
      <progress data-testid="job-progress" value={peak.current} max={100} />
      <p data-testid="job-stage">
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
      {Object.keys(job.request.settings).length > 0 && (
        <p data-testid="job-overrides">
          Overrides:{" "}
          {Object.entries(job.request.settings)
            .map(([key, value]) => `${key} = ${value}`)
            .join(", ")}
        </p>
      )}
      {job.timing && (
        <p data-testid="job-timing">Sliced in {job.timing.duration_ms} ms</p>
      )}
    </section>
  );
}
