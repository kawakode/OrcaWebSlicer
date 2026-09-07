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
}

export interface ProfileCatalog {
  machine: ProfileEntry[];
  process: ProfileEntry[];
  filament: ProfileEntry[];
}

export interface Upload {
  upload_id: string;
  filename: string;
  format: string;
  size_bytes: number;
  sha256: string;
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
  name: "gcode" | "result";
  media_type: string;
  size_bytes?: number;
  sha256?: string;
}

export interface Job {
  job_id: string;
  correlation_id: string;
  state: JobState;
  progress: JobProgress;
  warnings: JobWarning[];
  error: JobError | null;
  artifacts: JobArtifact[];
  retry_of: string | null;
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
