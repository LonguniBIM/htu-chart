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
import { useReplayStore } from "./replayStore";

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
  it("deterministically rewinds across CHECKPOINT_INTERVAL=25 boundaries (20 -> 50 -> 46 -> 35) and prunes future actions", () => {
    /**
     * Contract: Stepping forward from startIndex=20 to cursorIndex=50 crosses barIndex=45 (20 + 25),
     * records a checkpoint at barIndex=45, rewinds 50 -> 46 (using cp=45) and 46 -> 35 (dropping cp=45
     * and restoring from cp=20), and matches fresh forward runs bit-for-bit.
     * Defect caught: Untested checkpoint boundary (P1-004), off-by-one bar replay, or leaked future actions.
     */
    expect(CHECKPOINT_INTERVAL).toBe(25);
    const bars = makeBars(60);

    const buildForwardTo = (targetCursor: number) => {
      let s = createReplaySession(bars, "MES", 20);
      if (targetCursor >= 22) {
        while (s.cursorIndex < 22) s = stepReplayForward(s);
        s = openPaperPosition(s, {
          direction: "LONG",
          quantity: 2,
          stopLoss: 4990,
          takeProfit: 5050,
        });
      }
      if (targetCursor >= 35) {
        while (s.cursorIndex < 30) s = stepReplayForward(s);
        s = flattenPaperPosition(s);
        while (s.cursorIndex < 32) s = stepReplayForward(s);
        s = openPaperPosition(s, {
          direction: "SHORT",
          quantity: 1,
          stopLoss: 5030,
          takeProfit: 4980,
        });
      }
      if (targetCursor >= 45) {
        while (s.cursorIndex < 45) s = stepReplayForward(s);
        // Action right on the checkpoint bar 45
        s = flattenPaperPosition(s);
      }
      if (targetCursor >= 48) {
        while (s.cursorIndex < 48) s = stepReplayForward(s);
        s = openPaperPosition(s, {
          direction: "LONG",
          quantity: 1,
          stopLoss: 4995,
          takeProfit: 5060,
        });
      }
      while (s.cursorIndex < targetCursor) {
        s = stepReplayForward(s);
      }
      return s;
    };

    const refAt35 = buildForwardTo(35);
    const refAt46 = buildForwardTo(46);
    let session = buildForwardTo(50);

    expect(session.cursorIndex).toBe(50);
    expect(session.checkpoints.some((cp) => cp.barIndex === 45)).toBe(true);

    // Rewind 50 -> 46 (uses cp=45 + replays action on bar 45 -> steps to 46)
    while (session.cursorIndex > 46) {
      session = stepReplayBack(session);
    }
    expect(session.cursorIndex).toBe(46);
    expect(session.openPosition).toEqual(refAt46.openPosition);
    expect(session.closedTrades).toEqual(refAt46.closedTrades);
    expect(session.realizedPnlDollars).toBe(refAt46.realizedPnlDollars);
    expect(session.actions).toEqual(refAt46.actions);
    expect(session.nextSeq).toBe(refAt46.nextSeq);
    expect(session.nextTradeNum).toBe(refAt46.nextTradeNum);

    // Rewind 46 -> 35 across checkpoint 45 (drops cp=45 and restores from cp=20)
    while (session.cursorIndex > 35) {
      session = stepReplayBack(session);
    }
    expect(session.cursorIndex).toBe(35);
    expect(session.checkpoints.some((cp) => cp.barIndex === 45)).toBe(false);
    expect(session.openPosition).toEqual(refAt35.openPosition);
    expect(session.closedTrades).toEqual(refAt35.closedTrades);
    expect(session.realizedPnlDollars).toBe(refAt35.realizedPnlDollars);
    expect(session.actions).toEqual(refAt35.actions);
    expect(session.nextSeq).toBe(refAt35.nextSeq);
    expect(session.nextTradeNum).toBe(refAt35.nextTradeNum);
  });

  it("preserves replay prefix immutability when trailing provisional bar mutates and new bar arrives (P2-003)", () => {
    /**
     * Contract: registerPaneBars refuses to overwrite already-visited/loaded bars in an active
     * replay session when a trailing provisional bar's OHLC mutates alongside a new bar arrival,
     * preserving F08 rewind determinism and identical P&L.
     * Defect caught: Mutating trailing provisional bar close causing stepBack() validation failure or P&L drift.
     */
    useReplayStore.getState().reset();
    const initialBars: OhlcvBar[] = [
      { time: 1700000000, open: 5000, high: 5001, low: 4999, close: 5000, volume: 10, is_closed: true },
      { time: 1700000300, open: 5000, high: 5002, low: 4999.5, close: 5001, volume: 12, is_closed: false },
    ];
    const target = {
      paneId: "pane-1",
      contractId: "CON.F.US.MES.H26",
      rootSymbol: "MES" as const,
      timeframe: "5m" as const,
      liveMode: false,
    };

    useReplayStore.getState().startReplayAtBar(target, initialBars, 0);
    useReplayStore.getState().stepForward(); // bar 1 (close=5001)
    useReplayStore.getState().openPaperTrade({
      direction: "LONG",
      quantity: 1,
      stopLoss: 4998,
      takeProfit: 5005,
    });

    // Mutate trailing provisional bar close to 4997 (below SL!) AND append bar 2
    const mutatedIncoming: OhlcvBar[] = [
      initialBars[0]!,
      { time: 1700000300, open: 5000, high: 5002, low: 4996, close: 4997, volume: 20, is_closed: true },
      { time: 1700000600, open: 4997, high: 5003, low: 4996.5, close: 5002, volume: 15, is_closed: false },
    ];
    useReplayStore.getState().registerPaneBars("pane-1", mutatedIncoming, target);

    // Original session bar[1] must remain immutable at close=5001
    expect(useReplayStore.getState().session?.bars[1]?.close).toBe(5001);

    // Step back to 0 must succeed cleanly with zero error
    useReplayStore.getState().stepBack();
    const afterBack = useReplayStore.getState().session;
    expect(useReplayStore.getState().error).toBeNull();
    expect(afterBack?.cursorIndex).toBe(0);
    expect(afterBack?.openPosition).toBeNull();
    expect(afterBack?.realizedPnlDollars).toBe(0);
    useReplayStore.getState().reset();
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
