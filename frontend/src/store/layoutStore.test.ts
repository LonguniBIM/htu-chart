import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LayoutPayload, LayoutRecord } from "../api/types";
import { useLayoutStore } from "./layoutStore";

const createLayoutMock = vi.fn<(payload: LayoutPayload) => Promise<LayoutRecord>>();
const updateLayoutMock = vi.fn<
  (layoutId: string, payload: LayoutPayload) => Promise<LayoutRecord>
>();
const deleteLayoutMock = vi.fn<(layoutId: string) => Promise<{ deleted: boolean; id: string }>>();
const listLayoutsMock = vi.fn<() => Promise<LayoutRecord[]>>();

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    createLayout: (payload: LayoutPayload) => createLayoutMock(payload),
    updateLayout: (layoutId: string, payload: LayoutPayload) =>
      updateLayoutMock(layoutId, payload),
    deleteLayout: (layoutId: string) => deleteLayoutMock(layoutId),
    listLayouts: () => listLayoutsMock(),
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
      expect.objectContaining({ name: "Scalp MNQ", is_default: true }),
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
});
