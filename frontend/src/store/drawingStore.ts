import { create } from "zustand";
import {
  ApiError,
  clearDrawings,
  fetchDrawings,
  postDrawingCommand,
  redoDrawingCommand,
  undoDrawingCommand,
} from "../api/client";
import type {
  DrawingCommandResponse,
  DrawingCreateInput,
  DrawingRecord,
  DrawingUpdateInput,
  RootSymbol,
  WsDrawingsUpdateMessage,
} from "../api/types";
import type { DrawingToolId, FvgPreset } from "../drawings/types";
import type { AuthErrorHandler } from "./layoutStore";

export interface DrawingScopeTarget {
  symbol: RootSymbol;
  contractId?: string | null;
  paneId?: string | null;
}

export interface DrawingScopeState {
  symbol: RootSymbol;
  contractId: string | null;
  paneId: string | null;
  initialized: boolean;
  drawings: DrawingRecord[];
  scopeRevision: number;
  mutationEpoch: number;
  canUndo: boolean;
  canRedo: boolean;
  loading: boolean;
  mutating: boolean;
  error: string | null;
}

export interface DrawingStoreState {
  activeTool: DrawingToolId;
  magnetEnabled: boolean;
  fvgPreset: FvgPreset;
  obExtendRight: boolean;
  selectedByScope: Record<string, string | null>;
  scopes: Record<string, DrawingScopeState>;
  sessionEpoch: number;
  authErrorHandler: AuthErrorHandler | null;

  setAuthErrorHandler: (handler: AuthErrorHandler | null) => void;
  setActiveTool: (tool: DrawingToolId) => void;
  toggleMagnet: () => void;
  setMagnetEnabled: (enabled: boolean) => void;
  setFvgPreset: (preset: FvgPreset) => void;
  toggleObExtendRight: () => void;
  setObExtendRight: (extendRight: boolean) => void;
  setSelectedDrawing: (target: DrawingScopeTarget, drawingId: string | null) => void;
  clearError: (target: DrawingScopeTarget) => void;

  loadScope: (target: DrawingScopeTarget) => Promise<DrawingScopeState | null>;
  createDrawing: (
    target: DrawingScopeTarget,
    drawing: DrawingCreateInput,
  ) => Promise<DrawingCommandResponse | null>;
  updateDrawing: (
    target: DrawingScopeTarget,
    drawingId: string,
    drawing: DrawingUpdateInput,
    options?: { expectedDrawingRevision?: number },
  ) => Promise<DrawingCommandResponse | null>;
  deleteDrawing: (
    target: DrawingScopeTarget,
    drawingId: string,
  ) => Promise<DrawingCommandResponse | null>;
  clearScopeDrawings: (target: DrawingScopeTarget) => Promise<DrawingCommandResponse | null>;
  undoScope: (target: DrawingScopeTarget) => Promise<DrawingCommandResponse | null>;
  redoScope: (target: DrawingScopeTarget) => Promise<DrawingCommandResponse | null>;
  applyWsDrawingsUpdate: (msg: WsDrawingsUpdateMessage) => void;
  reset: () => void;
}

function normalizeScope(target: DrawingScopeTarget): {
  symbol: RootSymbol;
  contractId: string | null;
  paneId: string | null;
} {
  const symbol = target.symbol.trim().toUpperCase() as RootSymbol;
  const contractId = target.contractId?.trim() ? target.contractId.trim().toUpperCase() : null;
  const paneId = target.paneId?.trim() ? target.paneId.trim() : null;
  return { symbol, contractId, paneId };
}

export function buildDrawingScopeKey(
  symbol: RootSymbol,
  contractId?: string | null,
  paneId?: string | null,
): string {
  const norm = normalizeScope({ symbol, contractId: contractId ?? null, paneId: paneId ?? null });
  return `${norm.symbol}|${norm.contractId ?? ""}|${norm.paneId ?? ""}`;
}

