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
  const lineSegments: Array<{ x1: number; y1: number; x2: number; y2: number }> = [];
  let moveX = 0;
  let moveY = 0;

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
    moveTo: vi.fn((x: number, y: number) => {
      moveX = x;
      moveY = y;
    }),
    lineTo: vi.fn((x: number, y: number) => {
      lineSegments.push({ x1: moveX, y1: moveY, x2: x, y2: y });
      moveX = x;
      moveY = y;
    }),
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

  return { ctx, fillTexts, dashCalls, fillRects, lineSegments };
}

describe("DrawingsOverlayPrimitive (Phase 4 Part 2)", () => {
  it("renders all 8 drawing types, Bullish/Bearish FVG CE labels, draft preview, and right-axis tags", () => {
    /**
     * Contract: DrawingsOverlayPrimitive implements ISeriesPrimitive<Time>, renders all 8 drawing
     * types (trendline, ray, hline, bullish/bearish fvg_box with formatted CE labels, order_block,
     * long_position, short_position, measure) plus non-null draft previews via useMediaCoordinateSpace,
     * and emits right-axis price tags for hlines and selected position Entry/TP/SL.
     * Defect caught: Missing drawing type renderers, static "50% CE" label, or dropped draft preview.
     */
    const bars = [makeBar(1700000000, 5000), makeBar(1700000300, 5010), makeBar(1700000600, 5020)];
    const { chart, series } = makeChartSeriesMocks(bars);
    const requestUpdate = vi.fn();

    const base = {
      revision: 1,
      symbol: "MES" as const,
      contract_id: "CON.F.US.MES.H26",
      pane_id: "pane-1",
      created_by: "user" as const,
      source_bar_refs: [],
      style: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };

    const drawings: DrawingRecord[] = [
      {
        ...base,
        id: "tl-1",
        type: "trendline",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 4990 },
            { role: "end", time: 1700000300, price: 5000 },
          ],
        },
        metadata: {},
      },
      {
        ...base,
        id: "ray-1",
        type: "ray",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 4992 },
            { role: "end", time: 1700000300, price: 4996 },
          ],
        },
        metadata: {},
      },
      {
        ...base,
        id: "hl-1",
        type: "hline",
        coordinates: { anchors: [{ role: "level", time: 1700000000, price: 5005 }] },
        metadata: {},
      },
      {
        ...base,
        id: "fvg-bull",
        type: "fvg_box",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 5000 },
            { role: "end", time: 1700000300, price: 5010 },
          ],
        },
        metadata: { direction: "bullish" },
      },
      {
        ...base,
        id: "fvg-bear",
        type: "fvg_box",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000300, price: 5020 },
            { role: "end", time: 1700000600, price: 5010 },
          ],
        },
        metadata: { direction: "bearish" },
      },
      {
        ...base,
        id: "ob-1",
        type: "order_block",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 4995 },
            { role: "end", time: 1700000300, price: 5000 },
          ],
        },
        metadata: { extend_right: true, label: "Bull OB" },
      },
      {
        ...base,
        id: "pos-long",
        type: "long_position",
        coordinates: {
          anchors: [
            { role: "entry", time: 1700000000, price: 5000 },
            { role: "target", time: 1700000600, price: 5010 },
            { role: "stop", time: 1700000600, price: 4995 },
          ],
        },
        metadata: {},
      },
      {
        ...base,
        id: "pos-short",
        type: "short_position",
        coordinates: {
          anchors: [
            { role: "entry", time: 1700000000, price: 5020 },
            { role: "target", time: 1700000600, price: 5000 },
            { role: "stop", time: 1700000600, price: 5030 },
          ],
        },
        metadata: {},
      },
      {
        ...base,
        id: "meas-1",
        type: "measure",
        coordinates: {
          anchors: [
            { role: "start", time: 1700000000, price: 5000 },
            { role: "end", time: 1700000600, price: 5005 },
          ],
        },
        metadata: {},
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
      selectedDrawingId: "pos-short",
      draft: {
        tool: "trendline",
        anchors: [
          { role: "start", time: 1700000000, price: 4980 },
          { role: "end", time: 1700000600, price: 4988 },
        ],
      },
      bars,
      timeframeSec: 300,
      instrumentSpec: INSTRUMENT_SPECS.MES,
    });

    expect(requestUpdate).toHaveBeenCalledTimes(1);

    // Axis views: hline + selected short_position (Entry, TP, SL) — unselected long_position omitted
    const axisViews = primitive.priceAxisViews();
    expect(axisViews.map((v) => v.text())).toEqual([
      "5005.00",
      "Entry 5020.00",
      "TP 5000.00",
      "SL 5030.00",
    ]);
    expect(axisViews.every((v) => v.visible?.() === true)).toBe(true);

    const snapshot = primitive.getRenderSnapshot();
    expect(snapshot.projectedDrawings).toHaveLength(9);
    expect(snapshot.projectedDraft?.drawing.id).toBe("__draft__");

    const { ctx, fillTexts, dashCalls, fillRects, lineSegments } = makeFakeCanvasContext();
    const renderer = primitive.paneViews()[0]!.renderer();
    expect(renderer).not.toBeNull();
    renderer!.draw({
      useMediaCoordinateSpace: (cb: (scope: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => {
        cb({ context: ctx, mediaSize: { width: 900, height: 500 } });
      },
    } as never);

    expect(dashCalls).toContainEqual([5, 4]);
    expect(fillTexts).toContain("Bullish FVG · CE 5005.00");
    expect(fillTexts).toContain("Bearish FVG · CE 5015.00");
    expect(fillTexts).toContain("Bull OB");
    // Both long (20t / $25) and short (40t / $50) R:R 2.00 HUD labels rendered
    expect(fillTexts.some((t) => t.includes("R:R 2.00") && t.includes("20t ($25.00)"))).toBe(true);
    expect(fillTexts.some((t) => t.includes("R:R 2.00") && t.includes("40t ($50.00)"))).toBe(true);
    expect(fillTexts.some((t) => t.includes("+5.00") && t.includes("2 bars") && t.includes("10m"))).toBe(true);
    // OB extend_right reaches mediaWidth (900 - 100 = 800 width)
    expect(fillRects.some((r) => r[0] === 100 && r[2] === 800)).toBe(true);
    // Trendline (100,240)->(150,200), ray extending past viewport (x2 > 900), and draft trendline (100,280)->(200,248)
    expect(lineSegments).toContainEqual({ x1: 100, y1: 240, x2: 150, y2: 200 });
    expect(lineSegments.some((s) => s.x1 === 100 && s.y1 === 232 && s.x2 > 900)).toBe(true);
    expect(lineSegments).toContainEqual({ x1: 100, y1: 280, x2: 200, y2: 248 });

    // Hit-test selects anchor handle of selected short_position (entry at 100, (5050-5020)*4 = 120)
    expect(primitive.hitTestAt(100, 120)).toEqual({ drawingId: "pos-short", anchorRole: "entry" });

    primitive.detached();
    expect(primitive.priceAxisViews()).toEqual([]);
  });
});
