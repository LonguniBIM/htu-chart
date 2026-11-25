import { create } from "zustand";
import {
  ApiError,
  createLayout,
  deleteLayout,
  listLayouts,
  updateLayout,
} from "../api/client";
import {
  LAYOUT_TEMPLATE_PANE_COUNTS,
  ROOT_SYMBOLS,
  type LayoutPayload,
  type LayoutRecord,
  type LayoutTemplateId,
  type PaneConfig,
  type RootSymbol,
  type SyncConfig,
  type Timeframe,
} from "../api/types";

export type AuthErrorHandler = (status: number, detail: string) => void;

export interface LayoutStoreState {
  layouts: LayoutRecord[];
  activeLayout: LayoutRecord | null;
  activePaneId: string | null;
  dirty: boolean;
  saving: boolean;
  error: string | null;
  paneArchive: Record<string, PaneConfig>;
  paneArchiveBySlot: Record<number, PaneConfig>;
  sessionEpoch: number;
  draftRevision: number;
  authErrorHandler: AuthErrorHandler | null;

  setAuthErrorHandler: (handler: AuthErrorHandler | null) => void;
  hydrateLayouts: (layouts: LayoutRecord[]) => void;
  selectLayout: (layoutId: string) => void;
  selectPane: (paneId: string) => void;
  setTemplate: (templateId: LayoutTemplateId) => void;
  setPaneSymbol: (paneId: string, rootSymbol: RootSymbol) => void;
  setPaneTimeframe: (paneId: string, timeframe: Timeframe) => void;
  toggleSync: (key: keyof SyncConfig) => void;
  saveActiveLayout: (onAuthError?: AuthErrorHandler) => Promise<LayoutRecord | null>;
  saveLayoutAs: (
    name: string,
    setDefault?: boolean,
    onAuthError?: AuthErrorHandler,
  ) => Promise<LayoutRecord | null>;
  renameActiveLayout: (
    name: string,
    onAuthError?: AuthErrorHandler,
  ) => Promise<LayoutRecord | null>;
  setActiveLayoutDefault: (onAuthError?: AuthErrorHandler) => Promise<LayoutRecord | null>;
  deleteLayoutById: (
    layoutId: string,
    onAuthError?: AuthErrorHandler,
  ) => Promise<boolean>;
  clearError: () => void;
  reset: () => void;
}

function clonePane(pane: PaneConfig): PaneConfig {
  return { ...pane };
}

function cloneLayout(layout: LayoutRecord): LayoutRecord {
  return {
    ...layout,
    panes: layout.panes.map(clonePane),
    sync_config: { ...layout.sync_config },
  };
}

function seedArchive(panes: readonly PaneConfig[]): Record<string, PaneConfig> {
  const archive: Record<string, PaneConfig> = {};
  for (const pane of panes) {
    archive[pane.pane_id] = clonePane(pane);
  }
  return archive;
}

function seedSlotArchive(panes: readonly PaneConfig[]): Record<number, PaneConfig> {
  const archive: Record<number, PaneConfig> = {};
  panes.forEach((pane, idx) => {
    archive[idx] = clonePane(pane);
  });
  return archive;
}

function pickInitialLayout(layouts: readonly LayoutRecord[]): LayoutRecord | null {
  return layouts.find((item) => item.is_default) ?? layouts[0] ?? null;
}

function toLayoutPayload(
  layout: LayoutRecord,
  overrides?: Partial<LayoutPayload>,
): LayoutPayload {
  return {
    name: overrides?.name ?? layout.name,
    template_id: overrides?.template_id ?? layout.template_id,
    panes: (overrides?.panes ?? layout.panes).map(clonePane),
    sync_config: { ...(overrides?.sync_config ?? layout.sync_config) },
    is_default: overrides?.is_default ?? layout.is_default,
  };
}

