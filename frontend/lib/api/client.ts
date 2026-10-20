// Typed client for the read API. Types come from schema.d.ts, generated from the backend's
// OpenAPI schema (npm run gen:api); never edit them by hand.
import type { components } from "@/lib/api/schema";

type S = components["schemas"];
export type Health = S["Health"];
export type DatasetSummary = S["DatasetSummary"];
export type DatasetList = S["DatasetList"];
export type VesselSummary = S["VesselSummary"];
export type VesselPage = S["VesselPage"];
export type Track = S["Track"];
export type ApiErrorBody = S["ErrorBody"];
// Milestone 2 contracts (no endpoints yet)
export type ForecastRequest = S["ForecastRequest"];
export type ForecastResponse = S["ForecastResponse"];
export type ForecastEvaluation = S["ForecastEvaluation"];
export type BenchmarkRun = S["BenchmarkRun"];

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | undefined>;

async function get<T>(path: string, query: Query = {}, signal?: AbortSignal): Promise<T> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(query)) if (v !== undefined) url.searchParams.set(k, String(v));
  let res: Response;
  try {
    res = await fetch(url, { signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "backend_unreachable", `Cannot reach the API at ${API_URL}. Is it running (make dev)?`);
  }
  if (!res.ok) {
    let body: ApiErrorBody | undefined;
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, body?.error.code ?? "http_error", body?.error.message ?? res.statusText);
  }
  return (await res.json()) as T;
}

export const api = {
  health: (signal?: AbortSignal) => get<Health>("/health", {}, signal),
  datasets: (signal?: AbortSignal) => get<DatasetList>("/datasets", {}, signal),
  vessels: (
    q: { dataset_id: string; from?: string; to?: string; bbox?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => get<VesselPage>("/vessels", q, signal),
  track: (vesselId: string, q: { dataset_id: string; from?: string; to?: string }, signal?: AbortSignal) =>
    get<Track>(`/vessels/${encodeURIComponent(vesselId)}/track`, q, signal),
};
