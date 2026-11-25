import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LayoutPayload, LayoutRecord } from "../api/types";
import { useLayoutStore } from "./layoutStore";

const createLayoutMock = vi.fn<
  (payload: LayoutPayload, signal?: AbortSignal) => Promise<LayoutRecord>
>();
const updateLayoutMock = vi.fn<
  (layoutId: string, payload: LayoutPayload, signal?: AbortSignal) => Promise<LayoutRecord>
>();
const deleteLayoutMock = vi.fn<
  (layoutId: string, signal?: AbortSignal) => Promise<{ deleted: boolean; id: string }>
>();
const listLayoutsMock = vi.fn<(signal?: AbortSignal) => Promise<LayoutRecord[]>>();

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    createLayout: (payload: LayoutPayload, signal?: AbortSignal) =>
      createLayoutMock(payload, signal),
    updateLayout: (layoutId: string, payload: LayoutPayload, signal?: AbortSignal) =>
      updateLayoutMock(layoutId, payload, signal),
    deleteLayout: (layoutId: string, signal?: AbortSignal) =>
      deleteLayoutMock(layoutId, signal),
    listLayouts: (signal?: AbortSignal) => listLayoutsMock(signal),
  };
});

function makeFourPaneLayout(overrides: Partial<LayoutRecord> = {}): LayoutRecord {
  return {
    id: "layout-4-future",
    name: "4 Future",
    template_id: "4-grid-2x2",
    panes: [
      { pane_id: "pane-1", root_symbol: "MES", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-2", root_symbol: "MNQ", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-3", root_symbol: "MYM", timeframe: "5m", live_mode: false, playback_mode: "live" },
      { pane_id: "pane-4", root_symbol: "MGC", timeframe: "5m", live_mode: false, playback_mode: "live" },
    ],
    sync_config: {
      symbol: false,
      interval: true,
      crosshair: true,
      time: false,
      dateRange: false,
    },
    is_default: true,
    created_at_utc: 1700000000,
    updated_at_utc: 1700000000,
    ...overrides,
  };
}

