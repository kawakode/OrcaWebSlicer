import { ApiError, type Job, type ProfileCatalog, type Upload } from "./types";

const PREFIX = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  return (await response.json()) as T;
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

export function uploadModel(file: File): Promise<Upload> {
  const body = new FormData();
  body.append("file", file);
  return request<Upload>("/uploads", { method: "POST", body });
}

export interface SliceSubmission {
  upload_id: string;
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

export const artifactUrl = (jobId: string, name: "gcode" | "result"): string =>
  `${PREFIX}/jobs/${jobId}/artifacts/${name}`;
