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
export type ModelList = S["ModelList"];
export type ModelInfo = S["ModelInfo"];
export type ModelForecast = S["ModelForecast"];
export type ForecastRequest = S["ForecastRequest"];
export type ForecastResponse = S["ForecastResponse"];
export type ForecastEvaluation = S["ForecastEvaluation"];
export type BenchmarkRequest = S["BenchmarkRequest"];
export type BenchmarkStatus = S["BenchmarkStatus"];
export type BenchmarkSummary = S["BenchmarkSummary"];
export type BenchmarkRun = S["BenchmarkRun"];
export type BenchmarkList = S["BenchmarkList"];
export type BenchmarkListItem = S["BenchmarkListItem"];
export type BenchmarkSamples = S["BenchmarkSamples"];
export type BenchmarkSample = S["BenchmarkSample"];

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

async function request<T>(path: string, query: Query, init: RequestInit): Promise<T> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(query)) if (v !== undefined) url.searchParams.set(k, String(v));
  let res: Response;
  try {
    res = await fetch(url, init);
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

const get = <T>(path: string, query: Query = {}, signal?: AbortSignal) => request<T>(path, query, { signal });
const post = <T>(path: string, body: unknown, signal?: AbortSignal) =>
  request<T>(path, {}, { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" }, signal });

export const api = {
  health: (signal?: AbortSignal) => get<Health>("/health", {}, signal),
  datasets: (signal?: AbortSignal) => get<DatasetList>("/datasets", {}, signal),
  vessels: (
    q: { dataset_id: string; from?: string; to?: string; bbox?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => get<VesselPage>("/vessels", q, signal),
  track: (vesselId: string, q: { dataset_id: string; from?: string; to?: string }, signal?: AbortSignal) =>
    get<Track>(`/vessels/${encodeURIComponent(vesselId)}/track`, q, signal),
  models: (datasetId: string, signal?: AbortSignal) => get<ModelList>("/models", { dataset_id: datasetId }, signal),
  forecast: (req: ForecastRequest, signal?: AbortSignal) => post<ForecastResponse>("/forecasts", req, signal),
  /** Withheld truth for the forecast the same request produces (403 holdout_test_period on test). */
  benchmarks: (signal?: AbortSignal) => get<BenchmarkList>("/benchmarks", {}, signal),
  /** The full immutable run record (checksums verified by the API). */
  benchmark: (runId: string, signal?: AbortSignal) => get<BenchmarkStatus>(`/benchmarks/${encodeURIComponent(runId)}`, {}, signal),
  /** Failure gallery: one cell's paired samples, worst first (403 on a held-out test run). */
  samples: (
    runId: string,
    q: { stratum: string; horizon_minutes: number; model_id: string; sort?: "error" | "loss"; limit?: number },
    signal?: AbortSignal,
  ) => get<BenchmarkSamples>(`/benchmarks/${encodeURIComponent(runId)}/samples`, q, signal),
  evaluate: (req: ForecastRequest, signal?: AbortSignal) => post<ForecastEvaluation>("/forecasts/evaluation", req, signal),
};
