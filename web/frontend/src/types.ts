/** The API contract, mirrored from `docs/web/api.md`. */

export type JobState = "queued" | "running" | "succeeded" | "failed" | "canceled";

export const TERMINAL_STATES: readonly JobState[] = ["succeeded", "failed", "canceled"];

export interface ProfileEntry {
  profile_id: string;
  kind: "machine" | "process" | "filament";
  name: string;
  vendor: string;
  printer_model?: string;
  nozzle_diameter?: string;
  default_process?: string;
  /** Inherited profiles flattened into this one, root first, ending with it. */
  inherits_chain: string[];
}

export interface ProfileCatalog {
  machine: ProfileEntry[];
  process: ProfileEntry[];
  filament: ProfileEntry[];
}

/** One setting as `orca-slicer-worker --export-settings-catalog` describes it. */
export interface SettingDefinition {
  key: string;
  group: string;
  scope: "machine" | "process" | "filament" | "other";
  type: string;
  vector: boolean;
  label: string;
  tooltip?: string;
  unit?: string;
  min?: number;
  max?: number;
  ratio_over?: string;
  enabled_by?: string;
  enum?: { value: string; label: string }[];
  default?: string;
  missing?: boolean;
}

export interface SettingsCatalog {
  catalog_version: number;
  engine_version: string;
  groups: { id: string; label: string }[];
  settings: SettingDefinition[];
}

/** A profile value as bundled JSON carries it: a string or a per-extruder list. */
export type ProfileValue = string | string[];

export interface EffectiveProfile {
  profiles: Record<"machine" | "process" | "filament", ProfileEntry>;
  values: Record<string, ProfileValue>;
  bed: { printable_area: string[] | string; printable_height: string };
}

export interface Upload {
  upload_id: string;
  filename: string;
  format: string;
  size_bytes: number;
  sha256: string;
  /** The worker job exporting this upload's display geometry. */
  inspection: Job;
}

export interface JobProgress {
  stage: string;
  percent: number;
  message: string;
}

export interface JobWarning {
  code: string;
  message: string;
}

export interface JobError {
  category: string;
  code: string;
  message: string;
}

export interface JobArtifact {
  name: "gcode" | "result" | "preview" | "preview_index" | "mesh" | "inspection";
  media_type: string;
  size_bytes?: number;
  sha256?: string;
}

export interface Job {
  job_id: string;
  operation: "slice" | "inspect";
  correlation_id: string;
  state: JobState;
  progress: JobProgress;
  warnings: JobWarning[];
  error: JobError | null;
  artifacts: JobArtifact[];
  request: { settings: Record<string, string> };
  retry_of: string | null;
  profiles: Partial<Record<"machine" | "process" | "filament", { profile_id: string; inherits_chain: string[] }>>;
  timing: { duration_ms: number; cpu_time_ms: number } | null;
}

/** A failure the API named, kept distinct from a network or parsing failure. */
export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly correlationId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
