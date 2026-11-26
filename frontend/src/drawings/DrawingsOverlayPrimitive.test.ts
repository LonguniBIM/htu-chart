import type { Coordinate, IChartApiBase, ISeriesApi, Logical, SeriesType, Time } from "lightweight-charts";
import { describe, expect, it, vi } from "vitest";
import type { OhlcvBar } from "../api/types";
import { DrawingsOverlayPrimitive } from "./DrawingsOverlayPrimitive";
import { INSTRUMENT_SPECS, type DrawingRecord } from "./types";

function makeBar(time: number, close: number): OhlcvBar {
  return {
    time,
    open: close - 1,
    high: close + 2,
    low: close - 2,
    close,
    volume: 50,
    is_closed: true,
  };
}

function makeChartSeriesMocks(bars: readonly OhlcvBar[]) {
  const indexByTime = new Map<number, number>();
  bars.forEach((b, i) => indexByTime.set(b.time, i));

  const chart = {
    timeScale: () => ({
      timeToCoordinate: (time: Time) => {
        const idx = typeof time === "number" ? indexByTime.get(time) : undefined;
        return idx !== undefined ? ((100 + idx * 50) as Coordinate) : null;
      },
      coordinateToTime: () => null,
      logicalToCoordinate: (logical: Logical) => (100 + Number(logical) * 50) as Coordinate,
      coordinateToLogical: (x: number) => ((x - 100) / 50) as Logical,
    }),
  } as unknown as IChartApiBase<Time>;

  const series = {
    priceToCoordinate: (price: number) => ((5050 - price) * 4) as Coordinate,
    coordinateToPrice: (y: number) => 5050 - y / 4,
  } as unknown as ISeriesApi<SeriesType, Time>;

  return { chart, series };
}

function makeFakeCanvasContext() {
  const fillTexts: string[] = [];
  const dashCalls: number[][] = [];
  const fillRects: Array<[number, number, number, number]> = [];
  const ctx = {
    font: "",
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    globalAlpha: 1,
    textBaseline: "alphabetic",
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    arc: vi.fn(),
    fillRect: vi.fn((x: number, y: number, w: number, h: number) => {
      fillRects.push([x, y, w, h]);
    }),
    strokeRect: vi.fn(),
    setLineDash: vi.fn((segments: number[]) => {
      dashCalls.push(segments);
    }),
    measureText: vi.fn((text: string) => ({ width: text.length * 6 })),
    fillText: vi.fn((text: string) => {
      fillTexts.push(text);
    }),
  } as unknown as CanvasRenderingContext2D;

  return { ctx, fillTexts, dashCalls, fillRects };
}