describe("useLayoutStore (Phase 3 Part 2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useLayoutStore.getState().setAuthErrorHandler(null);
    useLayoutStore.getState().reset();
  });

  it("preserves hidden pane configurations across 4 -> 2 -> 4 template transitions via paneArchive", () => {
    /**
     * Contract: Customizing panes 3 & 4, shrinking template from 4 panes to 2, modifying pane 2,
     * and expanding back to 4 panes preserves panes 3 & 4's customized symbol/timeframe while
     * keeping pane 2's edit and marking the layout dirty.
     * Defect caught: Shrinking layout permanently losing pane 3/4 customizations on re-expansion.
     */
    const store = useLayoutStore.getState();
    store.hydrateLayouts([makeFourPaneLayout()]);

    // Turn off interval sync so pane-level timeframes stay independent
    store.toggleSync("interval");
    store.setPaneSymbol("pane-3", "MES");
    store.setPaneTimeframe("pane-3", "15m");
    store.setPaneTimeframe("pane-4", "1h");

    // Shrink 4 -> 2
    useLayoutStore.getState().setTemplate("2-cols");
    let active = useLayoutStore.getState().activeLayout!;
    expect(active.template_id).toBe("2-cols");
    expect(active.panes).toHaveLength(2);
    expect(active.panes.map((p) => p.pane_id)).toEqual(["pane-1", "pane-2"]);
    expect(useLayoutStore.getState().dirty).toBe(true);

    // Edit surviving pane-2 while in 2-pane mode
    useLayoutStore.getState().setPaneTimeframe("pane-2", "30m");

    // Expand 2 -> 4
    useLayoutStore.getState().setTemplate("4-grid-2x2");
    active = useLayoutStore.getState().activeLayout!;
    expect(active.panes).toHaveLength(4);
    expect(active.panes[1]!.timeframe).toBe("30m");
    expect(active.panes[2]).toMatchObject({
      pane_id: "pane-3",
      root_symbol: "MES",
      timeframe: "15m",
    });
    expect(active.panes[3]).toMatchObject({
      pane_id: "pane-4",
      root_symbol: "MGC",
      timeframe: "1h",
    });
  });

  it("creates deterministic new panes cycling MES/MNQ/MYM/MGC and respects Symbol/Interval sync toggles", () => {
    /**
     * Contract: Expanding from 1 pane to 4 creates deterministic pane-2..pane-4 cycling
     * MES/MNQ/MYM/MGC when sync toggles are off, and inherits active pane symbol/timeframe when on.
     * setPaneSymbol and setPaneTimeframe update all panes only when their respective sync toggle is true.
     * Defect caught: Incorrect pane symbol/timeframe propagation or duplicate pane IDs.
     */
    const single = makeFourPaneLayout({
      id: "layout-single",
      name: "Single",
      template_id: "1-single",
      panes: [
        { pane_id: "pane-1", root_symbol: "MGC", timeframe: "15m", live_mode: false, playback_mode: "live" },
      ],
      sync_config: {
        symbol: false,
        interval: false,
        crosshair: false,
        time: false,
        dateRange: false,
      },
    });
    useLayoutStore.getState().hydrateLayouts([single]);
    useLayoutStore.getState().setTemplate("4-grid-2x2");

    let active = useLayoutStore.getState().activeLayout!;
    expect(active.panes.map((p) => [p.pane_id, p.root_symbol, p.timeframe])).toEqual([
      ["pane-1", "MGC", "15m"],
      ["pane-2", "MNQ", "5m"],
      ["pane-3", "MYM", "5m"],
      ["pane-4", "MGC", "5m"],
    ]);

    // Sync Symbol OFF -> only target pane changes; Sync Symbol ON -> all panes change
    useLayoutStore.getState().setPaneSymbol("pane-2", "MES");
    expect(useLayoutStore.getState().activeLayout!.panes.map((p) => p.root_symbol)).toEqual([
      "MGC",
      "MES",
      "MYM",
      "MGC",
    ]);

    useLayoutStore.getState().toggleSync("symbol");
    useLayoutStore.getState().setPaneSymbol("pane-1", "MNQ");
    expect(useLayoutStore.getState().activeLayout!.panes.map((p) => p.root_symbol)).toEqual([
      "MNQ",
      "MNQ",
      "MNQ",
      "MNQ",
    ]);

    // Sync Interval ON -> all panes change timeframe together
    useLayoutStore.getState().toggleSync("interval");
    useLayoutStore.getState().setPaneTimeframe("pane-3", "1h");
    expect(useLayoutStore.getState().activeLayout!.panes.map((p) => p.timeframe)).toEqual([
      "1h",
      "1h",
      "1h",
      "1h",
    ]);

    // Expanding 4 -> 5 with both Symbol & Interval sync ON inherits active/first pane's symbol & timeframe
    useLayoutStore.getState().setTemplate("5-top2-bot3");
    active = useLayoutStore.getState().activeLayout!;
    expect(active.panes[4]).toEqual({
      pane_id: "pane-5",
      root_symbol: "MNQ",
      timeframe: "1h",
      live_mode: false,
      playback_mode: "live",
    });
  });

  it("handles CRUD persistence, Save As default switching, 409 delete conflict, and 401/403 auth fail-closed", async () => {
    /**
     * Contract: saveActiveLayout, saveLayoutAs, renameActiveLayout, setActiveLayoutDefault, and
     * deleteLayoutById persist via /api/layouts client functions, keep sole is_default accuracy,
     * surface 409 Conflict into error, guard concurrent saves, and route 401/403 to onAuthError.
     * Defect caught: Stale dirty/default flags, swallowed 409 error, or unhandled 401/403 auth failures.
     */
    const base = makeFourPaneLayout();
    const createdRecord: LayoutRecord = {
      ...base,
      id: "layout-scalp",
      name: "Scalp MNQ",
      is_default: true,
    };
    const renamedRecord: LayoutRecord = {
      ...createdRecord,
      name: "Scalp MNQ Pro",
    };

    createLayoutMock.mockResolvedValueOnce(createdRecord);
    updateLayoutMock.mockResolvedValueOnce(renamedRecord);
    deleteLayoutMock
      .mockRejectedValueOnce(new ApiError(409, "Cannot delete the last remaining layout"))
      .mockResolvedValueOnce({ deleted: true, id: "layout-scalp" });
    listLayoutsMock.mockResolvedValueOnce([{ ...base, is_default: true }]);

    useLayoutStore.getState().hydrateLayouts([base]);
    useLayoutStore.getState().setPaneSymbol("pane-1", "MNQ");
    expect(useLayoutStore.getState().dirty).toBe(true);

    // Rejects blank Save As locally without API call
    const emptyRes = await useLayoutStore.getState().saveLayoutAs("   ");
    expect(emptyRes).toBeNull();
    expect(useLayoutStore.getState().error).toBe("Layout name cannot be empty");
    expect(createLayoutMock).not.toHaveBeenCalled();

    // Save As with setDefault=true clears previous layout's is_default
    const created = await useLayoutStore.getState().saveLayoutAs("  Scalp MNQ ", true);
    expect(created?.id).toBe("layout-scalp");
    expect(createLayoutMock).toHaveBeenCalledWith(
      {
        name: "Scalp MNQ",
        template_id: base.template_id,
        panes: base.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MNQ" } : p)),
        sync_config: base.sync_config,
        is_default: true,
      },
      expect.any(AbortSignal),
    );
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().layouts.filter((l) => l.is_default)).toHaveLength(1);
    expect(useLayoutStore.getState().layouts.find((l) => l.id === base.id)?.is_default).toBe(
      false,
    );

    // Rename
    const renamed = await useLayoutStore.getState().renameActiveLayout(" Scalp MNQ Pro ");
    expect(renamed?.name).toBe("Scalp MNQ Pro");
    expect(useLayoutStore.getState().activeLayout?.name).toBe("Scalp MNQ Pro");

    // 409 Conflict surfaced cleanly in error
    const conflictDeleted = await useLayoutStore.getState().deleteLayoutById("layout-4-future");
    expect(conflictDeleted).toBe(false);
    expect(useLayoutStore.getState().error).toContain("409");

    // Delete active default -> syncs promoted default via listLayouts()
    const deletedOk = await useLayoutStore.getState().deleteLayoutById("layout-scalp");
    expect(deletedOk).toBe(true);
    expect(listLayoutsMock).toHaveBeenCalledTimes(1);
    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-4-future");
    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);

    // 401/403 fail-closed auth error invokes registered handler
    const authHandler = vi.fn();
    useLayoutStore.getState().setAuthErrorHandler(authHandler);
    updateLayoutMock.mockRejectedValueOnce(new ApiError(403, "Invalid CSRF token"));
    const authFailed = await useLayoutStore.getState().saveActiveLayout();
    expect(authFailed).toBeNull();
    expect(authHandler).toHaveBeenCalledWith(403, "Invalid CSRF token");
  });

  it("guards concurrent save calls while saving is already in flight", async () => {
    /**
     * Contract: When a save mutation is in flight (saving=true), concurrent save/rename/delete
     * calls return immediately without issuing duplicate API requests.
     * Defect caught: Double-click racing updateLayout/createLayout requests.
     */
    const base = makeFourPaneLayout();
    let resolveUpdate!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveUpdate = resolve;
        }),
    );

    useLayoutStore.getState().hydrateLayouts([base]);
    const first = useLayoutStore.getState().saveActiveLayout();
    expect(useLayoutStore.getState().saving).toBe(true);
    const second = await useLayoutStore.getState().saveActiveLayout();
    expect(second).toBeNull();
    expect(updateLayoutMock).toHaveBeenCalledTimes(1);

    resolveUpdate(base);
    await first;
    expect(useLayoutStore.getState().saving).toBe(false);
  });

  it("aborts pending Save/SaveAs on reset() and ignores late 401 after hydrateLayouts() (P1-001, P2-001)", async () => {
    /**
     * Contract: reset() aborts active CRUD AbortController and bumps sessionEpoch; after
     * hydrateLayouts() starts a new session, a late 401 rejection from the old session's
     * Save or SaveAs neither mutates new session state nor calls onAuthError.
     * Defect caught: Stale session 401 from in-flight save logging out a newly hydrated session.
     */
    const oldLayout = makeFourPaneLayout({ id: "old-layout", name: "Old" });
    const newLayout = makeFourPaneLayout({ id: "new-layout", name: "New Session" });
    const authHandler = vi.fn();

    let rejectOldSave!: (reason?: unknown) => void;
    let capturedSaveSignal: AbortSignal | undefined;
    updateLayoutMock.mockImplementationOnce((_id, _payload, signal) => {
      capturedSaveSignal = signal;
      return new Promise<LayoutRecord>((_resolve, reject) => {
        rejectOldSave = reject;
      });
    });

    useLayoutStore.getState().setAuthErrorHandler(authHandler);
    useLayoutStore.getState().hydrateLayouts([oldLayout]);
    useLayoutStore.getState().setPaneSymbol("pane-1", "MGC");

    const pendingSave = useLayoutStore.getState().saveActiveLayout();
    expect(useLayoutStore.getState().saving).toBe(true);
    expect(capturedSaveSignal?.aborted).toBe(false);

    useLayoutStore.getState().reset();
    expect(capturedSaveSignal?.aborted).toBe(true);
    expect(useLayoutStore.getState().saving).toBe(false);

    useLayoutStore.getState().hydrateLayouts([newLayout]);
    expect(useLayoutStore.getState().activeLayout?.id).toBe("new-layout");

    rejectOldSave(new ApiError(401, "Old session expired"));
    const saveResult = await pendingSave;
    expect(saveResult).toBeNull();
    expect(authHandler).not.toHaveBeenCalled();
    expect(useLayoutStore.getState().activeLayout?.id).toBe("new-layout");
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().saving).toBe(false);
    expect(useLayoutStore.getState().error).toBeNull();

    // Also verify SaveAs across reset() + hydrateLayouts() + late 401
    let rejectOldSaveAs!: (reason?: unknown) => void;
    let capturedCreateSignal: AbortSignal | undefined;
    createLayoutMock.mockImplementationOnce((_payload, signal) => {
      capturedCreateSignal = signal;
      return new Promise<LayoutRecord>((_resolve, reject) => {
        rejectOldSaveAs = reject;
      });
    });

    const pendingSaveAs = useLayoutStore.getState().saveLayoutAs("Should Abort");
    expect(capturedCreateSignal?.aborted).toBe(false);

    useLayoutStore.getState().reset();
    expect(capturedCreateSignal?.aborted).toBe(true);
    useLayoutStore.getState().hydrateLayouts([newLayout]);

    rejectOldSaveAs(new ApiError(401, "Old session expired"));
    expect(await pendingSaveAs).toBeNull();
    expect(authHandler).not.toHaveBeenCalled();
    expect(useLayoutStore.getState().layouts).toHaveLength(1);
    expect(useLayoutStore.getState().activeLayout?.id).toBe("new-layout");
    expect(useLayoutStore.getState().saving).toBe(false);
  });

  it("guards activeLayout and dirty when draft is edited or another layout is selected during pending Save/Rename/SetDefault (P1-002, P2-002)", async () => {
    /**
     * Contract: If the user edits the draft or switches to another layout while Save, Rename,
     * or SetDefault is in flight, the resolved record updates layouts[] without clobbering
     * the newer activeLayout draft or clearing dirty; Rename/SetDefault on the same layout
     * merge only name/is_default metadata.
     * Defect caught: In-flight save overwriting newer edits or switching activeLayout back.
     */
    const layoutA = makeFourPaneLayout({ id: "layout-a", name: "Layout A", is_default: false });
    const layoutB = makeFourPaneLayout({
      id: "layout-b",
      name: "Layout B",
      template_id: "2-cols",
      panes: [
        { pane_id: "pane-1", root_symbol: "MGC", timeframe: "15m", live_mode: false, playback_mode: "live" },
        { pane_id: "pane-2", root_symbol: "MYM", timeframe: "15m", live_mode: false, playback_mode: "live" },
      ],
      is_default: true,
    });

    useLayoutStore.getState().hydrateLayouts([layoutA, layoutB]);
    useLayoutStore.getState().selectLayout("layout-a");
    useLayoutStore.getState().setPaneSymbol("pane-1", "MNQ");

    // 1. Edit draft while saveActiveLayout is in flight -> newer edit & dirty preserved
    let resolveSaveA!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveSaveA = resolve;
        }),
    );
    const savePromise = useLayoutStore.getState().saveActiveLayout();
    useLayoutStore.getState().setPaneSymbol("pane-2", "MGC");

    const savedSnapshotA: LayoutRecord = {
      ...layoutA,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MNQ" } : p)),
    };
    resolveSaveA(savedSnapshotA);
    await savePromise;

    expect(useLayoutStore.getState().layouts.find((l) => l.id === "layout-a")?.panes[0]?.root_symbol).toBe("MNQ");
    expect(useLayoutStore.getState().activeLayout?.panes[1]?.root_symbol).toBe("MGC");
    expect(useLayoutStore.getState().dirty).toBe(true);
    expect(useLayoutStore.getState().saving).toBe(false);

    // 2. Switch to Layout B while saveActiveLayout on A is in flight -> B stays active
    let resolveSaveA2!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveSaveA2 = resolve;
        }),
    );
    const savePromise2 = useLayoutStore.getState().saveActiveLayout();
    useLayoutStore.getState().selectLayout("layout-b");
    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-b");

    resolveSaveA2({ ...savedSnapshotA, name: "Saved A2" });
    await savePromise2;

    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-b");
    expect(useLayoutStore.getState().activeLayout?.panes).toHaveLength(2);
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().layouts.find((l) => l.id === "layout-a")?.name).toBe("Saved A2");

    // 3. Rename & SetDefault on same layout A with newer edits -> merge only metadata
    useLayoutStore.getState().selectLayout("layout-a");
    let resolveRename!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveRename = resolve;
        }),
    );
    const renamePromise = useLayoutStore.getState().renameActiveLayout("Renamed A");
    useLayoutStore.getState().setPaneSymbol("pane-3", "MES");
    resolveRename({ ...savedSnapshotA, name: "Renamed A" });
    await renamePromise;

    expect(useLayoutStore.getState().activeLayout?.name).toBe("Renamed A");
    expect(useLayoutStore.getState().activeLayout?.panes[2]?.root_symbol).toBe("MES");
    expect(useLayoutStore.getState().dirty).toBe(true);

    let resolveDefault!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveDefault = resolve;
        }),
    );
    const defaultPromise = useLayoutStore.getState().setActiveLayoutDefault();
    useLayoutStore.getState().setPaneSymbol("pane-4", "MNQ");
    resolveDefault({ ...savedSnapshotA, name: "Renamed A", is_default: true });
    await defaultPromise;

    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(useLayoutStore.getState().activeLayout?.panes[3]?.root_symbol).toBe("MNQ");
    expect(useLayoutStore.getState().dirty).toBe(true);
  });

  it("preserves activeLayout dirty draft when deleting an inactive layout, even when inactive was default (P1-003)", async () => {
    /**
     * Contract: Deleting layoutId !== activeLayout.id never replaces activeLayout with the saved
     * record from layouts/remaining; it preserves activeLayout's unsaved template/pane/sync edits,
     * activePaneId, and dirty=true while syncing is_default if promoted.
     * Defect caught: deleteLayoutById overwriting dirty activeLayout with stale saved record.
     */
    const layoutA = makeFourPaneLayout({ id: "layout-a", name: "Alpha", is_default: false });
    const layoutB = makeFourPaneLayout({ id: "layout-b", name: "Beta Default", is_default: true });

    useLayoutStore.getState().hydrateLayouts([layoutA, layoutB]);
    useLayoutStore.getState().selectLayout("layout-a");
    useLayoutStore.getState().selectPane("pane-2");
    useLayoutStore.getState().setPaneSymbol("pane-2", "MGC");
    useLayoutStore.getState().toggleSync("crosshair");
    useLayoutStore.getState().setTemplate("2-cols");
    expect(useLayoutStore.getState().dirty).toBe(true);

    deleteLayoutMock.mockResolvedValueOnce({ deleted: true, id: "layout-b" });
    listLayoutsMock.mockResolvedValueOnce([{ ...layoutA, is_default: true }]);

    const ok = await useLayoutStore.getState().deleteLayoutById("layout-b");
    expect(ok).toBe(true);

    const active = useLayoutStore.getState().activeLayout!;
    expect(active.id).toBe("layout-a");
    expect(active.is_default).toBe(true);
    expect(active.template_id).toBe("2-cols");
    expect(active.panes).toHaveLength(2);
    expect(active.panes[1]?.root_symbol).toBe("MGC");
    expect(active.sync_config.crosshair).toBe(false);
    expect(useLayoutStore.getState().activePaneId).toBe("pane-2");
    expect(useLayoutStore.getState().dirty).toBe(true);
  });

  it("restores custom pane_ids (alpha/beta/gamma/delta) by slot index across 4 -> 2 -> 4 and generates pane-N only for unarchived slots (P1-005)", () => {
    /**
     * Contract: Shrinking a layout with custom pane_ids ["alpha", "beta", "gamma", "delta"]
     * from 4 -> 2 and re-expanding 2 -> 4 restores "gamma" and "delta" in slots 2 and 3 with
     * their exact pane_id, root_symbol, timeframe, live_mode, and playback_mode; expanding to 5
     * creates pane-5 without colliding, and respects sync_config.symbol/interval when enabled.
     * Defect caught: Re-expansion synthesizing pane-3/pane-4 instead of restoring custom pane_ids.
     */
    const customLayout = makeFourPaneLayout({
      id: "layout-custom",
      name: "Custom IDs",
      template_id: "4-grid-2x2",
      panes: [
        { pane_id: "alpha", root_symbol: "MES", timeframe: "5m", live_mode: false, playback_mode: "live" },
        { pane_id: "beta", root_symbol: "MNQ", timeframe: "15m", live_mode: true, playback_mode: "live" },
        { pane_id: "gamma", root_symbol: "MYM", timeframe: "1h", live_mode: true, playback_mode: "replay" },
        { pane_id: "delta", root_symbol: "MGC", timeframe: "4h", live_mode: false, playback_mode: "replay" },
      ],
      sync_config: {
        symbol: false,
        interval: false,
        crosshair: false,
        time: false,
        dateRange: false,
      },
    });

    useLayoutStore.getState().hydrateLayouts([customLayout]);
    useLayoutStore.getState().setTemplate("2-cols");
    expect(useLayoutStore.getState().activeLayout?.panes.map((p) => p.pane_id)).toEqual([
      "alpha",
      "beta",
    ]);

    useLayoutStore.getState().setTemplate("4-grid-2x2");
    let restored = useLayoutStore.getState().activeLayout!.panes;
    expect(restored[2]).toEqual({
      pane_id: "gamma",
      root_symbol: "MYM",
      timeframe: "1h",
      live_mode: true,
      playback_mode: "replay",
    });
    expect(restored[3]).toEqual({
      pane_id: "delta",
      root_symbol: "MGC",
      timeframe: "4h",
      live_mode: false,
      playback_mode: "replay",
    });

    // Expanding 4 -> 5 creates pane-5 because slot 4 has no archived pane
    useLayoutStore.getState().setTemplate("5-top2-bot3");
    restored = useLayoutStore.getState().activeLayout!.panes;
    expect(restored[4]?.pane_id).toBe("pane-5");

    // Shrinking 5 -> 2, enabling symbol+interval sync, editing alpha, and expanding 2 -> 4
    // still restores gamma/delta IDs and modes while syncing symbol & timeframe from reference pane
    useLayoutStore.getState().setTemplate("2-cols");
    useLayoutStore.getState().toggleSync("symbol");
    useLayoutStore.getState().toggleSync("interval");
    useLayoutStore.getState().selectPane("alpha");
    useLayoutStore.getState().setPaneSymbol("alpha", "MGC");
    useLayoutStore.getState().setPaneTimeframe("alpha", "30m");
    useLayoutStore.getState().setTemplate("4-grid-2x2");

    restored = useLayoutStore.getState().activeLayout!.panes;
    expect(restored[2]).toEqual({
      pane_id: "gamma",
      root_symbol: "MGC",
      timeframe: "30m",
      live_mode: true,
      playback_mode: "replay",
    });
    expect(restored[3]).toEqual({
      pane_id: "delta",
      root_symbol: "MGC",
      timeframe: "30m",
      live_mode: false,
      playback_mode: "replay",
    });
  });

  it("sends exact payloads and AbortSignal for saveActiveLayout, renameActiveLayout, and setActiveLayoutDefault", async () => {
    /**
     * Contract: saveActiveLayout, renameActiveLayout, and setActiveLayoutDefault invoke
     * updateLayout with exact layoutId, LayoutPayload fields, and AbortSignal, clearing dirty.
     * Defect caught: Missing or malformed payload fields or omitted AbortSignal on CRUD updates.
     */
    const initial = makeFourPaneLayout({ id: "layout-1", name: "Main", is_default: false });
    useLayoutStore.getState().hydrateLayouts([initial]);
    useLayoutStore.getState().setPaneSymbol("pane-1", "MGC");

    const savedAfterSave: LayoutRecord = {
      ...initial,
      panes: initial.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MGC" } : p)),
    };
    updateLayoutMock.mockResolvedValueOnce(savedAfterSave);

    const saveRes = await useLayoutStore.getState().saveActiveLayout();
    expect(saveRes?.panes[0]?.root_symbol).toBe("MGC");
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(updateLayoutMock).toHaveBeenNthCalledWith(
      1,
      "layout-1",
      {
        name: "Main",
        template_id: "4-grid-2x2",
        panes: savedAfterSave.panes,
        sync_config: initial.sync_config,
        is_default: false,
      },
      expect.any(AbortSignal),
    );

    const savedAfterDefault: LayoutRecord = { ...savedAfterSave, is_default: true };
    updateLayoutMock.mockResolvedValueOnce(savedAfterDefault);
    const defaultRes = await useLayoutStore.getState().setActiveLayoutDefault();
    expect(defaultRes?.is_default).toBe(true);
    expect(updateLayoutMock).toHaveBeenNthCalledWith(
      2,
      "layout-1",
      {
        name: "Main",
        template_id: "4-grid-2x2",
        panes: savedAfterSave.panes,
        sync_config: initial.sync_config,
        is_default: true,
      },
      expect.any(AbortSignal),
    );

    const savedAfterRename: LayoutRecord = { ...savedAfterDefault, name: "Main Pro" };
    updateLayoutMock.mockResolvedValueOnce(savedAfterRename);
    const renameRes = await useLayoutStore.getState().renameActiveLayout("  Main Pro ");
    expect(renameRes?.name).toBe("Main Pro");
    expect(updateLayoutMock).toHaveBeenNthCalledWith(
      3,
      "layout-1",
      {
        name: "Main Pro",
        template_id: "4-grid-2x2",
        panes: savedAfterSave.panes,
        sync_config: initial.sync_config,
        is_default: true,
      },
      expect.any(AbortSignal),
    );
  });

  it("keeps dirty === false and draftRevision unchanged when setTemplate is called with already active template (P1-002)", () => {
    /**
     * Contract: Calling setTemplate(activeLayout.template_id) when activeLayout already uses
     * that template with matching pane count is a no-op that preserves dirty === false and
     * draftRevision.
     * Defect caught: Re-selecting the active template marking a clean layout dirty or bumping draftRevision.
     */
    const base = makeFourPaneLayout();
    useLayoutStore.getState().hydrateLayouts([base]);
    const initialRevision = useLayoutStore.getState().draftRevision;
    expect(useLayoutStore.getState().dirty).toBe(false);

    useLayoutStore.getState().setTemplate(base.template_id);
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().draftRevision).toBe(initialRevision);
    expect(useLayoutStore.getState().activeLayout?.template_id).toBe(base.template_id);
    expect(useLayoutStore.getState().activeLayout?.panes).toHaveLength(4);
  });

  it("syncs clean activeLayout when re-selecting the same layout while Save/Rename/SetDefault is in flight (P1-001)", async () => {
    /**
     * Contract: Editing A (MES -> MNQ), starting saveActiveLayout(), and calling selectLayout(A)
     * (directly or via A -> B -> A) while Save is pending updates activeLayout to the saved MNQ
     * record with dirty === false once Save resolves, instead of leaving stale MES with dirty === false.
     * Defect caught: draftRevision mismatch blocking activeLayout sync on clean re-selected layout.
     */
    const layoutA = makeFourPaneLayout({ id: "layout-a", name: "Layout A", is_default: false });
    const layoutB = makeFourPaneLayout({
      id: "layout-b",
      name: "Layout B",
      is_default: true,
      panes: [
        { pane_id: "pane-1", root_symbol: "MGC", timeframe: "15m", live_mode: false, playback_mode: "live" },
      ],
      template_id: "1-single",
    });

    useLayoutStore.getState().hydrateLayouts([layoutA, layoutB]);

    // Case 1: selectLayout(A) directly while saveActiveLayout is pending
    useLayoutStore.getState().selectLayout("layout-a");
    useLayoutStore.getState().setPaneSymbol("pane-1", "MNQ");
    expect(useLayoutStore.getState().dirty).toBe(true);

    let resolveSaveDirect!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveSaveDirect = resolve;
        }),
    );
    const saveDirectPromise = useLayoutStore.getState().saveActiveLayout();
    useLayoutStore.getState().selectLayout("layout-a");
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MES");

    const savedMnq: LayoutRecord = {
      ...layoutA,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MNQ" } : p)),
    };
    resolveSaveDirect(savedMnq);
    await saveDirectPromise;

    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-a");
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MNQ");
    expect(useLayoutStore.getState().dirty).toBe(false);

    // Case 2: A -> B -> A while saveActiveLayout is pending
    useLayoutStore.getState().setPaneSymbol("pane-1", "MYM");
    let resolveSaveRoundTrip!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveSaveRoundTrip = resolve;
        }),
    );
    const saveRoundTripPromise = useLayoutStore.getState().saveActiveLayout();
    useLayoutStore.getState().selectLayout("layout-b");
    useLayoutStore.getState().selectLayout("layout-a");
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MNQ");

    const savedMym: LayoutRecord = {
      ...layoutA,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MYM" } : p)),
    };
    resolveSaveRoundTrip(savedMym);
    await saveRoundTripPromise;

    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-a");
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MYM");
    expect(useLayoutStore.getState().dirty).toBe(false);

    // Case 3: renameActiveLayout and setActiveLayoutDefault also sync clean re-selected layout
    useLayoutStore.getState().setPaneSymbol("pane-1", "MGC");
    let resolveRename!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveRename = resolve;
        }),
    );
    const renamePromise = useLayoutStore.getState().renameActiveLayout("Layout A Renamed");
    useLayoutStore.getState().selectLayout("layout-a");
    const savedRenamed: LayoutRecord = {
      ...layoutA,
      name: "Layout A Renamed",
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MGC" } : p)),
    };
    resolveRename(savedRenamed);
    await renamePromise;

    expect(useLayoutStore.getState().activeLayout?.name).toBe("Layout A Renamed");
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MGC");
    expect(useLayoutStore.getState().dirty).toBe(false);

    useLayoutStore.getState().setPaneSymbol("pane-1", "MES");
    let resolveDefault!: (val: LayoutRecord) => void;
    updateLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveDefault = resolve;
        }),
    );
    const defaultPromise = useLayoutStore.getState().setActiveLayoutDefault();
    useLayoutStore.getState().selectLayout("layout-a");
    const savedDefault: LayoutRecord = {
      ...savedRenamed,
      is_default: true,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MES" } : p)),
    };
    resolveDefault(savedDefault);
    await defaultPromise;

    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(useLayoutStore.getState().activeLayout?.panes[0]?.root_symbol).toBe("MES");
    expect(useLayoutStore.getState().dirty).toBe(false);
  });

  it("promotes oldest remaining layout by (created_at_utc ASC, id ASC) on delete fallback when listLayouts fails (P1-002)", async () => {
    /**
     * Contract: When deleting an is_default layout and listLayouts() rejects with a non-auth error,
     * deleteLayoutById promotes the oldest remaining layout ordered by (created_at_utc ASC, id ASC)
     * matching sqlite_store.py rather than remaining[0].
     * Defect caught: Fallback promoting remaining[0] regardless of created_at_utc/id.
     */
    const layoutA = makeFourPaneLayout({
      id: "layout-a-newest",
      name: "A Newest",
      is_default: true,
      created_at_utc: 3000,
    });
    const layoutB = makeFourPaneLayout({
      id: "layout-b-oldest",
      name: "B Oldest",
      is_default: false,
      created_at_utc: 1000,
    });
    const layoutC = makeFourPaneLayout({
      id: "layout-c-middle",
      name: "C Middle",
      is_default: false,
      created_at_utc: 2000,
    });

    useLayoutStore.getState().hydrateLayouts([layoutA, layoutB, layoutC]);
    useLayoutStore.getState().selectLayout(layoutB.id);
    updateLayoutMock.mockResolvedValueOnce({ ...layoutB, is_default: true });
    await useLayoutStore.getState().setActiveLayoutDefault();

    deleteLayoutMock.mockResolvedValueOnce({ deleted: true, id: layoutB.id });
    listLayoutsMock.mockRejectedValueOnce(new TypeError("Network error"));

    const ok = await useLayoutStore.getState().deleteLayoutById(layoutB.id);
    expect(ok).toBe(true);
    expect(useLayoutStore.getState().activeLayout?.id).toBe(layoutC.id);
    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(useLayoutStore.getState().layouts.find((l) => l.id === layoutC.id)?.is_default).toBe(
      true,
    );
    expect(useLayoutStore.getState().layouts.find((l) => l.id === layoutA.id)?.is_default).toBe(
      false,
    );

    // Also verify id ASC tie-breaker when created_at_utc is equal
    const layoutZ = makeFourPaneLayout({
      id: "layout-zeta",
      name: "Zeta",
      is_default: false,
      created_at_utc: 2000,
    });
    const layoutAlpha = makeFourPaneLayout({
      id: "layout-alpha",
      name: "Alpha",
      is_default: false,
      created_at_utc: 2000,
    });
    useLayoutStore.getState().hydrateLayouts([layoutZ, layoutC, layoutAlpha]);
    useLayoutStore.getState().selectLayout(layoutC.id);
    updateLayoutMock.mockResolvedValueOnce({ ...layoutC, is_default: true });
    await useLayoutStore.getState().setActiveLayoutDefault();

    deleteLayoutMock.mockResolvedValueOnce({ deleted: true, id: layoutC.id });
    listLayoutsMock.mockRejectedValueOnce(new TypeError("Network error"));
    const okTie = await useLayoutStore.getState().deleteLayoutById(layoutC.id);
    expect(okTie).toBe(true);
    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-alpha");
    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(
      useLayoutStore.getState().layouts.find((l) => l.id === "layout-zeta")?.is_default,
    ).toBe(false);
  });

  it("switches to created layout on saveLayoutAs after clean re-select, but preserves newer draft edit on dirty re-select (P1-001)", async () => {
    /**
     * Contract:
     * (a) Editing A, starting saveLayoutAs("Layout B"), calling selectLayout(A.id) cleanly
     *     (dirty === false), and resolving createLayout with B switches activeLayout to B with
     *     dirty === false.
     * (b) Editing A, starting saveLayoutAs("Layout B"), calling selectLayout(A.id) and making a
     *     new edit on A (dirty === true), and resolving createLayout with B keeps activeLayout on
     *     A with dirty === true and the new draft edit preserved while appending B to layouts.
     * Defect caught: saveLayoutAs requiring draftRevision === targetDraftRevision even when
     * activeLayout was cleanly re-selected (dirty === false).
     */
    const layoutA = makeFourPaneLayout({ id: "layout-a", name: "Layout A", is_default: true });
    const recordB: LayoutRecord = {
      ...layoutA,
      id: "layout-b",
      name: "Layout B",
      is_default: false,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MNQ" } : p)),
    };

    // (a) Clean re-select of A while saveLayoutAs("Layout B") is pending -> activates B
    useLayoutStore.getState().hydrateLayouts([layoutA]);
    useLayoutStore.getState().setPaneSymbol("pane-1", "MNQ");
    expect(useLayoutStore.getState().dirty).toBe(true);

    let resolveCreateA!: (val: LayoutRecord) => void;
    createLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveCreateA = resolve;
        }),
    );
    const saveAsPromiseA = useLayoutStore.getState().saveLayoutAs("Layout B");
    useLayoutStore.getState().selectLayout("layout-a");
    expect(useLayoutStore.getState().dirty).toBe(false);

    resolveCreateA(recordB);
    const createdA = await saveAsPromiseA;
    expect(createdA?.id).toBe("layout-b");
    expect(useLayoutStore.getState().activeLayout?.id).toBe("layout-b");
    expect(useLayoutStore.getState().dirty).toBe(false);
    expect(useLayoutStore.getState().layouts.some((l) => l.id === "layout-b")).toBe(true);

    // (b) Re-select A and edit A while saveLayoutAs("Layout B 2") is pending -> preserves draft A
    useLayoutStore.getState().hydrateLayouts([layoutA]);
    useLayoutStore.getState().setPaneSymbol("pane-1", "MYM");
    expect(useLayoutStore.getState().dirty).toBe(true);

    let resolveCreateB!: (val: LayoutRecord) => void;
    createLayoutMock.mockImplementationOnce(
      () =>
        new Promise<LayoutRecord>((resolve) => {
          resolveCreateB = resolve;
        }),
    );
    const saveAsPromiseB = useLayoutStore.getState().saveLayoutAs("Layout B 2");
    useLayoutStore.getState().selectLayout("layout-a");
    useLayoutStore.getState().setPaneSymbol("pane-1", "MGC");
    useLayoutStore.getState().setPaneTimeframe("pane-1", "15m");
    expect(useLayoutStore.getState().dirty).toBe(true);

    const recordB2: LayoutRecord = {
      ...layoutA,
      id: "layout-b-2",
      name: "Layout B 2",
      is_default: false,
      panes: layoutA.panes.map((p) => (p.pane_id === "pane-1" ? { ...p, root_symbol: "MYM" } : p)),
    };
    resolveCreateB(recordB2);
    const createdB = await saveAsPromiseB;
    expect(createdB?.id).toBe("layout-b-2");

    const state = useLayoutStore.getState();
    expect(state.activeLayout?.id).toBe("layout-a");
    expect(state.dirty).toBe(true);
    expect(state.activeLayout?.panes[0]?.root_symbol).toBe("MGC");
    expect(state.activeLayout?.panes[0]?.timeframe).toBe("15m");
    expect(state.layouts.some((l) => l.id === "layout-b-2")).toBe(true);
  });
});
