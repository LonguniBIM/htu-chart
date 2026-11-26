import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearCsrfToken, login } from "../api/client";
import type { DrawingRecord, WsDrawingsUpdateMessage } from "../api/types";
import { buildDrawingScopeKey, useDrawingStore } from "./drawingStore";

function makeDrawing(id: string, price = 5000): DrawingRecord {
  return {
    id,
    revision: 1,
    type: "hline",
    symbol: "MES",
    contract_id: "CON.F.US.MES.H26",
    pane_id: "pane-1",
    created_by: "user",
    source_bar_refs: [],
    coordinates: { anchors: [{ role: "level", time: 1700000000, price }] },
    style: {},
    metadata: {},
    created_at_utc: 1700000000,
    updated_at_utc: 1700000000,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useDrawingStore and api/client drawing wire contract (Phase 4 Part 2)", () => {
  const scope = { symbol: "MES" as const, contractId: "CON.F.US.MES.H26", paneId: "pane-1" };
  const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");

  beforeEach(() => {
    useDrawingStore.getState().setAuthErrorHandler(null);
    useDrawingStore.getState().reset();
    clearCsrfToken();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useDrawingStore.getState().setAuthErrorHandler(null);
    useDrawingStore.getState().reset();
    clearCsrfToken();
  });

  it("serializes mutations per scope, omits forbidden fields, and sends CSRF + actor on undo/redo", async () => {
    /**
     * Contract: Sequential mutations on the same scope serialize behind the per-scope queue so
     * expected_revision chains 0 -> 1 -> 2, POST bodies omit fields forbidden by Pydantic
     * ConfigDict(extra="forbid"), undo/redo send `actor` (not `created_by`), and X-CSRF-Token is attached.
     * Defect caught: Parallel drag/create racing on stale expected_revision or sending forbidden fields causing 422.
     */
    const recordedRequests: Array<{ url: string; method: string; headers: Record<string, string>; body: unknown }> = [];
    let releaseFirstCreate!: () => void;
    const firstCreateGate = new Promise<void>((resolve) => {
      releaseFirstCreate = resolve;
    });

    const d1 = makeDrawing("draw-1", 5000);
    const d1Updated = makeDrawing("draw-1", 5010);

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      recordedRequests.push({ url, method, headers, body });

      if (url === "/api/auth/login") {
        return jsonResponse(200, { authenticated: true, csrf_token: "csrf-drawing-secret" });
      }
      if (url === "/api/drawings/MES" && method === "POST" && (body as { action: string }).action === "create") {
        await firstCreateGate;
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 1,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["draw-1"],
          drawings: [d1],
        });
      }
      if (url === "/api/drawings/MES" && method === "POST" && (body as { action: string }).action === "update") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 2,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["draw-1"],
          drawings: [d1Updated],
        });
      }
      if (url === "/api/drawings/MES/undo" && method === "POST") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 3,
          can_undo: true,
          can_redo: true,
          deduplicated: false,
          affected_ids: ["draw-1"],
          drawings: [d1],
        });
      }
      if (url === "/api/drawings/MES/redo" && method === "POST") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 4,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["draw-1"],
          drawings: [d1Updated],
        });
      }
      if (url.startsWith("/api/drawings/MES?") && method === "DELETE") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 5,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["draw-1"],
          drawings: [],
        });
      }
      return jsonResponse(500, { detail: `Unexpected ${method} ${url}` });
    });
    vi.stubGlobal("fetch", fetchMock);

    await login("pw");

    // Enqueue create and update concurrently; update must wait until create finishes and read rev 1
    const pCreate = useDrawingStore.getState().createDrawing(scope, {
      type: "hline",
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
    });
    const pUpdate = useDrawingStore.getState().updateDrawing(scope, "draw-1", {
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5010 }] },
    });

    await Promise.resolve();
    await Promise.resolve();
    // Only login + create have fired so far; update is queued behind create
    expect(recordedRequests).toHaveLength(2);

    releaseFirstCreate();
    await Promise.all([pCreate, pUpdate]);

    await useDrawingStore.getState().undoScope(scope);
    await useDrawingStore.getState().redoScope(scope);
    await useDrawingStore.getState().clearScopeDrawings(scope);

    const createReq = recordedRequests[1]!;
    expect(createReq.headers["X-CSRF-Token"]).toBe("csrf-drawing-secret");
    expect((createReq.body as Record<string, unknown>).expected_revision).toBe(0);
    expect("drawing_id" in (createReq.body as Record<string, unknown>)).toBe(false);
    expect("operations" in (createReq.body as Record<string, unknown>)).toBe(false);

    const updateReq = recordedRequests[2]!;
    expect((updateReq.body as Record<string, unknown>).expected_revision).toBe(1);
    expect("operations" in (updateReq.body as Record<string, unknown>)).toBe(false);

    const undoReq = recordedRequests[3]!;
    expect((undoReq.body as Record<string, unknown>).expected_revision).toBe(2);
    expect((undoReq.body as Record<string, unknown>).actor).toBe("user");
    expect("created_by" in (undoReq.body as Record<string, unknown>)).toBe(false);

    const redoReq = recordedRequests[4]!;
    expect((redoReq.body as Record<string, unknown>).expected_revision).toBe(3);
    expect((redoReq.body as Record<string, unknown>).actor).toBe("user");

    const clearReq = recordedRequests[5]!;
    expect(clearReq.method).toBe("DELETE");
    expect(clearReq.url).toContain("expected_revision=4");
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(5);
  });

  it("auto-resyncs authoritative scope state on 409 revision_conflict and preserves newer revisions on WS/loadScope", async () => {
    /**
     * Contract: On HTTP 409 { code: "revision_conflict" }, drawingStore parses ApiError.code,
     * automatically re-fetches GET /api/drawings/{symbol}, syncs scopeRevision & drawings, and
     * sets a non-fatal error; loadScope and applyWsDrawingsUpdate ignore stale lower revisions.
     * Defect caught: 409 conflict leaving client permanently stuck on stale expected_revision.
     */
    const serverDrawing = makeDrawing("draw-server", 5025);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/api/drawings/MES" && method === "POST") {
        return jsonResponse(409, {
          detail: {
            code: "revision_conflict",
            message: "Drawing scope revision conflict: expected 0, current is 4",
            current_revision: 4,
            expected_revision: 0,
          },
        });
      }
      if (url.startsWith("/api/drawings/MES") && method === "GET") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 4,
          can_undo: true,
          can_redo: false,
          drawings: [serverDrawing],
        });
      }
      return jsonResponse(500, { detail: "unexpected" });
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await useDrawingStore.getState().createDrawing(scope, {
      type: "hline",
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
    });

    expect(res).toBeNull();
    const stateAfter409 = useDrawingStore.getState().scopes[scopeKey]!;
    expect(stateAfter409.scopeRevision).toBe(4);
    expect(stateAfter409.drawings).toEqual([serverDrawing]);
    expect(stateAfter409.error).toContain("Revision conflict");
    expect(stateAfter409.mutating).toBe(false);

    // Stale WS update (rev 3 <= 4) must be ignored
    const staleWs: WsDrawingsUpdateMessage = {
      type: "drawings_update",
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      scope_revision: 3,
      command_type: "create",
      can_undo: false,
      can_redo: false,
      drawings: [],
    };
    useDrawingStore.getState().applyWsDrawingsUpdate(staleWs);
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(4);

    // Newer WS update (rev 5 > 4) must apply
    useDrawingStore.getState().applyWsDrawingsUpdate({
      ...staleWs,
      scope_revision: 5,
      drawings: [],
    });
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(5);
    expect(useDrawingStore.getState().scopes[scopeKey]?.drawings).toEqual([]);

    // Stale loadScope (rev 4 < 5) must preserve newer local rev 5
    await useDrawingStore.getState().loadScope(scope);
    expect(useDrawingStore.getState().scopes[scopeKey]?.scopeRevision).toBe(5);
    expect(useDrawingStore.getState().scopes[scopeKey]?.drawings).toEqual([]);
  });

  it("triggers authErrorHandler on 401/403 and aborts/epoch-guards in-flight requests on reset()", async () => {
    /**
     * Contract: 401/403 errors invoke authErrorHandler when sessionEpoch matches; calling reset()
     * aborts in-flight fetch signals, bumps sessionEpoch, and discards late 401/200 settlements.
     * Defect caught: Stale 401 after Logout/Login invalidating the new authenticated session.
     */
    const authErrors: Array<{ status: number; detail: string }> = [];
    useDrawingStore.getState().setAuthErrorHandler((status, detail) => {
      authErrors.push({ status, detail });
    });

    let rejectPending!: (err: unknown) => void;
    let capturedSignal: AbortSignal | undefined;
    const fetchMock = vi
      .fn()
      .mockImplementationOnce((_url: RequestInfo | URL, init?: RequestInit) => {
        capturedSignal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          rejectPending = reject;
        });
      })
      .mockResolvedValueOnce(jsonResponse(401, { detail: "Session expired" }));
    vi.stubGlobal("fetch", fetchMock);

    const staleLoad = useDrawingStore.getState().loadScope(scope);
    expect(capturedSignal?.aborted).toBe(false);

    useDrawingStore.getState().reset();
    expect(capturedSignal?.aborted).toBe(true);

    // Late rejection from previous epoch must NOT fire authErrorHandler
    rejectPending(new DOMException("Aborted", "AbortError"));
    await staleLoad;
    expect(authErrors).toEqual([]);

    // Active-epoch 401 MUST fire authErrorHandler
    await useDrawingStore.getState().loadScope(scope);
    expect(authErrors).toEqual([{ status: 401, detail: "Session expired" }]);
  });
});