function createEmptyScope(target: DrawingScopeTarget): DrawingScopeState {
  const norm = normalizeScope(target);
  return {
    symbol: norm.symbol,
    contractId: norm.contractId,
    paneId: norm.paneId,
    initialized: false,
    drawings: [],
    scopeRevision: 0,
    mutationEpoch: 0,
    canUndo: false,
    canRedo: false,
    loading: false,
    mutating: false,
    error: null,
  };
}

function isAuthError(err: unknown): err is ApiError {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

function isRevisionConflictError(err: unknown): err is ApiError {
  if (!(err instanceof ApiError) || err.status !== 409) {
    return false;
  }
  return err.code === "revision_conflict" || err.detailObject?.code === "revision_conflict";
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `ui-${crypto.randomUUID()}`;
  }
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function reconcileSelectedId(
  currentSelected: string | null | undefined,
  drawings: readonly DrawingRecord[],
): string | null {
  if (!currentSelected) {
    return null;
  }
  return drawings.some((d) => d.id === currentSelected) ? currentSelected : null;
}

const loadControllers = new Map<string, AbortController>();
const mutationControllers = new Set<AbortController>();
const mutationQueues = new Map<string, Promise<void>>();

function enqueueScopeMutation<T>(scopeKey: string, task: () => Promise<T>): Promise<T> {
  const previous = mutationQueues.get(scopeKey) ?? Promise.resolve();
  const resultPromise = previous.then(task, task);
  const settled = resultPromise.then(
    () => undefined,
    () => undefined,
  );
  mutationQueues.set(scopeKey, settled);
  void settled.then(() => {
    if (mutationQueues.get(scopeKey) === settled) {
      mutationQueues.delete(scopeKey);
    }
  });
  return resultPromise;
}

function abortAllControllers(): void {
  for (const controller of loadControllers.values()) {
    controller.abort();
  }
  loadControllers.clear();
  for (const controller of mutationControllers) {
    controller.abort();
  }
  mutationControllers.clear();
}

export const useDrawingStore = create<DrawingStoreState>((set, get) => {
  const updateScope = (
    key: string,
    target: DrawingScopeTarget,
    updater: (prev: DrawingScopeState) => DrawingScopeState,
  ) => {
    set((state) => {
      const prev = state.scopes[key] ?? createEmptyScope(target);
      return {
        scopes: {
          ...state.scopes,
          [key]: updater(prev),
        },
      };
    });
  };

  const applyAuthoritativeSnapshot = (
    key: string,
    target: DrawingScopeTarget,
    payload: {
      drawings: DrawingRecord[];
      scope_revision: number;
      can_undo: boolean;
      can_redo: boolean;
    },
    options?: {
      error?: string | null;
      preferredSelectedId?: string | null;
      clearLoading?: boolean;
      clearMutating?: boolean;
    },
  ) => {
    set((state) => {
      const prev = state.scopes[key] ?? createEmptyScope(target);
      const shouldReplace = !prev.initialized || payload.scope_revision >= prev.scopeRevision;
      const nextDrawings = shouldReplace ? payload.drawings : prev.drawings;
      const nextScope: DrawingScopeState = {
        ...prev,
        initialized: shouldReplace ? true : prev.initialized,
        drawings: nextDrawings,
        scopeRevision: shouldReplace ? payload.scope_revision : prev.scopeRevision,
        canUndo: shouldReplace ? payload.can_undo : prev.canUndo,
        canRedo: shouldReplace ? payload.can_redo : prev.canRedo,
        loading: options?.clearLoading ? false : prev.loading,
        mutating: options?.clearMutating ? false : prev.mutating,
        error: options?.error !== undefined ? options.error : null,
      };
      const nextSelected =
        options?.preferredSelectedId !== undefined
          ? reconcileSelectedId(options.preferredSelectedId, nextDrawings)
          : reconcileSelectedId(state.selectedByScope[key], nextDrawings);
      return {
        scopes: {
          ...state.scopes,
          [key]: nextScope,
        },
        selectedByScope: {
          ...state.selectedByScope,
          [key]: nextSelected,
        },
      };
    });
  };

  const runSerializedMutation = (
    target: DrawingScopeTarget,
    action: (ctx: {
      expectedRevision: number;
      idempotencyKey: string;
      norm: { symbol: RootSymbol; contractId: string | null; paneId: string | null };
      signal: AbortSignal;
    }) => Promise<DrawingCommandResponse>,
    options?: {
      selectCreated?: boolean;
      expectedDrawingId?: string;
      expectedDrawingRevision?: number;
      clearSelectedDrawingId?: string | "__all__";
    },
  ): Promise<DrawingCommandResponse | null> => {
    const norm = normalizeScope(target);
    const key = buildDrawingScopeKey(norm.symbol, norm.contractId, norm.paneId);
    const requestEpoch = get().sessionEpoch;

    // Synchronously advance mutationEpoch (and clear target selection on delete/clear)
    // before awaiting the per-scope queue so mid-drag interactions cancel immediately.
    set((state) => {
      const prev = state.scopes[key] ?? createEmptyScope(norm);
      const nextScopes = {
        ...state.scopes,
        [key]: {
          ...prev,
          mutationEpoch: prev.mutationEpoch + 1,
        },
      };
      if (options?.clearSelectedDrawingId === "__all__") {
        return {
          scopes: nextScopes,
          selectedByScope: { ...state.selectedByScope, [key]: null },
        };
      }
      if (
        options?.clearSelectedDrawingId !== undefined &&
        state.selectedByScope[key] === options.clearSelectedDrawingId
      ) {
        return {
          scopes: nextScopes,
          selectedByScope: { ...state.selectedByScope, [key]: null },
        };
      }
      return { scopes: nextScopes };
    });

    return enqueueScopeMutation(key, async () => {
      if (requestEpoch !== get().sessionEpoch) {
        return null;
      }

      const currentScope = get().scopes[key] ?? createEmptyScope(norm);
      if (
        options?.expectedDrawingId !== undefined &&
        options?.expectedDrawingRevision !== undefined
      ) {
        const targetDrawing = currentScope.drawings.find(
          (d) => d.id === options.expectedDrawingId,
        );
        if (!targetDrawing || targetDrawing.revision !== options.expectedDrawingRevision) {
          return null;
        }
      }

      const expectedRevision = currentScope.scopeRevision;
      const idempotencyKey = generateIdempotencyKey();
      const controller = new AbortController();
      mutationControllers.add(controller);

      updateScope(key, norm, (prev) => ({
        ...prev,
        mutating: true,
        error: null,
      }));

      try {
        const response = await action({
          expectedRevision,
          idempotencyKey,
          norm,
          signal: controller.signal,
        });
        if (controller.signal.aborted || requestEpoch !== get().sessionEpoch) {
          return null;
        }
        const preferredSelectedId =
          options?.selectCreated && response.affected_ids.length === 1
            ? response.affected_ids[0]!
            : undefined;
        applyAuthoritativeSnapshot(key, norm, response, {
          clearMutating: true,
          error: null,
          ...(preferredSelectedId !== undefined ? { preferredSelectedId } : {}),
        });
        return response;
      } catch (err: unknown) {
        if (controller.signal.aborted || isAbortError(err) || requestEpoch !== get().sessionEpoch) {
          return null;
        }
        if (isAuthError(err)) {
          updateScope(key, norm, (prev) => ({ ...prev, mutating: false }));
          get().authErrorHandler?.(err.status, err.detail);
          return null;
        }
        if (isRevisionConflictError(err)) {
          const syncController = new AbortController();
          mutationControllers.add(syncController);
          try {
            const latest = await fetchDrawings(
              norm.symbol,
              { contract_id: norm.contractId, pane_id: norm.paneId },
              syncController.signal,
            );
            if (syncController.signal.aborted || requestEpoch !== get().sessionEpoch) {
              return null;
            }
            applyAuthoritativeSnapshot(key, norm, latest, {
              clearMutating: true,
              error: `Revision conflict: synced to rev ${latest.scope_revision}`,
            });
          } catch (syncErr: unknown) {
            if (
              syncController.signal.aborted ||
              isAbortError(syncErr) ||
              requestEpoch !== get().sessionEpoch
            ) {
              return null;
            }
            if (isAuthError(syncErr)) {
              updateScope(key, norm, (prev) => ({ ...prev, mutating: false }));
              get().authErrorHandler?.(syncErr.status, syncErr.detail);
              return null;
            }
            updateScope(key, norm, (prev) => ({
              ...prev,
              mutating: false,
              error: err.detail || "Drawing revision conflict",
            }));
          } finally {
            mutationControllers.delete(syncController);
          }
          return null;
        }
        updateScope(key, norm, (prev) => ({
          ...prev,
          mutating: false,
          error: err instanceof Error ? err.message : "Drawing command failed",
        }));
        return null;
      } finally {
        mutationControllers.delete(controller);
      }
    });
  };

  return {
    activeTool: "cursor",
    magnetEnabled: false,
    fvgPreset: "auto",
    obExtendRight: true,
    selectedByScope: {},
    scopes: {},
    sessionEpoch: 0,
    authErrorHandler: null,

    setAuthErrorHandler: (handler) => {
      set({ authErrorHandler: handler });
    },

    setActiveTool: (tool) => {
      set({ activeTool: tool });
    },

    toggleMagnet: () => {
      set((state) => ({ magnetEnabled: !state.magnetEnabled }));
    },

    setMagnetEnabled: (enabled) => {
      set({ magnetEnabled: enabled });
    },

    setFvgPreset: (preset) => {
      set({ fvgPreset: preset });
    },

    toggleObExtendRight: () => {
      set((state) => ({ obExtendRight: !state.obExtendRight }));
    },

    setObExtendRight: (extendRight) => {
      set({ obExtendRight: extendRight });
    },

    setSelectedDrawing: (target, drawingId) => {
      const key = buildDrawingScopeKey(target.symbol, target.contractId, target.paneId);
      set((state) => ({
        selectedByScope: {
          ...state.selectedByScope,
          [key]: drawingId,
        },
      }));
    },

    clearError: (target) => {
      const key = buildDrawingScopeKey(target.symbol, target.contractId, target.paneId);
      updateScope(key, target, (prev) => ({ ...prev, error: null }));
    },

    loadScope: async (target) => {
      const norm = normalizeScope(target);
      const key = buildDrawingScopeKey(norm.symbol, norm.contractId, norm.paneId);
      loadControllers.get(key)?.abort();
      const controller = new AbortController();
      loadControllers.set(key, controller);
      const requestEpoch = get().sessionEpoch;

      updateScope(key, norm, (prev) => ({
        ...prev,
        loading: true,
        error: null,
      }));

      try {
        const snapshot = await fetchDrawings(
          norm.symbol,
          { contract_id: norm.contractId, pane_id: norm.paneId },
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          requestEpoch !== get().sessionEpoch ||
          loadControllers.get(key) !== controller
        ) {
          return null;
        }
        applyAuthoritativeSnapshot(key, norm, snapshot, {
          clearLoading: true,
          error: null,
        });
        return get().scopes[key] ?? null;
      } catch (err: unknown) {
        if (
          controller.signal.aborted ||
          isAbortError(err) ||
          requestEpoch !== get().sessionEpoch ||
          loadControllers.get(key) !== controller
        ) {
          return null;
        }
        if (isAuthError(err)) {
          updateScope(key, norm, (prev) => ({ ...prev, loading: false }));
          get().authErrorHandler?.(err.status, err.detail);
          return null;
        }
        updateScope(key, norm, (prev) => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : "Failed to load drawings",
        }));
        return null;
      } finally {
        if (loadControllers.get(key) === controller) {
          loadControllers.delete(key);
        }
      }
    },

    createDrawing: (target, drawing) =>
      runSerializedMutation(
        target,
        ({ expectedRevision, idempotencyKey, norm, signal }) =>
          postDrawingCommand(
            norm.symbol,
            {
              action: "create",
              expected_revision: expectedRevision,
              idempotency_key: idempotencyKey,
              contract_id: norm.contractId,
              pane_id: norm.paneId,
              created_by: drawing.created_by ?? "user",
              drawing,
            },
            signal,
          ),
        { selectCreated: true },
      ),

    updateDrawing: (target, drawingId, drawing, options) =>
      runSerializedMutation(
        target,
        ({ expectedRevision, idempotencyKey, norm, signal }) =>
          postDrawingCommand(
            norm.symbol,
            {
              action: "update",
              expected_revision: expectedRevision,
              idempotency_key: idempotencyKey,
              contract_id: norm.contractId,
              pane_id: norm.paneId,
              drawing_id: drawingId,
              drawing,
            },
            signal,
          ),
        options?.expectedDrawingRevision !== undefined
          ? {
              expectedDrawingId: drawingId,
              expectedDrawingRevision: options.expectedDrawingRevision,
            }
          : undefined,
      ),

    deleteDrawing: (target, drawingId) =>
      runSerializedMutation(
        target,
        ({ expectedRevision, idempotencyKey, norm, signal }) =>
          postDrawingCommand(
            norm.symbol,
            {
              action: "delete",
              expected_revision: expectedRevision,
              idempotency_key: idempotencyKey,
              contract_id: norm.contractId,
              pane_id: norm.paneId,
              drawing_id: drawingId,
            },
            signal,
          ),
        { clearSelectedDrawingId: drawingId },
      ),

    clearScopeDrawings: (target) =>
      runSerializedMutation(
        target,
        ({ expectedRevision, idempotencyKey, norm, signal }) =>
          clearDrawings(
            norm.symbol,
            {
              expected_revision: expectedRevision,
              idempotency_key: idempotencyKey,
              contract_id: norm.contractId,
              pane_id: norm.paneId,
            },
            signal,
          ),
        { clearSelectedDrawingId: "__all__" },
      ),

    undoScope: (target) =>
      runSerializedMutation(target, ({ expectedRevision, idempotencyKey, norm, signal }) =>
        undoDrawingCommand(
          norm.symbol,
          {
            expected_revision: expectedRevision,
            idempotency_key: idempotencyKey,
            contract_id: norm.contractId,
            pane_id: norm.paneId,
            actor: "user",
          },
          signal,
        ),
      ),

    redoScope: (target) =>
      runSerializedMutation(target, ({ expectedRevision, idempotencyKey, norm, signal }) =>
        redoDrawingCommand(
          norm.symbol,
          {
            expected_revision: expectedRevision,
            idempotency_key: idempotencyKey,
            contract_id: norm.contractId,
            pane_id: norm.paneId,
            actor: "user",
          },
          signal,
        ),
      ),

    applyWsDrawingsUpdate: (msg) => {
      const norm = normalizeScope({
        symbol: msg.symbol,
        contractId: msg.contract_id,
        paneId: msg.pane_id,
      });
      const key = buildDrawingScopeKey(norm.symbol, norm.contractId, norm.paneId);
      const current = get().scopes[key];
      if (current !== undefined && current.initialized && msg.scope_revision <= current.scopeRevision) {
        return;
      }
      applyAuthoritativeSnapshot(key, norm, msg, {
        clearLoading: true,
        error: null,
      });
    },

    reset: () => {
      abortAllControllers();
      set((state) => ({
        activeTool: "cursor",
        magnetEnabled: false,
        fvgPreset: "auto",
        obExtendRight: true,
        selectedByScope: {},
        scopes: {},
        sessionEpoch: state.sessionEpoch + 1,
      }));
    },
  };
});
