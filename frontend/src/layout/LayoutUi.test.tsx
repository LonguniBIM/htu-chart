// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { LayoutPayload, LayoutRecord } from "../api/types";
import { useLayoutStore } from "../store/layoutStore";
import { LayoutPicker } from "./LayoutPicker";
import { SyncControls } from "./SyncControls";

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

function makeDefaultLayout(): LayoutRecord {
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
  };
}

describe("LayoutPicker and SyncControls UI (Phase 3 Part 2)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useLayoutStore.getState().setAuthErrorHandler(null);
    useLayoutStore.getState().reset();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders all 21 1-8 pane templates, selects templates with dirty indicator, and toggles all 5 sync switches", () => {
    /**
     * Contract: LayoutPicker lists all 21 canonical templates with SVG preview rects and
     * aria-pressed state, marks dirty (*) on template switch, and SyncControls toggles all
     * 5 SYNC IN LAYOUT switches independently.
     * Defect caught: Missing template buttons in dropdown or non-functional sync switches.
     */
    useLayoutStore.getState().hydrateLayouts([makeDefaultLayout()]);

    render(
      <div>
        <LayoutPicker />
        <SyncControls />
      </div>,
    );

    const trigger = screen.getByRole("button", { name: "Layout picker" });
    expect(trigger.textContent).toContain("4 Future");
    expect(trigger.textContent).not.toContain("*");

    fireEvent.click(trigger);
    const tplButtons = document.querySelectorAll("[data-template-id]");
    expect(tplButtons).toHaveLength(21);
    expect(
      screen.getByRole("button", { name: "4 Grid (2x2)" }).getAttribute("aria-pressed"),
    ).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "8 Grid (4x2)" }));
    expect(useLayoutStore.getState().activeLayout?.template_id).toBe("8-grid-4x2");
    expect(useLayoutStore.getState().activeLayout?.panes).toHaveLength(8);
    expect(trigger.textContent).toContain("4 Future *");

    // Toggle all 5 SYNC IN LAYOUT switches
    const switches = [
      { name: "Sync Symbol", key: "symbol", initial: false },
      { name: "Sync Interval", key: "interval", initial: true },
      { name: "Sync Crosshair", key: "crosshair", initial: true },
      { name: "Sync Time", key: "time", initial: false },
      { name: "Sync Date range", key: "dateRange", initial: false },
    ] as const;

    for (const item of switches) {
      const btn = screen.getByRole("switch", { name: item.name });
      expect(btn.getAttribute("aria-checked")).toBe(String(item.initial));
      fireEvent.click(btn);
      expect(btn.getAttribute("aria-checked")).toBe(String(!item.initial));
      expect(useLayoutStore.getState().activeLayout?.sync_config[item.key]).toBe(!item.initial);
    }
  });

  it("supports Save As, Rename, Set Default, Delete, and disables Delete on final remaining layout", async () => {
    /**
     * Contract: LayoutPicker disables Delete when only 1 layout remains, enables Save As /
     * Rename / Set Default / Delete when multiple layouts exist, and displays 409 errors inline.
     * Defect caught: Allowing delete of last layout or hiding persistence error feedback.
     */
    const base = makeDefaultLayout();
    const scalp: LayoutRecord = {
      ...base,
      id: "layout-scalp",
      name: "Scalp",
      is_default: false,
    };
    const scalpRenamed: LayoutRecord = {
      ...scalp,
      name: "Scalp V2",
    };

    createLayoutMock.mockResolvedValueOnce(scalp);
    deleteLayoutMock
      .mockRejectedValueOnce(new ApiError(409, "Cannot delete the last remaining layout"))
      .mockResolvedValueOnce({ deleted: true, id: "layout-4-future" });

    useLayoutStore.getState().hydrateLayouts([base]);
    render(<LayoutPicker />);
    fireEvent.click(screen.getByRole("button", { name: "Layout picker" }));

    // Only 1 layout -> Delete button is disabled, Save is disabled while clean
    const deleteBaseBtn = screen.getByRole("button", { name: "Delete 4 Future" }) as HTMLButtonElement;
    expect(deleteBaseBtn.disabled).toBe(true);
    const saveBtn = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);

    // Editing template marks dirty -> Save enables, and Save disables again after resolving
    fireEvent.click(screen.getByRole("button", { name: "2 Columns" }));
    expect(saveBtn.disabled).toBe(false);
    const savedBase: LayoutRecord = {
      ...base,
      template_id: "2-cols",
      panes: base.panes.slice(0, 2),
    };
    updateLayoutMock.mockResolvedValueOnce(savedBase);
    await act(async () => {
      fireEvent.click(saveBtn);
    });
    expect(updateLayoutMock).toHaveBeenCalledTimes(1);
    expect(saveBtn.disabled).toBe(true);

    // Save As "Scalp" (non-default)
    fireEvent.change(screen.getByLabelText("Save layout as"), {
      target: { value: "Scalp" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save As" }));
    });
    expect(createLayoutMock).toHaveBeenCalledTimes(1);
    expect(useLayoutStore.getState().activeLayout?.name).toBe("Scalp");
    expect(saveBtn.disabled).toBe(true);

    // Rename "Scalp" -> "Scalp V2"
    updateLayoutMock.mockResolvedValueOnce(scalpRenamed);
    fireEvent.change(screen.getByLabelText("Rename layout"), {
      target: { value: "Scalp V2" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    });
    expect(useLayoutStore.getState().activeLayout?.name).toBe("Scalp V2");

    // Set Default on active non-default layout "Scalp V2"
    const scalpDefault: LayoutRecord = { ...scalpRenamed, is_default: true };
    updateLayoutMock.mockResolvedValueOnce(scalpDefault);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Set Default" }));
    });
    expect(updateLayoutMock).toHaveBeenLastCalledWith(
      "layout-scalp",
      expect.objectContaining({ name: "Scalp V2", is_default: true }),
      expect.any(AbortSignal),
    );
    expect(useLayoutStore.getState().activeLayout?.is_default).toBe(true);
    expect(screen.getByText("Default", { selector: ".terminal-badge" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set Default" })).toBeNull();

    // Delete 409 surfaces inline alert, then successful Delete removes "4 Future"
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete 4 Future" }));
    });
    expect(screen.getByRole("alert").textContent).toContain("409");

    deleteLayoutMock.mockResolvedValueOnce({ deleted: true, id: "layout-4-future" });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete 4 Future" }));
    });
    expect(screen.queryByRole("button", { name: "Delete 4 Future" })).toBeNull();
    expect(useLayoutStore.getState().layouts).toHaveLength(1);
  });
});
