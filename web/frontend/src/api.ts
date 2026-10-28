import type { Inspection } from "./plater/mesh";
import type { PreviewIndex } from "./preview/preview";
import {
  ApiError,
  type EffectiveProfile,
  type Job,
  type JobArtifact,
  type ProfileCatalog,
  type SettingsCatalog,
  type Upload,
} from "./types";

const PREFIX = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await send(path, init)).json() as Promise<T>;
}

async function binary(path: string): Promise<ArrayBuffer> {
  return (await send(path)).arrayBuffer();
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${PREFIX}${path}`, init);
  } catch {
    throw new ApiError("network_unavailable", "The slicing service could not be reached.", 0);
  }
  const correlationId = response.headers.get("X-Correlation-Id") ?? undefined;
  if (!response.ok) {
    // Schema rejections come from the framework and have a different shape to
    // the API's own errors, so both are reduced to one code plus one message.
    const body = await response.json().catch(() => null);
    const named = body?.error;
    throw new ApiError(
      named?.code ?? "invalid_request",
      named?.message ?? describeSchemaFailure(body) ?? response.statusText,
      response.status,
      correlationId,
    );
  }
  return response;
}

function describeSchemaFailure(body: unknown): string | undefined {
  const detail = (body as { detail?: unknown })?.detail;
  if (!Array.isArray(detail) || detail.length === 0) return undefined;
  const first = detail[0] as { loc?: unknown[]; msg?: string };
  const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : undefined;
  return field ? `${String(field)}: ${first.msg ?? "is invalid"}` : first.msg;
}

export function listProfiles(printer?: string): Promise<ProfileCatalog> {
  const query = printer ? `?printer=${encodeURIComponent(printer)}` : "";
  return request<ProfileCatalog>(`/profiles${query}`);
}

export const readSettingsCatalog = (): Promise<SettingsCatalog> =>
  request<SettingsCatalog>("/settings");

export function readEffectiveProfile(
  machine: string,
  process: string,
  filament: string,
): Promise<EffectiveProfile> {
  const query = new URLSearchParams({ machine, process, filament });
  return request<EffectiveProfile>(`/profiles/effective?${query}`);
}

export function uploadModel(file: File): Promise<Upload> {
  const body = new FormData();
  body.append("file", file);
  return request<Upload>("/uploads", { method: "POST", body });
}

export interface PlacedObjectSubmission {
  upload_id: string;
  object: number;
  /** Row-major 4x4 matrices in millimetres. */
  instances: { transform: number[] }[];
}

export interface SliceSubmission {
  upload_id?: string;
  objects?: PlacedObjectSubmission[];
  machine_profile: string;
  process_profile: string;
  filament_profile: string;
  settings: Record<string, string>;
}

export function submitJob(submission: SliceSubmission): Promise<Job> {
  return request<Job>("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(submission),
  });
}

export const readJob = (jobId: string): Promise<Job> => request<Job>(`/jobs/${jobId}`);

export const cancelJob = (jobId: string): Promise<Job> =>
  request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" });

export const retryJob = (jobId: string): Promise<Job> =>
  request<Job>(`/jobs/${jobId}/retry`, { method: "POST" });

export const artifactUrl = (jobId: string, name: JobArtifact["name"]): string =>
  `${PREFIX}/jobs/${jobId}/artifacts/${name}`;

export const readInspection = (jobId: string): Promise<Inspection> =>
  request<Inspection>(`/jobs/${jobId}/artifacts/inspection`);

export const readMesh = (jobId: string): Promise<ArrayBuffer> => binary(`/jobs/${jobId}/artifacts/mesh`);

export const readPreviewIndex = (jobId: string): Promise<PreviewIndex> =>
  request<PreviewIndex>(`/jobs/${jobId}/artifacts/preview_index`);

export const readPreviewPage = (jobId: string, start: number, end: number): Promise<ArrayBuffer> =>
  binary(`/jobs/${jobId}/preview/layers?start=${start}&end=${end}`);
