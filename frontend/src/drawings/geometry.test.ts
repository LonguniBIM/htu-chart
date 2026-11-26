import type { Coordinate, Logical } from "lightweight-charts";
import { describe, expect, it } from "vitest";
import type { OhlcvBar } from "../api/types";
import {
  clampPositionAnchors,
  computeFvgMidpoint,
  computeMeasureMetrics,
  computePositionMetrics,
  hitTestAnchorHandle,
  hitTestDrawing,
  snapPriceToTick,
  snapToNearestOhlc,
  timeToX,
  xToTime,
  type ProjectedDrawingGeometry,
  type TimeScaleCoordinateApi,
} from "./geometry";
import { INSTRUMENT_SPECS, type DrawingRecord } from "./types";

function makeBar(time: number, open: number, high: number, low: number, close: number): OhlcvBar {
  return { time, open, high, low, close, volume: 100, is_closed: true };
}

function makeFakeTimeScale(bars: readonly OhlcvBar[], pxPerBar = 10, originX = 100): TimeScaleCoordinateApi {
  const indexByTime = new Map<number, number>();
  bars.forEach((b, i) => indexByTime.set(b.time, i));
  return {
    timeToCoordinate: (time) => {
      const idx = typeof time === "number" ? indexByTime.get(time) : undefined;
      return idx !== undefined ? ((originX + idx * pxPerBar) as Coordinate) : null;
    },
    coordinateToTime: (x) => {
      const logical = (x - originX) / pxPerBar;
      const rounded = Math.round(logical);
      if (rounded >= 0 && rounded < bars.length && Math.abs(logical - rounded) < 0.45) {
        return bars[rounded]!.time as never;
      }
      return null;
    },
    logicalToCoordinate: (logical) => (originX + Number(logical) * pxPerBar) as Coordinate,
    coordinateToLogical: (x) => ((x - originX) / pxPerBar) as Logical,
  };
}

