import { describe, expect, it } from "vitest";
import type {
  BacktestIndicatorSeries,
  BacktestMarker,
  BacktestTradeLine,
  BacktestZone,
  DrawingRecord,
  OhlcvBar,
} from "../api/types";
import {
  CHECKPOINT_INTERVAL,
  computePaperTradePnl,
  computeReplayStats,
  createReplaySession,
  filterBarsByReplayCursor,
  filterDrawingsByReplayCursor,
  filterIndicatorsByReplayCursor,
  filterMarkersByReplayCursor,
  filterTradeLinesByReplayCursor,
  filterZonesByReplayCursor,
  flattenPaperPosition,
  openPaperPosition,
  stepReplayBack,
  stepReplayForward,
} from "./replayEngine";

function makeBars(count: number, startTime = 1700000000, baseClose = 5000): OhlcvBar[] {
  const bars: OhlcvBar[] = [];
  for (let i = 0; i < count; i += 1) {
    const close = baseClose + i * 0.25;
    bars.push({
      time: startTime + i * 300,
      open: close - 0.25,
      high: close + 0.5,
      low: close - 0.5,
      close,
      volume: 100 + i,
      is_closed: true,
    });
  }
  return bars;
}

describe("replayEngine deterministic F08 causality & CME P&L", () => {
  it("deterministically rewinds across CHECKPOINT_INTERVAL=25 boundaries and prunes future actions", () => {
    /**
     * Contract: Stepping forward from cursorIndex=20 to 40 with open/close paper trades and
     * stepping back across checkpoint boundaries reproduces identical state to an uninterrupted
     * forward run and prunes actions beyond the rewound cursor.
     * Defect caught: Non-deterministic checkpoint restoration, off-by-one bar replay, or leaked future actions.
     */
    expect(CHECKPOINT_INTERVAL).toBe(25);
    const bars = makeBars(60);

    // Uninterrupted forward reference from 20 -> 39
    let ref = createReplaySession(bars, "MES", 20);
    ref = stepReplayForward(ref); // 21
    ref = openPaperPosition(ref, {
      direction: "LONG",
      quantity: 2,
      stopLoss: 4990,
      takeProfit: 5050,
    });
    for (let i = 21; i < 25; i += 1) {
      ref = stepReplayForward(ref);
    }
    // At bar 25 (checkpoint boundary): flatten and open another trade at 28
    expect(ref.cursorIndex).toBe(25);
    ref = flattenPaperPosition(ref);
    for (let i = 25; i < 28; i += 1) {
      ref = stepReplayForward(ref);
    }
    ref = openPaperPosition(ref, {
      direction: "SHORT",
      quantity: 1,
      stopLoss: 5030,
      takeProfit: 4980,
    });
    for (let i = 28; i < 39; i += 1) {
      ref = stepReplayForward(ref);
    }
    expect(ref.cursorIndex).toBe(39);

    // Now advance to 40, open/close an extra future trade at 40, then stepBack(-1) to 39
    let rewound = stepReplayForward(ref); // 40
    rewound = flattenPaperPosition(rewound);
    expect(rewound.cursorIndex).toBe(40);
    expect(rewound.closedTrades).toHaveLength(2);

    rewound = stepReplayBack(rewound); // back to 39
    expect(rewound.cursorIndex).toBe(ref.cursorIndex);
    expect(rewound.openPosition).toEqual(ref.openPosition);
    expect(rewound.closedTrades).toEqual(ref.closedTrades);
    expect(rewound.realizedPnlDollars).toBe(ref.realizedPnlDollars);
    expect(rewound.actions).toEqual(ref.actions);

    // Step all the way back across bar 25 checkpoint to bar 24
    let refAt24 = createReplaySession(bars, "MES", 20);
    refAt24 = stepReplayForward(refAt24); // 21
    refAt24 = openPaperPosition(refAt24, {
      direction: "LONG",
      quantity: 2,
      stopLoss: 4990,
      takeProfit: 5050,
    });
    for (let i = 21; i < 24; i += 1) {
      refAt24 = stepReplayForward(refAt24);
    }

    while (rewound.cursorIndex > 24) {
      rewound = stepReplayBack(rewound);
    }
    expect(rewound.cursorIndex).toBe(24);
    expect(rewound.openPosition).toEqual(refAt24.openPosition);
    expect(rewound.closedTrades).toEqual(refAt24.closedTrades);
    expect(rewound.realizedPnlDollars).toBe(refAt24.realizedPnlDollars);
    expect(rewound.actions).toEqual(refAt24.actions);
  });

  it("enforces conservative F08 SL-first tie-breaking with ambiguousFill and gap-aware fills", () => {
    /**
     * Contract: When a single bar touches both SL and TP for LONG or SHORT, replay resolves
     * stop_loss conservatively with ambiguousFill=true and gap-aware open pricing.
     * Defect caught: Optimistic TP-first resolution or missing ambiguousFill / gap-open fills.
     */
    const bars: OhlcvBar[] = [
      { time: 1000, open: 5000, high: 5001, low: 4999, close: 5000, volume: 10, is_closed: true },
      // Bar 1: gaps below LONG SL 4995 (open=4992) and also hits TP 5005 (high=5008)
      { time: 1300, open: 4992, high: 5008, low: 4990, close: 5004, volume: 20, is_closed: true },
      // Bar 2: entry bar for SHORT at 5004
      { time: 1600, open: 5004, high: 5005, low: 5003, close: 5004, volume: 20, is_closed: true },
      // Bar 3: touches both SHORT SL 5008 (open=5006, high=5010) and TP 4998 (low=4997)
      { time: 1900, open: 5006, high: 5010, low: 4997, close: 5000, volume: 20, is_closed: true },
    ];

    let session = createReplaySession(bars, "MES", 0);
    session = openPaperPosition(session, {
      direction: "LONG",
      quantity: 1,
      stopLoss: 4995,
      takeProfit: 5005,
    });
    session = stepReplayForward(session);
    expect(session.openPosition).toBeNull();
    expect(session.closedTrades).toHaveLength(1);
    const longTrade = session.closedTrades[0]!;
    expect(longTrade.exitReason).toBe("stop_loss");
    expect(longTrade.ambiguousFill).toBe(true);
    expect(longTrade.exitPrice).toBe(4992); // gap-aware open <= stopLoss

    session = stepReplayForward(session); // bar 2 close = 5004
    session = openPaperPosition(session, {
      direction: "SHORT",
      quantity: 1,
      stopLoss: 5008,
      takeProfit: 4998,
    });
    session = stepReplayForward(session); // bar 3
    expect(session.openPosition).toBeNull();
    expect(session.closedTrades).toHaveLength(2);
    const shortTrade = session.closedTrades[1]!;
    expect(shortTrade.exitReason).toBe("stop_loss");
    expect(shortTrade.ambiguousFill).toBe(true);
    expect(shortTrade.exitPrice).toBe(5008); // open 5006 < 5008 -> fills at stopLoss

    const stats = computeReplayStats(session);
    expect(stats.totalTrades).toBe(2);
    expect(stats.ambiguousFillCount).toBe(2);
    expect(stats.losingTrades).toBe(2);
    expect(stats.winRatePct).toBe(0);
  });

  it("computes exact CME tick-scaled P&L across MES, MNQ, MYM, and MGC without float drift", () => {
    /**
     * Contract: Integer tick-scaled math produces exact CME pnlTicks and pnlDollars across all 4
     * micro contracts including 0.10 MGC ticks and quantity > 1.
     * Defect caught: IEEE-754 floating point drift on 0.10/0.25 tick deltas or wrong tickValue.
     */
    expect(computePaperTradePnl("MES", "LONG", 5000.0, 5002.5, 2)).toEqual({
      pnlTicks: 10,
      pnlDollars: 25,
    });
    expect(computePaperTradePnl("MNQ", "SHORT", 18000.75, 17999.5, 3)).toEqual({
      pnlTicks: 5,
      pnlDollars: 7.5,
    });
    expect(computePaperTradePnl("MYM", "LONG", 39000, 39015, 2)).toEqual({
      pnlTicks: 15,
      pnlDollars: 15,
    });
    // 2650.3 - 2650.1 in binary float is 0.1999999999998181 -> must be exactly 2 ticks / $4.00
    expect(computePaperTradePnl("MGC", "LONG", 2650.1, 2650.3, 2)).toEqual({
      pnlTicks: 2,
      pnlDollars: 4,
    });
  });

  it("filters bars, zones, markers, trade lines, indicators, and drawings strictly by cursorTime", () => {
    /**
     * Contract: No bar, zone, marker, trade line, indicator point, or drawing with timestamp
     * or detected_at_time > cursorTime leaks through the F08 causality filters.
     * Defect caught: Future look-ahead leakage during replay mode.
     */
    const cursorTime = 1700000600;
    const bars = makeBars(5, 1700000000);
    expect(filterBarsByReplayCursor(bars, cursorTime).map((b) => b.time)).toEqual([
      1700000000, 1700000300, 1700000600,
    ]);

    const zones: BacktestZone[] = [
      {
        kind: "fvg",
        direction: "bullish",
        start_time: 1700000000,
        end_time: 1700000600,
        detected_at_time: 1700000600,
        top: 5005,
        bottom: 5001,
        midpoint: 5003,
        label: "Bullish FVG",
      },
      {
        kind: "order_block",
        direction: "bearish",
        start_time: 1700000300,
        end_time: 1700000900,
        detected_at_time: 1700000900,
        top: 5010,
        bottom: 5006,
        midpoint: null,
        label: "Future OB",
      },
    ];
    expect(filterZonesByReplayCursor(zones, cursorTime)).toHaveLength(1);

    const markers: BacktestMarker[] = [
      { time: 1700000300, position: "belowBar", color: "#22c55e", shape: "arrowUp", text: "LONG" },
      { time: 1700000900, position: "aboveBar", color: "#ef4444", shape: "arrowDown", text: "EXIT" },
    ];
    expect(filterMarkersByReplayCursor(markers, cursorTime)).toHaveLength(1);

    const lines: BacktestTradeLine[] = [
      {
        trade_id: "T1",
        direction: "LONG",
        entry_time: 1700000000,
        entry_price: 5000,
        exit_time: 1700000600,
        exit_price: 5004,
        pnl_dollars: 20,
        pnl_ticks: 16,
        color: "#22c55e",
        ambiguous_fill: false,
      },
      {
        trade_id: "T2",
        direction: "SHORT",
        entry_time: 1700000300,
        entry_price: 5003,
        exit_time: 1700000900,
        exit_price: 5001,
        pnl_dollars: 10,
        pnl_ticks: 8,
        color: "#22c55e",
        ambiguous_fill: false,
      },
    ];
    expect(filterTradeLinesByReplayCursor(lines, cursorTime).map((l) => l.trade_id)).toEqual(["T1"]);

    const indicators: BacktestIndicatorSeries[] = [
      {
        id: "ema_20",
        name: "EMA (20)",
        color: "#f59e0b",
        overlay: true,
        points: [
          { time: 1700000300, value: 5001 },
          { time: 1700000600, value: 5002 },
          { time: 1700000900, value: 5003 },
        ],
      },
    ];
    const filteredInd = filterIndicatorsByReplayCursor(indicators, cursorTime);
    expect(filteredInd[0]!.points.map((p) => p.time)).toEqual([1700000300, 1700000600]);

    const baseDrawing: DrawingRecord = {
      id: "d1",
      revision: 1,
      type: "trendline",
      symbol: "MES",
      contract_id: "CON.MES",
      pane_id: "pane-1",
      created_by: "user",
      source_bar_refs: [],
      coordinates: {
        anchors: [
          { role: "start", time: 1700000000, price: 5000 },
          { role: "end", time: 1700000600, price: 5002 },
        ],
      },
      style: {},
      metadata: {},
      created_at_utc: 1700000000,
      updated_at_utc: 1700000000,
    };
    const futureAnchorDrawing: DrawingRecord = {
      ...baseDrawing,
      id: "d2",
      coordinates: {
        anchors: [
          { role: "start", time: 1700000300, price: 5000 },
          { role: "end", time: 1700000900, price: 5004 },
        ],
      },
    };
    const futureDetectedAtDrawing: DrawingRecord = {
      ...baseDrawing,
      id: "d3",
      metadata: { detected_at_time: 1700000900 },
    };
    expect(
      filterDrawingsByReplayCursor(
        [baseDrawing, futureAnchorDrawing, futureDetectedAtDrawing],
        cursorTime,
      ).map((d) => d.id),
    ).toEqual(["d1"]);
  });
});
