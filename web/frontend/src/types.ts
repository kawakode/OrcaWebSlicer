/** The API contract, mirrored from `docs/web/api.md`. */

export type JobState = "queued" | "running" | "succeeded" | "failed" | "canceled";

export const TERMINAL_STATES: readonly JobState[] = ["succeeded", "failed", "canceled"];

export interface ProfileEntry {
  profile_id: string;
  kind: "machine" | "process" | "filament";
  name: string;
  vendor: string;
  /** The flattened inheritance chain, root first. */
  inherits_chain: string[];
  printer_model?: string;
  nozzle_diameter?: string;
  default_process?: string;
}

export interface ProfileCatalog {
  machine: ProfileEntry[];
  process: ProfileEntry[];
  filament: ProfileEntry[];
}

/**
 * One setting as `PrintConfigDef` declares it. Every field here is the engine's,
 * so no type, range, enum, or default is ever restated in this app.
 */
export interface SettingDefinition {
  key: string;
  group: string;
  scope: "process" | "filament" | "machine" | "other";
  type: string;
  vector: boolean;
  nullable: boolean;
  mode: string;
  label: string;
  category: string;
  tooltip: string;
  unit: string;
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
  name: "gcode" | "result" | "preview";
  media_type: string;
  size_bytes?: number;
  sha256?: string;
}

/** One layer of the preview index: its height and its range in the blob. */
export interface PreviewLayer {
  index: number;
  z: number;
  offset: number;
  length: number;
  segments: number;
  roles: string[];
  tools: number[];
}

export interface PreviewIndex {
  preview_version: number;
  units: "mm";
  quantum_mm: number;
  segment_count: number;
  tools: number[];
  roles: { id: string; label: string }[];
  bounding_box: { min: [number, number, number]; max: [number, number, number] };
  layers: PreviewLayer[];
}

/** One override paired with the engine's description of the setting. */
export interface JobOverride {
  key: string;
  value: string;
  label?: string;
  unit?: string;
  scope?: string;
}

export interface Job {
  job_id: string;
  correlation_id: string;
  state: JobState;
  progress: JobProgress;
  warnings: JobWarning[];
  error: JobError | null;
  artifacts: JobArtifact[];
  profiles: Partial<Record<"machine" | "process" | "filament", ProfileEntry>>;
  overrides: JobOverride[];
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

/** One object of an inspected scene, as `docs/web/scene-format.md` declares it. */
export interface SceneObject {
  index: number;
  name: string;
  triangle_count: number;
  vertex_count: number;
  /** In the object's own local frame — the frame its vertices are in. */
  bounding_box: { min: [number, number, number]; max: [number, number, number] };
  /** Column-major 4x4 in millimetres, placing that frame on the bed. */
  transform: number[];
  offset: number;
  length: number;
}

export interface SceneIndex {
  scene_version: number;
  units: "mm";
  quantum_mm: number;
  bed: { shape: [number, number][]; printable_height: number };
  data_bytes: number;
  objects: SceneObject[];
}

/** One placed copy: what the plater displays and what a slice request carries. */
export interface ObjectPlacement {
  source_object: number;
  transform: number[];
}