describe("DrawingsOverlayPrimitive (Phase 4 Part 2)", () => {
  it("renders FVG 50% CE dashed line, OB extend_right, Position HUD, Measure HUD, and axis tags", () => {
    /**
     * Contract: DrawingsOverlayPrimitive implements ISeriesPrimitive<Time>, emits right-axis
     * price tags for hline and selected position Entry/TP/SL, renders FVG 50% CE dashed lines,
     * OB extend_right rectangles, Position R:R/tick/$ HUD, and Measure HUD via useMediaCoordinateSpace.
     * Defect caught: Missing price axis views, missing FVG 50% CE dashed midline, or broken canvas rendering.
     */
    const bars = [makeBar(1700000000, 5000), makeBar(1700000300, 5010), makeBar(1700000600, 5020)];
    const { chart, series } = makeChartSeriesMocks(bars);
    const requestUpdate = vi.fn();

    const drawings: DrawingRecord[] = [
      {
        id: "hl-1",
        revision: 1,
        type: "hline",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        created_by: "user",
        source_bar_refs: [],
        coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5005 }] },
        style: {},
        metadata: {},
        created_at_utc: 1700000000,
        updated_at_utc: 1700000000,
      },
      {
        id: "fvg-1",
        revision: 1,
        type: "fvg_box",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        created_by: "user",
        source_bar_refs: [],
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 5000 },
            { role: "end", time: 1700000300, price: 5010 },
          ],
        },
        style: {},
        metadata: { direction: "bullish" },
        created_at_utc: 1700000000,
        updated_at_utc: 1700000000,
      },
      {
        id: "ob-1",
        revision: 1,
        type: "order_block",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        created_by: "user",
        source_bar_refs: [],
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 4995 },
            { role: "end", time: 1700000300, price: 5000 },
          ],
        },
        style: {},
        metadata: { extend_right: true, label: "Bull OB" },
        created_at_utc: 1700000000,
        updated_at_utc: 1700000000,
      },
      {
        id: "pos-1",
        revision: 1,
        type: "long_position",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        created_by: "user",
        source_bar_refs: [],
        coordinates: {
          anchors: [
            { role: "entry", time: 1700000000, price: 5000 },
            { role: "target", time: 1700000600, price: 5010 },
            { role: "stop", time: 1700000600, price: 4995 },
          ],
        },
        style: {},
        metadata: {},
        created_at_utc: 1700000000,
        updated_at_utc: 1700000000,
      },
      {
        id: "meas-1",
        revision: 1,
        type: "measure",
        symbol: "MES",
        contract_id: "CON.F.US.MES.H26",
        pane_id: "pane-1",
        created_by: "user",
        source_bar_refs: [],
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 5000 },
            { role: "end", time: 1700000600, price: 5005 },
          ],
        },
        style: {},
        metadata: {},
        created_at_utc: 1700000000,
        updated_at_utc: 1700000000,
      },
    ];

    const primitive = new DrawingsOverlayPrimitive();
    primitive.attached({
      chart,
      series,
      requestUpdate,
      horzScaleBehavior: {} as never,
    });
    primitive.setViewportWidth(900);
    primitive.setData({
      drawings,
      selectedDrawingId: "pos-1",
      draft: null,
      bars,
      timeframeSec: 300,
      instrumentSpec: INSTRUMENT_SPECS.MES,
    });

    expect(requestUpdate).toHaveBeenCalledTimes(1);

    // Axis views: 1 hline + 3 tags for selected pos-1 (Entry, TP, SL)
    const axisViews = primitive.priceAxisViews();
    expect(axisViews.map((v) => v.text())).toEqual([
      "5005.00",
      "Entry 5000.00",
      "TP 5010.00",
      "SL 4995.00",
    ]);
    expect(axisViews.every((v) => v.visible?.() === true)).toBe(true);

    // Render onto media coordinate space
    const { ctx, fillTexts, dashCalls, fillRects } = makeFakeCanvasContext();
    const renderer = primitive.paneViews()[0]!.renderer();
    expect(renderer).not.toBeNull();
    renderer!.draw({
      useMediaCoordinateSpace: (cb: (scope: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => {
        cb({ context: ctx, mediaSize: { width: 900, height: 500 } });
      },
    } as never);

    expect(dashCalls).toContainEqual([5, 4]);
    expect(fillTexts).toContain("50% CE");
    expect(fillTexts).toContain("Bull OB");
    expect(fillTexts.some((t) => t.includes("R:R 2.00") && t.includes("20t ($25.00)"))).toBe(true);
    expect(fillTexts.some((t) => t.includes("+5.00") && t.includes("2 bars") && t.includes("10m"))).toBe(true);
    // OB extend_right reaches mediaWidth (900 - 100 = 800 width)
    expect(fillRects.some((r) => r[0] === 100 && r[2] === 800)).toBe(true);

    // Hit-test via primitive delegates cleanly
    expect(primitive.hitTestAt(100, 200)).toMatchObject({ drawingId: "pos-1", anchorRole: "entry" });

    primitive.detached();
    expect(primitive.priceAxisViews()).toEqual([]);
  });
});
