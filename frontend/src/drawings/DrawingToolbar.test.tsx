// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DrawingRecord } from "../api/types";
import { buildDrawingScopeKey, useDrawingStore } from "../store/drawingStore";
import { DrawingToolbar } from "./DrawingToolbar";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DrawingToolbar (Phase 4 Part 2)", () => {
  const scope = { symbol: "MES" as const, contractId: "CON.F.US.MES.H26", paneId: "pane-1" };
  const scopeKey = buildDrawingScopeKey("MES", "CON.F.US.MES.H26", "pane-1");

  beforeEach(() => {
    useDrawingStore.getState().setAuthErrorHandler(null);
    useDrawingStore.getState().reset();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useDrawingStore.getState().reset();
  });

  it("renders all 9 drawing tools, toggles options, and triggers Undo/Redo/Delete/Clear store actions", async () => {
    /**
     * Contract: DrawingToolbar exposes accessible aria-label and aria-pressed controls for all
     * 9 tools plus Magnet Mode, FVG Preset, OB Extend Right, Undo, Redo, Delete Selected, and Clear Pane,
     * and clicking Undo/Redo/Delete/Clear dispatches the corresponding CommandService API calls.
     * Defect caught: Missing tool buttons, broken disabled states, or non-wired action buttons.
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

    const requests: Array<{ url: string; method: string; body: unknown }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      requests.push({ url, method, body });

      if (url === "/api/drawings/MES/undo") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 3,
          can_undo: false,
          can_redo: true,
          deduplicated: false,
          affected_ids: ["d-1"],
          drawings: [],
        });
      }
      if (url === "/api/drawings/MES/redo") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 4,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["d-1"],
          drawings: [sampleDrawing],
        });
      }
      if (url === "/api/drawings/MES" && method === "POST") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 5,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["d-1"],
          drawings: [sampleDrawing],
        });
      }
      if (url.startsWith("/api/drawings/MES?") && method === "DELETE") {
        return jsonResponse(200, {
          symbol: "MES",
          contract_id: "CON.F.US.MES.H26",
          pane_id: "pane-1",
          instrument: { root_symbol: "MES", name: "MES", exchange: "CME", tick_size: 0.25, tick_value: 1.25, currency: "USD" },
          scope_revision: 6,
          can_undo: true,
          can_redo: false,
          deduplicated: false,
          affected_ids: ["d-1"],
          drawings: [],
        });
      }
      return jsonResponse(500, { detail: "unexpected" });
    });
    vi.stubGlobal("fetch", fetchMock);

    useDrawingStore.setState({
      scopes: {
        [scopeKey]: {
          symbol: "MES",
          contractId: "CON.F.US.MES.H26",
          paneId: "pane-1",
          initialized: true,
          drawings: [sampleDrawing],
          scopeRevision: 2,
          mutationEpoch: 0,
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
      const btn = screen.getByRole("button", { name: label });
      expect(btn.getAttribute("aria-pressed")).toBe(label === "Cursor tool" ? "true" : "false");
    }

    const cursorBtn = screen.getByRole("button", { name: "Cursor tool" });
    const fvgBtn = screen.getByRole("button", { name: "FVG box tool" });
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
    expect(obExtBtn.getAttribute("aria-pressed")).toBe("false");

    const presetSelect = screen.getByLabelText("FVG Preset") as HTMLSelectElement;
    fireEvent.change(presetSelect, { target: { value: "bullish" } });
    expect(useDrawingStore.getState().fvgPreset).toBe("bullish");

    const undoBtn = screen.getByRole("button", { name: "Undo drawing" }) as HTMLButtonElement;
    const redoBtn = screen.getByRole("button", { name: "Redo drawing" }) as HTMLButtonElement;
    const delBtn = screen.getByRole("button", { name: "Delete Selected" }) as HTMLButtonElement;
    const clearBtn = screen.getByRole("button", { name: "Clear Pane" }) as HTMLButtonElement;

    expect(undoBtn.disabled).toBe(false);
    expect(redoBtn.disabled).toBe(true);
    expect(delBtn.disabled).toBe(false);
    expect(clearBtn.disabled).toBe(false);

    // Click Undo -> calls /undo, disables Undo and enables Redo
    await act(async () => {
      fireEvent.click(undoBtn);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests[0]).toMatchObject({ url: "/api/drawings/MES/undo", method: "POST" });
    expect(redoBtn.disabled).toBe(false);

    // Click Redo -> calls /redo, restores drawing
    await act(async () => {
      fireEvent.click(redoBtn);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests[1]).toMatchObject({ url: "/api/drawings/MES/redo", method: "POST" });

    // Select d-1 and click Delete Selected -> calls POST delete
    act(() => {
      useDrawingStore.getState().setSelectedDrawing(scope, "d-1");
    });
    await act(async () => {
      fireEvent.click(delBtn);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests[2]).toMatchObject({
      url: "/api/drawings/MES",
      method: "POST",
      body: expect.objectContaining({ action: "delete", drawing_id: "d-1" }),
    });

    // Click Clear Pane -> calls DELETE /api/drawings/MES?expected_revision=...
    await act(async () => {
      fireEvent.click(clearBtn);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(requests[3]).toMatchObject({ method: "DELETE" });
    expect(requests[3]!.url).toContain("/api/drawings/MES?expected_revision=5");
    expect(clearBtn.disabled).toBe(true);
  });
});
