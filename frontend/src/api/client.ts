import type {
  BarsResponse,
  ContractsResponse,
  LayoutPayload,
  LayoutRecord,
  RootSymbol,
  StatusResponse,
  Timeframe,
} from "./types";

let inMemoryCsrfToken: string | null = null;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function getCsrfToken(): string | null {
  return inMemoryCsrfToken;
}

export function clearCsrfToken(): void {
  inMemoryCsrfToken = null;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

async function apiRequest<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET" && inMemoryCsrfToken !== null) {
    headers["X-CSRF-Token"] = inMemoryCsrfToken;
  }

  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers,
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }
  if (options.signal !== undefined) {
    init.signal = options.signal;
  }

  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = response.statusText || "Request failed";
    try {
      const errJson = (await response.json()) as { detail?: unknown };
      if (typeof errJson.detail === "string") {
        detail = errJson.detail;
      } else if (errJson.detail !== undefined) {
        detail = JSON.stringify(errJson.detail);
      }
    } catch {
      // Ignore non-JSON error body
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

export async function fetchSession(
  signal?: AbortSignal,
): Promise<{ authenticated: boolean; csrf_token: string }> {
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const result = await apiRequest<{ authenticated: boolean; csrf_token: string }>(
    "/api/auth/session",
    opts,
  );
  inMemoryCsrfToken = result.csrf_token;
  return result;
}

export async function login(
  password: string,
  signal?: AbortSignal,
): Promise<{ authenticated: boolean; csrf_token: string }> {
  const opts: RequestOptions = { method: "POST", body: { password } };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const result = await apiRequest<{ authenticated: boolean; csrf_token: string }>(
    "/api/auth/login",
    opts,
  );
  inMemoryCsrfToken = result.csrf_token;
  return result;
}

export async function logout(signal?: AbortSignal): Promise<{ authenticated: boolean }> {
  const opts: RequestOptions = { method: "POST" };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  try {
    return await apiRequest<{ authenticated: boolean }>("/api/auth/logout", opts);
  } finally {
    inMemoryCsrfToken = null;
  }
}

export async function fetchStatus(signal?: AbortSignal): Promise<StatusResponse> {
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<StatusResponse>("/api/status", opts);
}

export async function fetchContracts(
  symbols: readonly RootSymbol[] = ["MES", "MNQ", "MYM", "MGC"],
  live?: boolean,
  signal?: AbortSignal,
): Promise<ContractsResponse> {
  const query = new URLSearchParams({ symbols: symbols.join(",") });
  if (live !== undefined) {
    query.set("live", String(live));
  }
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<ContractsResponse>(`/api/contracts?${query.toString()}`, opts);
}

export interface FetchBarsParams {
  contract_id: string;
  timeframe: Timeframe;
  live?: boolean;
  days?: number;
  limit?: number;
  include_partial?: boolean;
  root_symbol?: RootSymbol;
}

export async function fetchBars(
  params: FetchBarsParams,
  signal?: AbortSignal,
): Promise<BarsResponse> {
  const query = new URLSearchParams({
    contract_id: params.contract_id,
    timeframe: params.timeframe,
  });
  if (params.live !== undefined) {
    query.set("live", String(params.live));
  }
  if (params.days !== undefined) {
    query.set("days", String(params.days));
  }
  if (params.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  if (params.include_partial !== undefined) {
    query.set("include_partial", String(params.include_partial));
  }
  if (params.root_symbol !== undefined) {
    query.set("root_symbol", params.root_symbol);
  }
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BarsResponse>(`/api/bars?${query.toString()}`, opts);
}

export async function listLayouts(signal?: AbortSignal): Promise<LayoutRecord[]> {
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const data = await apiRequest<{ layouts: LayoutRecord[] }>("/api/layouts", opts);
  return data.layouts;
}

export async function createLayout(
  payload: LayoutPayload,
  signal?: AbortSignal,
): Promise<LayoutRecord> {
  const opts: RequestOptions = { method: "POST", body: payload };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const data = await apiRequest<{ layout: LayoutRecord }>("/api/layouts", opts);
  return data.layout;
}

export async function updateLayout(
  layoutId: string,
  payload: LayoutPayload,
  signal?: AbortSignal,
): Promise<LayoutRecord> {
  const opts: RequestOptions = { method: "PUT", body: payload };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const data = await apiRequest<{ layout: LayoutRecord }>(
    `/api/layouts/${encodeURIComponent(layoutId)}`,
    opts,
  );
  return data.layout;
}

export async function deleteLayout(
  layoutId: string,
  signal?: AbortSignal,
): Promise<{ deleted: boolean; id: string }> {
  const opts: RequestOptions = { method: "DELETE" };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<{ deleted: boolean; id: string }>(
    `/api/layouts/${encodeURIComponent(layoutId)}`,
    opts,
  );
}
