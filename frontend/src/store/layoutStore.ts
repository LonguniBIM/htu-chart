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

export const useLayoutStore = create<LayoutStoreState>((set, get) => ({
  layouts: [],
  activeLayout: null,
  activePaneId: null,
  dirty: false,
  saving: false,
  error: null,
  paneArchive: {},
  authErrorHandler: null,

  setAuthErrorHandler: (handler) => {
    set({ authErrorHandler: handler });
  },

  hydrateLayouts: (layouts) => {
    const clonedLayouts = layouts.map(cloneLayout);
    const picked = pickInitialLayout(clonedLayouts);
    set({
      layouts: clonedLayouts,
      activeLayout: picked !== null ? cloneLayout(picked) : null,
      activePaneId: picked?.panes[0]?.pane_id ?? null,
      dirty: false,
      error: null,
      paneArchive: picked !== null ? seedArchive(picked.panes) : {},
    });
  },

  selectLayout: (layoutId) => {
    const found = get().layouts.find((item) => item.id === layoutId);
    if (found === undefined) {
      return;
    }
    set({
      activeLayout: cloneLayout(found),
      activePaneId: found.panes[0]?.pane_id ?? null,
      dirty: false,
      error: null,
      paneArchive: seedArchive(found.panes),
    });
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
    const { activeLayout, activePaneId, paneArchive } = get();
    if (activeLayout === null) {
      return;
    }
    const targetCount = LAYOUT_TEMPLATE_PANE_COUNTS[templateId];
    const nextArchive: Record<string, PaneConfig> = { ...paneArchive };
    for (const pane of activeLayout.panes) {
      nextArchive[pane.pane_id] = clonePane(pane);
    }

    const referencePane =
      activeLayout.panes.find((pane) => pane.pane_id === activePaneId) ??
      activeLayout.panes[0];

    const surviving = activeLayout.panes.slice(0, targetCount).map(clonePane);
    const usedIds = new Set(surviving.map((pane) => pane.pane_id));
    const nextPanes: PaneConfig[] = [...surviving];

    while (nextPanes.length < targetCount) {
      const slotNumber = nextPanes.length + 1;
      const paneId = nextUniquePaneId(slotNumber, usedIds);
      usedIds.add(paneId);
      const archived = nextArchive[paneId];
      if (archived !== undefined) {
        const restored: PaneConfig = {
          ...archived,
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
        nextPanes.push(created);
      }
    }

    const nextActivePaneId =
      activePaneId !== null && nextPanes.some((pane) => pane.pane_id === activePaneId)
        ? activePaneId
        : (nextPanes[0]?.pane_id ?? null);

    set({
      activeLayout: {
        ...activeLayout,
        template_id: templateId,
        panes: nextPanes,
      },
      activePaneId: nextActivePaneId,
      paneArchive: nextArchive,
      dirty: true,
      error: null,
    });
  },

  setPaneSymbol: (paneId, rootSymbol) => {
    const { activeLayout, paneArchive } = get();
    if (activeLayout === null) {
      return;
    }
    const syncAll = activeLayout.sync_config.symbol;
    const nextArchive = { ...paneArchive };
    const nextPanes = activeLayout.panes.map((pane) => {
      if (!syncAll && pane.pane_id !== paneId) {
        return pane;
      }
      const updated = { ...pane, root_symbol: rootSymbol };
      nextArchive[pane.pane_id] = clonePane(updated);
      return updated;
    });
    set({
      activeLayout: {
        ...activeLayout,
        panes: nextPanes,
      },
      paneArchive: nextArchive,
      dirty: true,
      error: null,
    });
  },

  setPaneTimeframe: (paneId, timeframe) => {
    const { activeLayout, paneArchive } = get();
    if (activeLayout === null) {
      return;
    }
    const syncAll = activeLayout.sync_config.interval;
    const nextArchive = { ...paneArchive };
    const nextPanes = activeLayout.panes.map((pane) => {
      if (!syncAll && pane.pane_id !== paneId) {
        return pane;
      }
      const updated = { ...pane, timeframe };
      nextArchive[pane.pane_id] = clonePane(updated);
      return updated;
    });
    set({
      activeLayout: {
        ...activeLayout,
        panes: nextPanes,
      },
      paneArchive: nextArchive,
      dirty: true,
      error: null,
    });
  },

  toggleSync: (key) => {
    const { activeLayout } = get();
    if (activeLayout === null) {
      return;
    }
    set({
      activeLayout: {
        ...activeLayout,
        sync_config: {
          ...activeLayout.sync_config,
          [key]: !activeLayout.sync_config[key],
        },
      },
      dirty: true,
      error: null,
    });
  },

  saveActiveLayout: async (onAuthError) => {
    const { activeLayout, saving, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    set({ saving: true, error: null });
    try {
      const saved = await updateLayout(activeLayout.id, toLayoutPayload(activeLayout));
      const cloned = cloneLayout(saved);
      set((state) => ({
        layouts: state.layouts.map((item) =>
          item.id === cloned.id ? cloneLayout(cloned) : item,
        ),
        activeLayout: cloned,
        activePaneId:
          state.activePaneId !== null &&
          cloned.panes.some((pane) => pane.pane_id === state.activePaneId)
            ? state.activePaneId
            : (cloned.panes[0]?.pane_id ?? null),
        paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
        dirty: false,
        saving: false,
        error: null,
      }));
      return cloned;
    } catch (err: unknown) {
      if (isAuthStatusError(err)) {
        set({ saving: false });
        (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
        return null;
      }
      set({
        saving: false,
        error: err instanceof Error ? err.message : "Failed to save layout",
      });
      return null;
    }
  },

  saveLayoutAs: async (name, setDefault = false, onAuthError) => {
    const trimmed = name.trim();
    if (!trimmed) {
      set({ error: "Layout name cannot be empty" });
      return null;
    }
    const { activeLayout, saving, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    set({ saving: true, error: null });
    try {
      const created = await createLayout(
        toLayoutPayload(activeLayout, {
          name: trimmed,
          is_default: setDefault,
        }),
      );
      const cloned = cloneLayout(created);
      set((state) => {
        const baseLayouts = cloned.is_default
          ? state.layouts.map((item) => ({ ...item, is_default: false }))
          : [...state.layouts];
        return {
          layouts: [...baseLayouts, cloneLayout(cloned)],
          activeLayout: cloned,
          activePaneId:
            state.activePaneId !== null &&
            cloned.panes.some((pane) => pane.pane_id === state.activePaneId)
              ? state.activePaneId
              : (cloned.panes[0]?.pane_id ?? null),
          paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
          dirty: false,
          saving: false,
          error: null,
        };
      });
      return cloned;
    } catch (err: unknown) {
      if (isAuthStatusError(err)) {
        set({ saving: false });
        (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
        return null;
      }
      set({
        saving: false,
        error: err instanceof Error ? err.message : "Failed to create layout",
      });
      return null;
    }
  },

  renameActiveLayout: async (name, onAuthError) => {
    const trimmed = name.trim();
    if (!trimmed) {
      set({ error: "Layout name cannot be empty" });
      return null;
    }
    const { activeLayout, saving, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    set({ saving: true, error: null });
    try {
      const updated = await updateLayout(
        activeLayout.id,
        toLayoutPayload(activeLayout, { name: trimmed }),
      );
      const cloned = cloneLayout(updated);
      set((state) => ({
        layouts: state.layouts.map((item) =>
          item.id === cloned.id ? cloneLayout(cloned) : item,
        ),
        activeLayout: cloned,
        paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
        dirty: false,
        saving: false,
        error: null,
      }));
      return cloned;
    } catch (err: unknown) {
      if (isAuthStatusError(err)) {
        set({ saving: false });
        (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
        return null;
      }
      set({
        saving: false,
        error: err instanceof Error ? err.message : "Failed to rename layout",
      });
      return null;
    }
  },

  setActiveLayoutDefault: async (onAuthError) => {
    const { activeLayout, saving, authErrorHandler } = get();
    if (saving || activeLayout === null) {
      return null;
    }
    set({ saving: true, error: null });
    try {
      const updated = await updateLayout(
        activeLayout.id,
        toLayoutPayload(activeLayout, { is_default: true }),
      );
      const cloned = cloneLayout({ ...updated, is_default: true });
      set((state) => ({
        layouts: state.layouts.map((item) =>
          item.id === cloned.id
            ? cloneLayout(cloned)
            : { ...item, is_default: false },
        ),
        activeLayout: cloned,
        paneArchive: { ...state.paneArchive, ...seedArchive(cloned.panes) },
        dirty: false,
        saving: false,
        error: null,
      }));
      return cloned;
    } catch (err: unknown) {
      if (isAuthStatusError(err)) {
        set({ saving: false });
        (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
        return null;
      }
      set({
        saving: false,
        error: err instanceof Error ? err.message : "Failed to set default layout",
      });
      return null;
    }
  },

  deleteLayoutById: async (layoutId, onAuthError) => {
    const { saving, authErrorHandler } = get();
    if (saving) {
      return false;
    }
    set({ saving: true, error: null });
    try {
      const targetRecord = get().layouts.find((item) => item.id === layoutId);
      await deleteLayout(layoutId);

      let remaining = get().layouts
        .filter((item) => item.id !== layoutId)
        .map(cloneLayout);

      if (targetRecord?.is_default && remaining.length > 0) {
        let syncedFromServer: LayoutRecord[] | null = null;
        try {
          const fetched = await listLayouts();
          if (Array.isArray(fetched) && fetched.length > 0) {
            syncedFromServer = fetched.map(cloneLayout);
          }
        } catch (refreshErr: unknown) {
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

      const currentActive = get().activeLayout;
      const nextActive =
        currentActive !== null && currentActive.id !== layoutId
          ? (remaining.find((item) => item.id === currentActive.id) ?? currentActive)
          : pickInitialLayout(remaining);
      const nextActiveWasReplaced =
        currentActive === null || currentActive.id === layoutId;

      set((state) => ({
        layouts: remaining,
        activeLayout: nextActive !== null ? cloneLayout(nextActive) : null,
        activePaneId: nextActiveWasReplaced
          ? (nextActive?.panes[0]?.pane_id ?? null)
          : state.activePaneId,
        paneArchive: nextActiveWasReplaced
          ? (nextActive !== null ? seedArchive(nextActive.panes) : {})
          : state.paneArchive,
        dirty: nextActiveWasReplaced ? false : state.dirty,
        saving: false,
        error: null,
      }));
      return true;
    } catch (err: unknown) {
      if (isAuthStatusError(err)) {
        set({ saving: false });
        (onAuthError ?? authErrorHandler)?.(err.status, err.detail);
        return false;
      }
      set({
        saving: false,
        error: err instanceof Error ? err.message : "Failed to delete layout",
      });
      return false;
    }
  },

  clearError: () => {
    set({ error: null });
  },

  reset: () => {
    set({
      layouts: [],
      activeLayout: null,
      activePaneId: null,
      dirty: false,
      saving: false,
      error: null,
      paneArchive: {},
    });
  },
}));
