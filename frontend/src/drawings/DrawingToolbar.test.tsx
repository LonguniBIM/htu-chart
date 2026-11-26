// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DrawingRecord } from "../api/types";
import { buildDrawingScopeKey, useDrawingStore } from "../store/drawingStore";
import { DrawingToolbar } from "./DrawingToolbar";

const fetchMock = vi.fn();

describe("DrawingToolbar (Phase 4 Part 2)", () => {
  const scope = { symbol: "MES" as const, contractId: "CON.F.US.MES.H26", paneId: "pane-1" };
  const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
    useDrawingStore.getState().setAuthErrorHandler(null);
    useDrawingStore.getState().reset();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useDrawingStore.getState().reset();
  });

  it("renders all 9 drawing tools, toggles Magnet/OB Extend Right/FVG Preset, and enables Undo/Redo/Delete/Clear from scope state", () => {
    /**
     * Contract: DrawingToolbar exposes accessible aria-label and aria-pressed controls for all
     * 9 tools plus Magnet Mode, FVG Preset, OB Extend Right, Undo, Redo, Delete Selected, and Clear Pane.
     * Defect caught: Missing tool buttons, broken aria-pressed state, or enabled Delete/Undo when empty.
     */
    const sampleDrawing: DrawingRecord = {
      id: "d-1",
      revision: 1,
      type: "hline",
      symbol: "MES",
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user",
      source_bar_refs: [],
      coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5000 }] },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };

    useDrawingStore.setState({
      scopes: {
        [scopeKey]: {
          symbol: "MES",
          contractId: "CON.F.US.MES.H26",
          paneId: "pane-1",
          initialized: true,
          drawings: [sampleDrawing],
          scopeRevision: 2,
          canUndo: true,
          canRedo: false,
          loading: false,
          mutating: false,
          error: null,
        },
      },
      selectedByScope: { [scopeKey]: "d-1" },
    });

    render(<DrawingToolbar activeScope={scope} />);

    const toolLabels = [
      "Cursor tool",
      "Trendline tool",
      "Ray tool",
      "Horizontal line tool",
      "FVG box tool",
      "Order block tool",
      "Long position tool",
      "Short position tool",
      "Measure tool",
    ];
    for (const label of toolLabels) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }

    const cursorBtn = screen.getByRole("button", { name: "Cursor tool" });
    const fvgBtn = screen.getByRole("button", { name: "FVG box tool" });
    expect(cursorBtn.getAttribute("aria-pressed")).toBe("true");
    expect(fvgBtn.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(fvgBtn);
    expect(useDrawingStore.getState().activeTool).toBe("fvg_box");
    expect(fvgBtn.getAttribute("aria-pressed")).toBe("true");
    expect(cursorBtn.getAttribute("aria-pressed")).toBe("false");

    const magnetBtn = screen.getByRole("button", { name: "Magnet Mode" });
    expect(magnetBtn.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(magnetBtn);
    expect(useDrawingStore.getState().magnetEnabled).toBe(true);
    expect(magnetBtn.getAttribute("aria-pressed")).toBe("true");

    const obExtBtn = screen.getByRole("button", { name: "OB Extend Right" });
    expect(obExtBtn.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(obExtBtn);
    expect(useDrawingStore.getState().obExtendRight).toBe(false);

    const presetSelect = screen.getByLabelText("FVG Preset") as HTMLSelectElement;
    fireEvent.change(presetSelect, { target: { value: "bullish" } });
    expect(useDrawingStore.getState().fvgPreset).toBe("bullish");

    expect((screen.getByRole("button", { name: "Undo drawing" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Redo drawing" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Delete Selected" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Clear Pane" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