describe("drawing geometry and domain math (Phase 4 Part 2)", () => {
  it("snaps prices cleanly to CME tick sizes across MES/MNQ/MYM/MGC without float drift", () => {
    /**
     * Contract: snapPriceToTick rounds raw prices to exact multiples of instrument tickSize
     * without IEEE-754 floating-point artifacts.
     * Defect caught: Off-tick drawing prices or 2400.3000000000002 precision drift on MGC/MES.
     */
    expect(snapPriceToTick(5000.14, INSTRUMENT_SPECS.MES.tickSize)).toBe(5000.25);
    expect(snapPriceToTick(5000.12, INSTRUMENT_SPECS.MES.tickSize)).toBe(5000.0);
    expect(snapPriceToTick(18250.37, INSTRUMENT_SPECS.MNQ.tickSize)).toBe(18250.25);
    expect(snapPriceToTick(42111.6, INSTRUMENT_SPECS.MYM.tickSize)).toBe(42112);
    expect(snapPriceToTick(2400.34, INSTRUMENT_SPECS.MGC.tickSize)).toBe(2400.3);
  });

  it("snaps Magnet Mode coordinates to nearest bar timestamp and closest OHLC price", () => {
    /**
     * Contract: snapToNearestOhlc selects the nearest bar by time and the closest price
     * among [open, high, low, close].
     * Defect caught: Magnet Mode snapping to raw pointer time or ignoring bar high/low wicks.
     */
    const bars = [
      makeBar(1700000000, 5000, 5008, 4996, 5005),
      makeBar(1700000300, 5005, 5015, 5001, 5010),
    ];
    expect(snapToNearestOhlc(1700000100, 5007.2, bars)).toEqual({
      time: 1700000000,
      price: 5008,
    });
    expect(snapToNearestOhlc(1700000280, 5001.4, bars)).toEqual({
      time: 1700000300,
      price: 5001,
    });
  });

  it("interpolates timeToX between higher-timeframe bars and extrapolates into future margin", () => {
    /**
     * Contract: timeToX/xToTime handle exact bar timestamps, timestamps strictly between
     * higher-timeframe bars via logicalToCoordinate, and timestamps in the right-hand future margin.
     * Defect caught: Drawings disappearing on TF switch (e.g. 5m -> 1h) or failing in future margin.
     */
    const bars1h = [
      makeBar(1700000000, 5000, 5010, 4995, 5005),
      makeBar(1700003600, 5005, 5015, 5000, 5012),
    ];
    const timeScale = makeFakeTimeScale(bars1h, 40, 100);

    // Exact bar
    expect(timeToX(1700000000, timeScale, bars1h, 3600)).toBe(100);
    // 25% between 1h bars (15m offset = +900s) -> logical 0.25 -> x = 110
    const betweenX = timeToX(1700000900, timeScale, bars1h, 3600);
    expect(betweenX).toBe(110);
    // Exact round-trip timeToX -> xToTime -> timeToX even when coordinateToTime snaps to nearest bar
    const roundTrippedTime = xToTime(betweenX!, timeScale, bars1h, 3600);
    expect(roundTrippedTime).toBe(1700000900);
    expect(timeToX(roundTrippedTime!, timeScale, bars1h, 3600)).toBe(110);

    // Larger bar gap (e.g. weekend gap of 72h = 259200s): midpoint interpolates cleanly
    const gapBars = [
      makeBar(1700000000, 5000, 5010, 4995, 5005),
      makeBar(1700259200, 5005, 5015, 5000, 5012),
    ];
    const gapScale = makeFakeTimeScale(gapBars, 40, 100);
    const gapMidX = timeToX(1700129600, gapScale, gapBars, 3600);
    expect(gapMidX).toBe(120);
    expect(xToTime(gapMidX!, gapScale, gapBars, 3600)).toBe(1700129600);

    // 2 bars into future margin (+7200s after last bar) -> logical 1 + 2 = 3 -> x = 220
    expect(timeToX(1700010800, timeScale, bars1h, 3600)).toBe(220);
    // Round-trip xToTime from future margin
    expect(xToTime(220, timeScale, bars1h, 3600)).toBe(1700010800);
  });

  it("computes exact 50% FVG midpoint, Position R:R/ticks/$ metrics, and Measure metrics", () => {
    /**
     * Contract: computeFvgMidpoint returns exact 50% CE, computePositionMetrics returns
     * tick/dollar/R:R metrics with zero-risk guard, and computeMeasureMetrics computes delta/bars/duration.
     * Defect caught: Wrong FVG 50% CE, wrong instrument tickValue multiplier, or NaN on zero risk.
     */
    expect(computeFvgMidpoint(5010, 5020)).toBe(5015);

    const longMetrics = computePositionMetrics(
      "long_position",
      5000,
      5010,
      4995,
      INSTRUMENT_SPECS.MES,
      2,
    );
    expect(longMetrics).toEqual({
      riskPrice: 5,
      rewardPrice: 10,
      riskTicks: 20,
      rewardTicks: 40,
      dollarRisk: 50,
      dollarReward: 100,
      rrRatio: 2,
    });

    const zeroRisk = computePositionMetrics("short_position", 5000, 4990, 5000, INSTRUMENT_SPECS.MNQ);
    expect(zeroRisk.rrRatio).toBe(0);

    const mnqMetrics = computePositionMetrics("short_position", 18000, 17980, 18010, INSTRUMENT_SPECS.MNQ);
    expect(mnqMetrics).toEqual({
      riskPrice: 10,
      rewardPrice: 20,
      riskTicks: 40,
      rewardTicks: 80,
      dollarRisk: 20,
      dollarReward: 40,
      rrRatio: 2,
    });

    const mymMetrics = computePositionMetrics("long_position", 42000, 42030, 41990, INSTRUMENT_SPECS.MYM);
    expect(mymMetrics).toEqual({
      riskPrice: 10,
      rewardPrice: 30,
      riskTicks: 10,
      rewardTicks: 30,
      dollarRisk: 5,
      dollarReward: 15,
      rrRatio: 3,
    });

    const mgcMetrics = computePositionMetrics("short_position", 2400, 2394, 2403, INSTRUMENT_SPECS.MGC, 2);
    expect(mgcMetrics.riskPrice).toBeCloseTo(3, 6);
    expect(mgcMetrics.rewardPrice).toBeCloseTo(6, 6);
    expect(mgcMetrics.riskTicks).toBeCloseTo(30, 6);
    expect(mgcMetrics.rewardTicks).toBeCloseTo(60, 6);
    expect(mgcMetrics.dollarRisk).toBeCloseTo(60, 6);
    expect(mgcMetrics.dollarReward).toBeCloseTo(120, 6);
    expect(mgcMetrics.rrRatio).toBeCloseTo(2, 6);

    const bars5m = [
      makeBar(1700000000, 5000, 5002, 4998, 5001),
      makeBar(1700000300, 5001, 5005, 5000, 5004),
      makeBar(1700000600, 5004, 5008, 5003, 5006),
      makeBar(1700000900, 5006, 5010, 5005, 5009),
    ];
    const measure = computeMeasureMetrics(
      { time: 1700000000, price: 5000 },
      { time: 1700000900, price: 5005 },
      bars5m,
      300,
      INSTRUMENT_SPECS.MES,
    );
    expect(measure.priceDelta).toBe(5);
    expect(measure.pctChange).toBeCloseTo(0.1, 6);
    expect(measure.ticks).toBe(20);
    expect(measure.barCount).toBe(3);
    expect(measure.durationSec).toBe(900);
    expect(measure.durationLabel).toBe("15m");
  });

  it("enforces directional >= 1*tickSize clamping for long_position and short_position", () => {
    /**
     * Contract: clampPositionAnchors prevents target/stop inversion or equality relative to entry
     * by enforcing at least 1 * tickSize separation.
     * Defect caught: Dragging TP below entry on long_position causing backend 422 rejection.
     */
    const clampedLong = clampPositionAnchors(
      "long_position",
      [
        { role: "entry", time: 1700000000, price: 5000 },
        { role: "target", time: 1700000300, price: 4998 },
        { role: "stop", time: 1700000300, price: 5001 },
      ],
      "target",
      0.25,
    );
    expect(clampedLong.find((a) => a.role === "target")?.price).toBe(5000.25);
    expect(clampedLong.find((a) => a.role === "stop")?.price).toBe(4999.75);

    const clampedShort = clampPositionAnchors(
      "short_position",
      [
        { role: "entry", time: 1700000000, price: 5000 },
        { role: "target", time: 1700000300, price: 5002 },
        { role: "stop", time: 1700000300, price: 4999 },
      ],
      "stop",
      0.25,
    );
    expect(clampedShort.find((a) => a.role === "target")?.price).toBe(4999.75);
    expect(clampedShort.find((a) => a.role === "stop")?.price).toBe(5000.25);
  });

  it("prioritizes anchor handles over line/box bodies in hitTestDrawing", () => {
    /**
     * Contract: hitTestDrawing returns the anchor handle role whenever a pointer hits within
     * handleRadiusPx of an anchor, taking precedence over overlapping body segments.
     * Defect caught: Body hit intercepting handle drags so users cannot resize trendlines or boxes.
     */
    const trendline: DrawingRecord = {
      id: "tl-1",
      revision: 1,
      type: "trendline",
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
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };
    const ob: DrawingRecord = {
      ...trendline,
      id: "ob-1",
      type: "order_block",
      metadata: { extend_right: true },
    };
    const projected: ProjectedDrawingGeometry[] = [
      {
        drawing: ob,
        anchors: [
          { role: "start", time: 1700000000, price: 5000, x: 100, y: 200 },
          { role: "end", time: 1700000300, price: 5010, x: 200, y: 100 },
        ],
      },
      {
        drawing: trendline,
        anchors: [
          { role: "start", time: 1700000000, price: 5000, x: 100, y: 200 },
          { role: "end", time: 1700000300, price: 5010, x: 200, y: 100 },
        ],
      },
    ];

    // Hit near anchor handle -> handle takes priority over body
    expect(hitTestAnchorHandle(202, 102, projected[1]!.anchors)).toMatchObject({ role: "end" });
    expect(hitTestDrawing(202, 102, projected, "tl-1", 800)).toEqual({
      drawingId: "tl-1",
      anchorRole: "end",
    });

    // Hit midpoint of trendline -> body hit (anchorRole: null)
    expect(hitTestDrawing(150, 150, projected, "tl-1", 800)).toEqual({
      drawingId: "tl-1",
      anchorRole: null,
    });

    // Hit rightward extension of order_block beyond end.x -> ob-1 body hit
    expect(hitTestDrawing(650, 150, projected, null, 800)).toEqual({
      drawingId: "ob-1",
      anchorRole: null,
    });
  });
});