function nextUniquePaneId(preferredSlot: number, usedIds: Set<string>): string {
  const preferred = `pane-${preferredSlot}`;
  if (!usedIds.has(preferred)) {
    return preferred;
  }
  for (let i = 1; i <= 64; i += 1) {
    const candidate = `pane-${i}`;
    if (!usedIds.has(candidate)) {
      return candidate;
    }
  }
  return `pane-${Date.now()}`;
}

function isAuthStatusError(err: unknown): err is ApiError {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

let activeCrudController: AbortController | null = null;

function abortActiveCrud(): void {
  activeCrudController?.abort();
  activeCrudController = null;
}

interface CrudRequestContext {
  controller: AbortController;
  isStale: () => boolean;
}

function beginCrudRequest(
  get: () => LayoutStoreState,
  set: (partial: Partial<LayoutStoreState>) => void,
): CrudRequestContext {
  const controller = new AbortController();
  activeCrudController = controller;
  const requestEpoch = get().sessionEpoch;
  set({ saving: true, error: null });
  return {
    controller,
    isStale: () => controller.signal.aborted || requestEpoch !== get().sessionEpoch,
  };
}

function finishCrudRequest(controller: AbortController): void {
  if (activeCrudController === controller) {
    activeCrudController = null;
  }
}

function handleCrudError(
  err: unknown,
  isStale: () => boolean,
  set: (partial: Partial<LayoutStoreState>) => void,
  authErrorHandler: AuthErrorHandler | null,
  onAuthError: AuthErrorHandler | undefined,
  fallbackMessage: string,
): void {
  if (isStale()) {
    return;
  }
  if (isAuthStatusError(err)) {
    set({ saving: false });
    (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
    return;
  }
  set({
    saving: false,
    error: err instanceof Error ? err.message : fallbackMessage,
  });
}

function resolveSurvivingActivePaneId(
  currentActivePaneId: string | null,
  panes: readonly PaneConfig[],
): string | null {
  return currentActivePaneId !== null &&
    panes.some((pane) => pane.pane_id === currentActivePaneId)
    ? currentActivePaneId
    : (panes[0]?.pane_id ?? null);
}

export const useLayoutStore = create<LayoutStoreState>((set, get) => ({
  layouts: [],
  activeLayout: null,
  activePaneId: null,
  dirty: false,
  saving: false,
  error: null,
  paneArchive: {},
  paneArchiveBySlot: {},
  sessionEpoch: 0,
  draftRevision: 0,
  authErrorHandler: null,

  setAuthErrorHandler: (handler) => {
    set({ authErrorHandler: handler });
  },

  hydrateLayouts: (layouts) => {
    abortActiveCrud();
    const clonedLayouts = layouts.map(cloneLayout);
    const picked = pickInitialLayout(clonedLayouts);
    set((state) => ({
      layouts: clonedLayouts,
      activeLayout: picked !== null ? cloneLayout(picked) : null,
      activePaneId: picked?.panes[0]?.pane_id ?? null,
      dirty: false,
      saving: false,
      error: null,
      paneArchive: picked !== null ? seedArchive(picked.panes) : {},
      paneArchiveBySlot: picked !== null ? seedSlotArchive(picked.panes) : {},
      sessionEpoch: state.sessionEpoch + 1,
      draftRevision: state.draftRevision + 1,
    }));
  },

  selectLayout: (layoutId) => {
    const found = get().layouts.find((item) => item.id === layoutId);
    if (found === undefined) {
      return;
    }
    set((state) => ({
      activeLayout: cloneLayout(found),
      activePaneId: found.panes[0]?.pane_id ?? null,
      dirty: false,
      error: null,
      paneArchive: seedArchive(found.panes),
      paneArchiveBySlot: seedSlotArchive(found.panes),
      draftRevision: state.draftRevision + 1,
    }));
  },

  selectPane: (paneId) => {
    const { activeLayout } = get();
    if (activeLayout === null) {
      return;
    }
    if (activeLayout.panes.some((pane) => pane.pane_id === paneId)) {
      set({ activePaneId: paneId });
    }
  },

  setTemplate: (templateId) => {
    const { activeLayout, activePaneId, paneArchive, paneArchiveBySlot } = get();
    if (activeLayout === null) {
      return;
    }
    const targetCount = LAYOUT_TEMPLATE_PANE_COUNTS[templateId];
    if (
      activeLayout.template_id === templateId &&
      activeLayout.panes.length === targetCount
    ) {
      return;
    }
    const nextArchive: Record<string, PaneConfig> = { ...paneArchive };
    const nextArchiveBySlot: Record<number, PaneConfig> = { ...paneArchiveBySlot };
    activeLayout.panes.forEach((pane, idx) => {
      nextArchive[pane.pane_id] = clonePane(pane);
      nextArchiveBySlot[idx] = clonePane(pane);
    });

    const referencePane =
      activeLayout.panes.find((pane) => pane.pane_id === activePaneId) ??
      activeLayout.panes[0];

    const surviving = activeLayout.panes.slice(0, targetCount).map(clonePane);
    const usedIds = new Set(surviving.map((pane) => pane.pane_id));
    const nextPanes: PaneConfig[] = [...surviving];

    while (nextPanes.length < targetCount) {
      const slotIndex = nextPanes.length;
      const slotNumber = slotIndex + 1;
      const slotArchived = nextArchiveBySlot[slotIndex];
      const paneId =
        slotArchived !== undefined && !usedIds.has(slotArchived.pane_id)
          ? slotArchived.pane_id
          : nextUniquePaneId(slotNumber, usedIds);
      usedIds.add(paneId);

      const archived = slotArchived ?? nextArchive[paneId];
      if (archived !== undefined) {
        const restored: PaneConfig = {
          ...archived,
          pane_id: paneId,
          root_symbol:
            activeLayout.sync_config.symbol && referencePane !== undefined
              ? referencePane.root_symbol
              : archived.root_symbol,
          timeframe:
            activeLayout.sync_config.interval && referencePane !== undefined
              ? referencePane.timeframe
              : archived.timeframe,
        };
        nextArchive[paneId] = clonePane(restored);
        nextArchiveBySlot[slotIndex] = clonePane(restored);
        nextPanes.push(restored);
      } else {
        const defaultSymbol =
          ROOT_SYMBOLS[(slotNumber - 1) % ROOT_SYMBOLS.length] ?? "MES";
        const created: PaneConfig = {
          pane_id: paneId,
          root_symbol:
            activeLayout.sync_config.symbol && referencePane !== undefined
              ? referencePane.root_symbol
              : defaultSymbol,
          timeframe:
            activeLayout.sync_config.interval && referencePane !== undefined
              ? referencePane.timeframe
              : "5m",
          live_mode: false,
          playback_mode: "live",
        };
        nextArchive[paneId] = clonePane(created);
        nextArchiveBySlot[slotIndex] = clonePane(created);
        nextPanes.push(created);
      }
    }

    const nextActivePaneId =
      activePaneId !== null && nextPanes.some((pane) => pane.pane_id === activePaneId)
        ? activePaneId
        : (nextPanes[0]?.pane_id ?? null);

    set((state) => ({
      activeLayout: {
        ...activeLayout,
        template_id: templateId,
        panes: nextPanes,
      },
      activePaneId: nextActivePaneId,
      paneArchive: nextArchive,
      paneArchiveBySlot: nextArchiveBySlot,
      dirty: true,
      error: null,
      draftRevision: state.draftRevision + 1,
    }));
  },

  setPaneSymbol: (paneId, rootSymbol) => {
    const { activeLayout, paneArchive, paneArchiveBySlot } = get();
    if (activeLayout === null) {
      return;
    }
    const syncAll = activeLayout.sync_config.symbol;
    const nextArchive = { ...paneArchive };
    const nextArchiveBySlot = { ...paneArchiveBySlot };
    const nextPanes = activeLayout.panes.map((pane, idx) => {
      if (!syncAll && pane.pane_id !== paneId) {
        return pane;
      }
      const updated = { ...pane, root_symbol: rootSymbol };
      nextArchive[pane.pane_id] = clonePane(updated);
      nextArchiveBySlot[idx] = clonePane(updated);
      return updated;
    });
    set((state) => ({
      activeLayout: {
        ...activeLayout,
        panes: nextPanes,
      },
      paneArchive: nextArchive,
      paneArchiveBySlot: nextArchiveBySlot,
      dirty: true,
      error: null,
      draftRevision: state.draftRevision + 1,
    }));
  },

  setPaneTimeframe: (paneId, timeframe) => {
    const { activeLayout, paneArchive, paneArchiveBySlot } = get();
    if (activeLayout === null) {
      return;
    }
    const syncAll = activeLayout.sync_config.interval;
    const nextArchive = { ...paneArchive };
    const nextArchiveBySlot = { ...paneArchiveBySlot };
    const nextPanes = activeLayout.panes.map((pane, idx) => {
      if (!syncAll && pane.pane_id !== paneId) {
        return pane;
      }
      const updated = { ...pane, timeframe };
      nextArchive[pane.pane_id] = clonePane(updated);
      nextArchiveBySlot[idx] = clonePane(updated);
      return updated;
    });
    set((state) => ({
      activeLayout: {
        ...activeLayout,
        panes: nextPanes,
      },
      paneArchive: nextArchive,
      paneArchiveBySlot: nextArchiveBySlot,
      dirty: true,
      error: null,
      draftRevision: state.draftRevision + 1,
    }));
  },

  toggleSync: (key) => {
    const { activeLayout } = get();
    if (activeLayout === null) {
      return;
    }
    set((state) => ({
      activeLayout: {
        ...activeLayout,
        sync_config: {
          ...activeLayout.sync_config,
          [key]: !activeLayout.sync_config[key],
        },
      },
      dirty: true,
      error: null,
      draftRevision: state.draftRevision + 1,
    }));
  },

  saveActiveLayout: async (onAuthError) => {
    const { activeLayout, saving, draftRevision, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    const targetLayoutId = activeLayout.id;
    const targetDraftRevision = draftRevision;
    const { controller, isStale } = beginCrudRequest(get, set);
    try {
      const saved = await updateLayout(
        targetLayoutId,
        toLayoutPayload(activeLayout),
        controller.signal,
      );
      if (isStale()) {
        return null;
      }
      const cloned = cloneLayout(saved);
      set((state) => {
        const nextLayouts = state.layouts.map((item) =>
          item.id === cloned.id ? cloneLayout(cloned) : item,
        );
        const canReplaceActive =
          state.activeLayout?.id === targetLayoutId &&
          state.draftRevision === targetDraftRevision;
        if (!canReplaceActive) {
          return {
            layouts: nextLayouts,
            saving: false,
            error: null,
          };
        }
        return {
          layouts: nextLayouts,
          activeLayout: cloned,
          activePaneId: resolveSurvivingActivePaneId(state.activePaneId, cloned.panes),
          paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
          paneArchiveBySlot: {
            ...state.paneArchiveBySlot,
            ...seedSlotArchive(cloned.panes),
          },
          dirty: false,
          saving: false,
          error: null,
        };
      });
      return cloned;
    } catch (err: unknown) {
      handleCrudError(
        err,
        isStale,
        set,
        authErrorHandler,
        onAuthError,
        "Failed to save layout",
      );
      return null;
    } finally {
      finishCrudRequest(controller);
    }
  },

  saveLayoutAs: async (name, setDefault = false, onAuthError) => {
    const trimmed = name.trim();
    if (!trimmed) {
      set({ error: "Layout name cannot be empty" });
      return null;
    }
    const { activeLayout, saving, draftRevision, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    const targetLayoutId = activeLayout.id;
    const targetDraftRevision = draftRevision;
    const { controller, isStale } = beginCrudRequest(get, set);
    try {
      const created = await createLayout(
        toLayoutPayload(activeLayout, {
          name: trimmed,
          is_default: setDefault,
        }),
        controller.signal,
      );
      if (isStale()) {
        return null;
      }
      const cloned = cloneLayout(created);
      set((state) => {
        const baseLayouts = cloned.is_default
          ? state.layouts.map((item) => ({ ...item, is_default: false }))
          : [...state.layouts];
        const nextLayouts = [...baseLayouts, cloneLayout(cloned)];
        const canReplaceActive =
          state.activeLayout?.id === targetLayoutId &&
          state.draftRevision === targetDraftRevision;
        if (!canReplaceActive) {
          return {
            layouts: nextLayouts,
            activeLayout:
              cloned.is_default && state.activeLayout !== null
                ? { ...state.activeLayout, is_default: false }
                : state.activeLayout,
            saving: false,
            error: null,
          };
        }
        return {
          layouts: nextLayouts,
          activeLayout: cloned,
          activePaneId: resolveSurvivingActivePaneId(state.activePaneId, cloned.panes),
          paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
          paneArchiveBySlot: {
            ...state.paneArchiveBySlot,
            ...seedSlotArchive(cloned.panes),
          },
          dirty: false,
          saving: false,
          error: null,
        };
      });
      return cloned;
    } catch (err: unknown) {
      handleCrudError(
        err,
        isStale,
        set,
        authErrorHandler,
        onAuthError,
        "Failed to create layout",
      );
      return null;
    } finally {
      finishCrudRequest(controller);
    }
  },

  renameActiveLayout: async (name, onAuthError) => {
    const trimmed = name.trim();
    if (!trimmed) {
      set({ error: "Layout name cannot be empty" });
      return null;
    }
    const { activeLayout, saving, draftRevision, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    const targetLayoutId = activeLayout.id;
    const targetDraftRevision = draftRevision;
    const { controller, isStale } = beginCrudRequest(get, set);
    try {
      const updated = await updateLayout(
        targetLayoutId,
        toLayoutPayload(activeLayout, { name: trimmed }),
        controller.signal,
      );
      if (isStale()) {
        return null;
      }
      const cloned = cloneLayout(updated);
      set((state) => {
        const nextLayouts = state.layouts.map((item) =>
          item.id === cloned.id ? cloneLayout(cloned) : item,
        );
        const isSameLayout = state.activeLayout?.id === targetLayoutId;
        if (!isSameLayout) {
          return {
            layouts: nextLayouts,
            saving: false,
            error: null,
          };
        }
        if (state.draftRevision !== targetDraftRevision) {
          return {
            layouts: nextLayouts,
            activeLayout:
              state.activeLayout !== null
                ? { ...state.activeLayout, name: cloned.name }
                : null,
            saving: false,
            error: null,
          };
        }
        return {
          layouts: nextLayouts,
          activeLayout: cloned,
          paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
          paneArchiveBySlot: {
            ...state.paneArchiveBySlot,
            ...seedSlotArchive(cloned.panes),
          },
          dirty: false,
          saving: false,
          error: null,
        };
      });
      return cloned;
    } catch (err: unknown) {
      handleCrudError(
        err,
        isStale,
        set,
        authErrorHandler,
        onAuthError,
        "Failed to rename layout",
      );
      return null;
    } finally {
      finishCrudRequest(controller);
    }
  },

  setActiveLayoutDefault: async (onAuthError) => {
    const { activeLayout, saving, draftRevision, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    const targetLayoutId = activeLayout.id;
    const targetDraftRevision = draftRevision;
    const { controller, isStale } = beginCrudRequest(get, set);
    try {
      const updated = await updateLayout(
        targetLayoutId,
        toLayoutPayload(activeLayout, { is_default: true }),
        controller.signal,
      );
      if (isStale()) {
        return null;
      }
      const cloned = cloneLayout({ ...updated, is_default: true });
      set((state) => {
        const nextLayouts = state.layouts.map((item) =>
          item.id === cloned.id
            ? cloneLayout(cloned)
            : { ...item, is_default: false },
        );
        const isSameLayout = state.activeLayout?.id === targetLayoutId;
        if (!isSameLayout) {
          return {
            layouts: nextLayouts,
            activeLayout:
              state.activeLayout !== null
                ? { ...state.activeLayout, is_default: false }
                : null,
            saving: false,
            error: null,
          };
        }
        if (state.draftRevision !== targetDraftRevision) {
          return {
            layouts: nextLayouts,
            activeLayout:
              state.activeLayout !== null
                ? { ...state.activeLayout, is_default: true }
                : null,
            saving: false,
            error: null,
          };
        }
        return {
          layouts: nextLayouts,
          activeLayout: cloned,
          paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
          paneArchiveBySlot: {
            ...state.paneArchiveBySlot,
            ...seedSlotArchive(cloned.panes),
          },
          dirty: false,
          saving: false,
          error: null,
        };
      });
      return cloned;
    } catch (err: unknown) {
      handleCrudError(
        err,
        isStale,
        set,
        authErrorHandler,
        onAuthError,
        "Failed to set default layout",
      );
      return null;
    } finally {
      finishCrudRequest(controller);
    }
  },

  deleteLayoutById: async (layoutId, onAuthError) => {
    const { saving, authErrorHandler } = get();
    if (saving) {
      return false;
    }
    const { controller, isStale } = beginCrudRequest(get, set);
    try {
      const targetRecord = get().layouts.find((item) => item.id === layoutId);
      await deleteLayout(layoutId, controller.signal);
      if (isStale()) {
        return false;
      }

      let remaining = get().layouts
        .filter((item) => item.id !== layoutId)
        .map(cloneLayout);

      if (targetRecord?.is_default && remaining.length > 0) {
        let syncedFromServer: LayoutRecord[] | null = null;
        try {
          const fetched = await listLayouts(controller.signal);
          if (isStale()) {
            return false;
          }
          if (Array.isArray(fetched) && fetched.length > 0) {
            syncedFromServer = fetched.map(cloneLayout);
          }
        } catch (refreshErr: unknown) {
          if (isStale()) {
            return false;
          }
          if (isAuthStatusError(refreshErr)) {
            set({ saving: false });
            (onAuthError ?? authErrorHandler)?.(refreshErr.status, refreshErr.detail);
            return false;
          }
        }
        if (syncedFromServer !== null) {
          remaining = syncedFromServer;
        } else if (!remaining.some((item) => item.is_default) && remaining[0] !== undefined) {
          remaining[0] = { ...remaining[0], is_default: true };
        }
      }

      set((state) => {
        const currentActive = state.activeLayout;
        if (currentActive !== null && currentActive.id !== layoutId) {
          const matchedRecord = remaining.find((item) => item.id === currentActive.id);
          return {
            layouts: remaining,
            activeLayout:
              matchedRecord !== undefined
                ? { ...currentActive, is_default: matchedRecord.is_default }
                : currentActive,
            saving: false,
            error: null,
          };
        }
        const nextActive = pickInitialLayout(remaining);
        return {
          layouts: remaining,
          activeLayout: nextActive !== null ? cloneLayout(nextActive) : null,
          activePaneId: nextActive?.panes[0]?.pane_id ?? null,
          paneArchive: nextActive !== null ? seedArchive(nextActive.panes) : {},
          paneArchiveBySlot:
            nextActive !== null ? seedSlotArchive(nextActive.panes) : {},
          dirty: false,
          saving: false,
          error: null,
          draftRevision: state.draftRevision + 1,
        };
      });
      return true;
    } catch (err: unknown) {
      handleCrudError(
        err,
        isStale,
        set,
        authErrorHandler,
        onAuthError,
        "Failed to delete layout",
      );
      return false;
    } finally {
      finishCrudRequest(controller);
    }
  },

  clearError: () => {
    set({ error: null });
  },

  reset: () => {
    abortActiveCrud();
    set((state) => ({
      layouts: [],
      activeLayout: null,
      activePaneId: null,
      dirty: false,
      saving: false,
      error: null,
      paneArchive: {},
      paneArchiveBySlot: {},
      sessionEpoch: state.sessionEpoch + 1,
      draftRevision: state.draftRevision + 1,
    }));
  },
}));
