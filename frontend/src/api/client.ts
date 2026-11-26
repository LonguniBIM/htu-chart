import type {
  BacktestJobDetail,
  BacktestJobListResponse,
  BacktestJobStatus,
  BacktestJobSummary,
  BacktestStrategiesResponse,
  BacktestSubmitRequest,
  BarsResponse,
  ContractsResponse,
  DrawingBatchOperation,
  DrawingCommandRequest,
  DrawingCommandResponse,
  DrawingScopeResponse,
  LayoutPayload,
  LayoutRecord,
  RootSymbol,
  StatusResponse,
  Timeframe,
  UndoRedoRequest,
} from "./types";

let inMemoryCsrfToken: string | null = null;
let authGeneration = 0;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly code?: string;
  readonly detailObject?: Record<string, unknown>;

  constructor(
    status: number,
    detail: string,
    options?: {
      code?: string;
      detailObject?: Record<string, unknown>;
    },
  ) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    if (options?.code !== undefined) {
      this.code = options.code;
    }
    if (options?.detailObject !== undefined) {
      this.detailObject = options.detailObject;
    }
  }
}

export function getCsrfToken(): string | null {
  return inMemoryCsrfToken;
}

export function clearCsrfToken(): void {
  authGeneration += 1;
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
    let code: string | undefined;
    let detailObject: Record<string, unknown> | undefined;
    try {
      const errJson = (await response.json()) as { detail?: unknown };
      if (typeof errJson.detail === "string") {
        detail = errJson.detail;
      } else if (
        errJson.detail !== null &&
        typeof errJson.detail === "object" &&
        !Array.isArray(errJson.detail)
      ) {
        const obj = errJson.detail as Record<string, unknown>;
        detailObject = obj;
        if (typeof obj.code === "string") {
          code = obj.code;
        }
        if (typeof obj.message === "string") {
          detail = obj.message;
        } else {
          detail = JSON.stringify(obj);
        }
      } else if (errJson.detail !== undefined) {
        detail = JSON.stringify(errJson.detail);
      }
    } catch {
      // Ignore non-JSON error body
    }
    throw new ApiError(response.status, detail, {
      ...(code !== undefined ? { code } : {}),
      ...(detailObject !== undefined ? { detailObject } : {}),
    });
  }

  return (await response.json()) as T;
}

export async function fetchSession(
  signal?: AbortSignal,
): Promise<{ authenticated: boolean; csrf_token: string }> {
  const gen = ++authGeneration;
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const result = await apiRequest<{ authenticated: boolean; csrf_token: string }>(
    "/api/auth/session",
    opts,
  );
  if (gen === authGeneration) {
    inMemoryCsrfToken = result.csrf_token;
  }
  return result;
}

export async function login(
  password: string,
  signal?: AbortSignal,
): Promise<{ authenticated: boolean; csrf_token: string }> {
  const gen = ++authGeneration;
  const opts: RequestOptions = { method: "POST", body: { password } };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  const result = await apiRequest<{ authenticated: boolean; csrf_token: string }>(
    "/api/auth/login",
    opts,
  );
  if (gen === authGeneration) {
    inMemoryCsrfToken = result.csrf_token;
  }
  return result;
}

export async function logout(signal?: AbortSignal): Promise<{ authenticated: boolean }> {
  const gen = ++authGeneration;
  const opts: RequestOptions = { method: "POST" };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  try {
    return await apiRequest<{ authenticated: boolean }>("/api/auth/logout", opts);
  } finally {
    if (gen === authGeneration) {
      inMemoryCsrfToken = null;
    }
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

function appendScopeQueryParams(
  query: URLSearchParams,
  params?: { contract_id?: string | null; pane_id?: string | null },
): void {
  const contractId = params?.contract_id?.trim();
  if (contractId) {
    query.set("contract_id", contractId);
  }
  const paneId = params?.pane_id?.trim();
  if (paneId) {
    query.set("pane_id", paneId);
  }
}

function sanitizeBatchOperation(op: DrawingBatchOperation): Record<string, unknown> {
  const base: Record<string, unknown> = { action: op.action };
  if (op.created_by !== undefined) {
    base.created_by = op.created_by;
  }
  if (op.action === "create") {
    base.drawing = op.drawing;
  } else if (op.action === "update") {
    base.drawing_id = op.drawing_id;
    base.drawing = op.drawing;
  } else {
    base.drawing_id = op.drawing_id;
  }
  return base;
}

function sanitizeDrawingCommand(command: DrawingCommandRequest): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    action: command.action,
    expected_revision: command.expected_revision,
  };
  if (command.idempotency_key !== undefined) {
    payload.idempotency_key = command.idempotency_key;
  }
  if (command.contract_id !== undefined && command.contract_id !== null && command.contract_id.trim()) {
    payload.contract_id = command.contract_id.trim();
  }
  if (command.pane_id !== undefined && command.pane_id !== null && command.pane_id.trim()) {
    payload.pane_id = command.pane_id.trim();
  }
  if (command.created_by !== undefined) {
    payload.created_by = command.created_by;
  }

  switch (command.action) {
    case "create":
      payload.drawing = command.drawing;
      break;
    case "update":
      payload.drawing_id = command.drawing_id;
      payload.drawing = command.drawing;
      break;
    case "delete":
      payload.drawing_id = command.drawing_id;
      break;
    case "clear":
      break;
    case "batch":
      payload.operations = command.operations.map(sanitizeBatchOperation);
      break;
  }
  return payload;
}

export async function fetchDrawings(
  symbol: RootSymbol,
  params?: { contract_id?: string | null; pane_id?: string | null },
  signal?: AbortSignal,
): Promise<DrawingScopeResponse> {
  const query = new URLSearchParams();
  appendScopeQueryParams(query, params);
  const qs = query.toString();
  const url = qs
    ? `/api/drawings/${encodeURIComponent(symbol)}?${qs}`
    : `/api/drawings/${encodeURIComponent(symbol)}`;
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<DrawingScopeResponse>(url, opts);
}

export async function postDrawingCommand(
  symbol: RootSymbol,
  command: DrawingCommandRequest,
  signal?: AbortSignal,
): Promise<DrawingCommandResponse> {
  const opts: RequestOptions = {
    method: "POST",
    body: sanitizeDrawingCommand(command),
  };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<DrawingCommandResponse>(
    `/api/drawings/${encodeURIComponent(symbol)}`,
    opts,
  );
}

function sanitizeUndoRedoRequest(request: UndoRedoRequest): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    expected_revision: request.expected_revision,
  };
  if (request.idempotency_key !== undefined) {
    payload.idempotency_key = request.idempotency_key;
  }
  if (request.contract_id !== undefined && request.contract_id !== null && request.contract_id.trim()) {
    payload.contract_id = request.contract_id.trim();
  }
  if (request.pane_id !== undefined && request.pane_id !== null && request.pane_id.trim()) {
    payload.pane_id = request.pane_id.trim();
  }
  if (request.actor !== undefined) {
    payload.actor = request.actor;
  }
  return payload;
}

export async function undoDrawingCommand(
  symbol: RootSymbol,
  request: UndoRedoRequest,
  signal?: AbortSignal,
): Promise<DrawingCommandResponse> {
  const opts: RequestOptions = {
    method: "POST",
    body: sanitizeUndoRedoRequest(request),
  };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<DrawingCommandResponse>(
    `/api/drawings/${encodeURIComponent(symbol)}/undo`,
    opts,
  );
}

export async function redoDrawingCommand(
  symbol: RootSymbol,
  request: UndoRedoRequest,
  signal?: AbortSignal,
): Promise<DrawingCommandResponse> {
  const opts: RequestOptions = {
    method: "POST",
    body: sanitizeUndoRedoRequest(request),
  };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<DrawingCommandResponse>(
    `/api/drawings/${encodeURIComponent(symbol)}/redo`,
    opts,
  );
}

export async function clearDrawings(
  symbol: RootSymbol,
  params: {
    expected_revision: number;
    idempotency_key?: string;
    contract_id?: string | null;
    pane_id?: string | null;
  },
  signal?: AbortSignal,
): Promise<DrawingCommandResponse> {
  const query = new URLSearchParams({
    expected_revision: String(params.expected_revision),
  });
  if (params.idempotency_key !== undefined) {
    query.set("idempotency_key", params.idempotency_key);
  }
  appendScopeQueryParams(query, params);
  const opts: RequestOptions = { method: "DELETE" };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<DrawingCommandResponse>(
    `/api/drawings/${encodeURIComponent(symbol)}?${query.toString()}`,
    opts,
  );
}

export async function fetchBacktestStrategies(
  signal?: AbortSignal,
): Promise<BacktestStrategiesResponse> {
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BacktestStrategiesResponse>("/api/backtest/strategies", opts);
}

export async function listBacktestJobs(
  params?: { status?: BacktestJobStatus; limit?: number },
  signal?: AbortSignal,
): Promise<BacktestJobListResponse> {
  const query = new URLSearchParams();
  if (params?.status !== undefined) {
    query.set("status", params.status);
  }
  if (params?.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  const qs = query.toString();
  const url = qs ? `/api/backtest?${qs}` : "/api/backtest";
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BacktestJobListResponse>(url, opts);
}

export async function submitBacktestJob(
  payload: BacktestSubmitRequest,
  signal?: AbortSignal,
): Promise<BacktestJobSummary> {
  const opts: RequestOptions = {
    method: "POST",
    body: payload,
  };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BacktestJobSummary>("/api/backtest", opts);
}

export async function getBacktestJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<BacktestJobDetail> {
  const opts: RequestOptions = {};
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BacktestJobDetail>(
    `/api/backtest/${encodeURIComponent(jobId)}`,
    opts,
  );
}

export async function cancelBacktestJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<BacktestJobSummary> {
  const opts: RequestOptions = {
    method: "DELETE",
  };
  if (signal !== undefined) {
    opts.signal = signal;
  }
  return apiRequest<BacktestJobSummary>(
    `/api/backtest/${encodeURIComponent(jobId)}`,
    opts,
  );
}
