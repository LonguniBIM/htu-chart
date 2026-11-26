import type {
  BacktestIndicatorSeries,
  BacktestMarker,
  BacktestTradeLine,
  BacktestZone,
  DrawingRecord,
  OhlcvBar,
  RootSymbol,
} from "../api/types";
import { INSTRUMENT_SPECS } from "../drawings/types";

export { INSTRUMENT_SPECS };

export const CHECKPOINT_INTERVAL = 25;
export const REPLAY_SPEEDS = [0.5, 1, 2, 5, 10] as const;
export type ReplaySpeed = (typeof REPLAY_SPEEDS)[number];

export type ReplayDirection = "LONG" | "SHORT";
export type ReplayExitReason = "stop_loss" | "take_profit" | "manual_close";

export type ReplayAction =
  | {
      seq: number;
      barIndex: number;
      type: "OPEN_LONG" | "OPEN_SHORT";
      quantity: number;
      stopLoss: number;
      takeProfit: number;
    }
  | {
      seq: number;
      barIndex: number;
      type: "CLOSE_POSITION";
    };

export interface ReplayPosition {
  tradeId: string;
  direction: ReplayDirection;
  entryBarIndex: number;
  entryTime: number;
  entryPrice: number;
  quantity: number;
  stopLoss: number;
  takeProfit: number;
}

export interface ReplayClosedTrade {
  tradeId: string;
  direction: ReplayDirection;
  entryBarIndex: number;
  entryTime: number;
  entryPrice: number;
  exitBarIndex: number;
  exitTime: number;
  exitPrice: number;
  quantity: number;
  stopLoss: number;
  takeProfit: number;
  pnlTicks: number;
  pnlDollars: number;
  exitReason: ReplayExitReason;
  ambiguousFill: boolean;
}

export interface ReplayCheckpoint {
  barIndex: number;
  nextSeq: number;
  nextTradeNum: number;
  openPosition: ReplayPosition | null;
  closedTrades: readonly ReplayClosedTrade[];
  realizedPnlDollars: number;
}

export interface ReplayStats {
  realizedPnlDollars: number;
  unrealizedPnlDollars: number;
  netPnlDollars: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRatePct: number;
  ambiguousFillCount: number;
}

export interface ReplaySession {
  rootSymbol: RootSymbol;
  bars: readonly OhlcvBar[];
  startIndex: number;
  cursorIndex: number;
  nextSeq: number;
  nextTradeNum: number;
  openPosition: ReplayPosition | null;
  closedTrades: readonly ReplayClosedTrade[];
  realizedPnlDollars: number;
  actions: readonly ReplayAction[];
  checkpoints: readonly ReplayCheckpoint[];
}

export interface OpenPaperPositionInput {
  direction: ReplayDirection;
  quantity: number;
  stopLoss: number;
  takeProfit: number;
}

function roundDollars(value: number): number {
  return Number(value.toFixed(2));
}

export function computePaperTradePnl(
  rootSymbol: RootSymbol,
  direction: ReplayDirection,
  entryPrice: number,
  exitPrice: number,
  quantity: number,
): { pnlTicks: number; pnlDollars: number } {
  const spec = INSTRUMENT_SPECS[rootSymbol];
  const entryTicks = Math.round(entryPrice / spec.tickSize);
  const exitTicks = Math.round(exitPrice / spec.tickSize);
  const pnlTicks = direction === "LONG" ? exitTicks - entryTicks : entryTicks - exitTicks;
  const pnlDollars = roundDollars(pnlTicks * spec.tickValue * quantity);
  return { pnlTicks, pnlDollars };
}

function clonePosition(pos: ReplayPosition | null): ReplayPosition | null {
  return pos === null ? null : { ...pos };
}

function cloneClosedTrades(trades: readonly ReplayClosedTrade[]): ReplayClosedTrade[] {
  return trades.map((t) => ({ ...t }));
}

function makeCheckpoint(
  barIndex: number,
  nextSeq: number,
  nextTradeNum: number,
  openPosition: ReplayPosition | null,
  closedTrades: readonly ReplayClosedTrade[],
  realizedPnlDollars: number,
): ReplayCheckpoint {
  return {
    barIndex,
    nextSeq,
    nextTradeNum,
    openPosition: clonePosition(openPosition),
    closedTrades: cloneClosedTrades(closedTrades),
    realizedPnlDollars,
  };
}

function shouldRecordCheckpoint(startIndex: number, barIndex: number): boolean {
  return barIndex > startIndex && (barIndex - startIndex) % CHECKPOINT_INTERVAL === 0;
}

function evaluatePositionExitOnBar(
  pos: ReplayPosition,
  bar: OhlcvBar,
): { exitPrice: number; exitReason: "stop_loss" | "take_profit"; ambiguousFill: boolean } | null {
  if (pos.direction === "LONG") {
    const slHit = bar.low <= pos.stopLoss;
    const tpHit = bar.high >= pos.takeProfit;
    if (slHit && tpHit) {
      return {
        exitPrice: bar.open <= pos.stopLoss ? bar.open : pos.stopLoss,
        exitReason: "stop_loss",
        ambiguousFill: true,
      };
    }
    if (slHit) {
      return {
        exitPrice: bar.open <= pos.stopLoss ? bar.open : pos.stopLoss,
        exitReason: "stop_loss",
        ambiguousFill: false,
      };
    }
    if (tpHit) {
      return {
        exitPrice: bar.open >= pos.takeProfit ? bar.open : pos.takeProfit,
        exitReason: "take_profit",
        ambiguousFill: false,
      };
    }
    return null;
  }

  const slHit = bar.high >= pos.stopLoss;
  const tpHit = bar.low <= pos.takeProfit;
  if (slHit && tpHit) {
    return {
      exitPrice: bar.open >= pos.stopLoss ? bar.open : pos.stopLoss,
      exitReason: "stop_loss",
      ambiguousFill: true,
    };
  }
  if (slHit) {
    return {
      exitPrice: bar.open >= pos.stopLoss ? bar.open : pos.stopLoss,
      exitReason: "stop_loss",
      ambiguousFill: false,
    };
  }
  if (tpHit) {
    return {
      exitPrice: bar.open <= pos.takeProfit ? bar.open : pos.takeProfit,
      exitReason: "take_profit",
      ambiguousFill: false,
    };
  }
  return null;
}

function validatePaperEntry(
  entryPrice: number,
  input: OpenPaperPositionInput,
): void {
  if (!Number.isInteger(input.quantity) || input.quantity < 1) {
    throw new Error("Quantity must be an integer >= 1");
  }
  if (
    !Number.isFinite(input.stopLoss) ||
    input.stopLoss <= 0 ||
    !Number.isFinite(input.takeProfit) ||
    input.takeProfit <= 0
  ) {
    throw new Error("Stop loss and take profit must be positive finite numbers");
  }
  if (input.direction === "LONG") {
    if (!(input.stopLoss < entryPrice && entryPrice < input.takeProfit)) {
      throw new Error(
        `LONG paper position requires stopLoss (${input.stopLoss}) < entry (${entryPrice}) < takeProfit (${input.takeProfit})`,
      );
    }
    return;
  }
  if (!(input.takeProfit < entryPrice && entryPrice < input.stopLoss)) {
    throw new Error(
      `SHORT paper position requires takeProfit (${input.takeProfit}) < entry (${entryPrice}) < stopLoss (${input.stopLoss})`,
    );
  }
}

export function createReplaySession(
  bars: readonly OhlcvBar[],
  rootSymbol: RootSymbol,
  startIndex = 0,
): ReplaySession {
  if (bars.length === 0) {
    throw new Error("Cannot start replay with zero bars");
  }
  const clampedStart = Math.max(0, Math.min(Math.trunc(startIndex), bars.length - 1));
  const initialCp = makeCheckpoint(clampedStart, 1, 1, null, [], 0);
  return {
    rootSymbol,
    bars: [...bars],
    startIndex: clampedStart,
    cursorIndex: clampedStart,
    nextSeq: 1,
    nextTradeNum: 1,
    openPosition: null,
    closedTrades: [],
    realizedPnlDollars: 0,
    actions: [],
    checkpoints: [initialCp],
  };
}

export function stepReplayForward(session: ReplaySession): ReplaySession {
  if (session.cursorIndex >= session.bars.length - 1) {
    return session;
  }
  const nextIdx = session.cursorIndex + 1;
  const bar = session.bars[nextIdx]!;

  let nextOpenPosition = clonePosition(session.openPosition);
  let nextClosedTrades = session.closedTrades;
  let nextRealizedPnl = session.realizedPnlDollars;

  if (nextOpenPosition !== null) {
    const exitOutcome = evaluatePositionExitOnBar(nextOpenPosition, bar);
    if (exitOutcome !== null) {
      const { pnlTicks, pnlDollars } = computePaperTradePnl(
        session.rootSymbol,
        nextOpenPosition.direction,
        nextOpenPosition.entryPrice,
        exitOutcome.exitPrice,
        nextOpenPosition.quantity,
      );
      const closed: ReplayClosedTrade = {
        tradeId: nextOpenPosition.tradeId,
        direction: nextOpenPosition.direction,
        entryBarIndex: nextOpenPosition.entryBarIndex,
        entryTime: nextOpenPosition.entryTime,
        entryPrice: nextOpenPosition.entryPrice,
        exitBarIndex: nextIdx,
        exitTime: bar.time,
        exitPrice: exitOutcome.exitPrice,
        quantity: nextOpenPosition.quantity,
        stopLoss: nextOpenPosition.stopLoss,
        takeProfit: nextOpenPosition.takeProfit,
        pnlTicks,
        pnlDollars,
        exitReason: exitOutcome.exitReason,
        ambiguousFill: exitOutcome.ambiguousFill,
      };
      nextClosedTrades = [...session.closedTrades, closed];
      nextRealizedPnl = roundDollars(session.realizedPnlDollars + pnlDollars);
      nextOpenPosition = null;
    }
  }

  let nextCheckpoints = session.checkpoints;
  if (
    shouldRecordCheckpoint(session.startIndex, nextIdx) &&
    !session.checkpoints.some((cp) => cp.barIndex === nextIdx)
  ) {
    nextCheckpoints = [
      ...session.checkpoints,
      makeCheckpoint(
        nextIdx,
        session.nextSeq,
        session.nextTradeNum,
        nextOpenPosition,
        nextClosedTrades,
        nextRealizedPnl,
      ),
    ];
  }

  return {
    ...session,
    cursorIndex: nextIdx,
    openPosition: nextOpenPosition,
    closedTrades: nextClosedTrades,
    realizedPnlDollars: nextRealizedPnl,
    checkpoints: nextCheckpoints,
  };
}

function applyActionAtCursor(
  session: ReplaySession,
  action: ReplayAction,
): ReplaySession {
  const bar = session.bars[session.cursorIndex]!;
  if (action.type === "CLOSE_POSITION") {
    if (session.openPosition === null) {
      throw new Error("No open paper position to flatten");
    }
    const pos = session.openPosition;
    const exitPrice = bar.close;
    const { pnlTicks, pnlDollars } = computePaperTradePnl(
      session.rootSymbol,
      pos.direction,
      pos.entryPrice,
      exitPrice,
      pos.quantity,
    );
    const closed: ReplayClosedTrade = {
      tradeId: pos.tradeId,
      direction: pos.direction,
      entryBarIndex: pos.entryBarIndex,
      entryTime: pos.entryTime,
      entryPrice: pos.entryPrice,
      exitBarIndex: session.cursorIndex,
      exitTime: bar.time,
      exitPrice,
      quantity: pos.quantity,
      stopLoss: pos.stopLoss,
      takeProfit: pos.takeProfit,
      pnlTicks,
      pnlDollars,
      exitReason: "manual_close",
      ambiguousFill: false,
    };
    return {
      ...session,
      nextSeq: action.seq + 1,
      openPosition: null,
      closedTrades: [...session.closedTrades, closed],
      realizedPnlDollars: roundDollars(session.realizedPnlDollars + pnlDollars),
      actions: [...session.actions, action],
    };
  }

  if (session.openPosition !== null) {
    throw new Error("A paper position is already open; flatten before opening another");
  }
  const direction: ReplayDirection = action.type === "OPEN_LONG" ? "LONG" : "SHORT";
  const entryPrice = bar.close;
  validatePaperEntry(entryPrice, {
    direction,
    quantity: action.quantity,
    stopLoss: action.stopLoss,
    takeProfit: action.takeProfit,
  });

  const openPosition: ReplayPosition = {
    tradeId: `R${session.nextTradeNum}`,
    direction,
    entryBarIndex: session.cursorIndex,
    entryTime: bar.time,
    entryPrice,
    quantity: action.quantity,
    stopLoss: action.stopLoss,
    takeProfit: action.takeProfit,
  };

  return {
    ...session,
    nextSeq: action.seq + 1,
    nextTradeNum: session.nextTradeNum + 1,
    openPosition,
    actions: [...session.actions, action],
  };
}

export function rebuildReplayStateAtCursor(
  bars: readonly OhlcvBar[],
  rootSymbol: RootSymbol,
  startIndex: number,
  targetCursorIndex: number,
  actions: readonly ReplayAction[],
  checkpoints: readonly ReplayCheckpoint[],
): ReplaySession {
  if (bars.length === 0) {
    throw new Error("Cannot rebuild replay state with zero bars");
  }
  const clampedStart = Math.max(0, Math.min(Math.trunc(startIndex), bars.length - 1));
  const clampedTarget = Math.max(
    clampedStart,
    Math.min(Math.trunc(targetCursorIndex), bars.length - 1),
  );

  const validCheckpoints = checkpoints
    .filter((cp) => cp.barIndex >= clampedStart && cp.barIndex <= clampedTarget)
    .sort((a, b) => a.barIndex - b.barIndex);
  const nearestCp = validCheckpoints[validCheckpoints.length - 1];

  const validActions = actions
    .filter((a) => a.barIndex >= clampedStart && a.barIndex <= clampedTarget)
    .sort((a, b) => a.seq - b.seq);

  const actionsByBar = new Map<number, ReplayAction[]>();
  for (const act of validActions) {
    const list = actionsByBar.get(act.barIndex);
    if (list !== undefined) {
      list.push(act);
    } else {
      actionsByBar.set(act.barIndex, [act]);
    }
  }

  let rebuilt: ReplaySession;
  let resumeBarIndex: number;
  if (nearestCp !== undefined) {
    resumeBarIndex = nearestCp.barIndex;
    rebuilt = {
      rootSymbol,
      bars: [...bars],
      startIndex: clampedStart,
      cursorIndex: nearestCp.barIndex,
      nextSeq: nearestCp.nextSeq,
      nextTradeNum: nearestCp.nextTradeNum,
      openPosition: clonePosition(nearestCp.openPosition),
      closedTrades: cloneClosedTrades(nearestCp.closedTrades),
      realizedPnlDollars: nearestCp.realizedPnlDollars,
      actions: validActions.filter(
        (a) => a.barIndex < nearestCp.barIndex || a.seq < nearestCp.nextSeq,
      ),
      checkpoints: validCheckpoints.map((cp) =>
        makeCheckpoint(
          cp.barIndex,
          cp.nextSeq,
          cp.nextTradeNum,
          cp.openPosition,
          cp.closedTrades,
          cp.realizedPnlDollars,
        ),
      ),
    };
  } else {
    resumeBarIndex = clampedStart;
    rebuilt = createReplaySession(bars, rootSymbol, clampedStart);
  }

  const startBarActions = (actionsByBar.get(resumeBarIndex) ?? []).filter(
    (a) => a.seq >= rebuilt.nextSeq,
  );
  for (const act of startBarActions) {
    rebuilt = applyActionAtCursor(rebuilt, act);
  }

  for (let barIdx = resumeBarIndex + 1; barIdx <= clampedTarget; barIdx += 1) {
    rebuilt = stepReplayForward(rebuilt);
    const barActions = actionsByBar.get(barIdx) ?? [];
    for (const act of barActions) {
      rebuilt = applyActionAtCursor(rebuilt, act);
    }
  }

  return rebuilt;
}

export function stepReplayBack(session: ReplaySession): ReplaySession {
  if (session.cursorIndex <= session.startIndex) {
    return session;
  }
  const nextCursorIndex = session.cursorIndex - 1;
  const prunedActions = session.actions.filter((a) => a.barIndex <= nextCursorIndex);
  const prunedCheckpoints = session.checkpoints.filter((cp) => cp.barIndex <= nextCursorIndex);
  return rebuildReplayStateAtCursor(
    session.bars,
    session.rootSymbol,
    session.startIndex,
    nextCursorIndex,
    prunedActions,
    prunedCheckpoints,
  );
}

export function openPaperPosition(
  session: ReplaySession,
  input: OpenPaperPositionInput,
): ReplaySession {
  const action: ReplayAction = {
    seq: session.nextSeq,
    barIndex: session.cursorIndex,
    type: input.direction === "LONG" ? "OPEN_LONG" : "OPEN_SHORT",
    quantity: input.quantity,
    stopLoss: input.stopLoss,
    takeProfit: input.takeProfit,
  };
  return applyActionAtCursor(session, action);
}

export function flattenPaperPosition(session: ReplaySession): ReplaySession {
  const action: ReplayAction = {
    seq: session.nextSeq,
    barIndex: session.cursorIndex,
    type: "CLOSE_POSITION",
  };
  return applyActionAtCursor(session, action);
}

export function computeReplayStats(session: ReplaySession): ReplayStats {
  const currentBar = session.bars[session.cursorIndex];
  const unrealizedPnlDollars =
    session.openPosition !== null && currentBar !== undefined
      ? computePaperTradePnl(
          session.rootSymbol,
          session.openPosition.direction,
          session.openPosition.entryPrice,
          currentBar.close,
          session.openPosition.quantity,
        ).pnlDollars
      : 0;

  let winningTrades = 0;
  let losingTrades = 0;
  let ambiguousFillCount = 0;
  for (const trade of session.closedTrades) {
    if (trade.pnlDollars > 0) {
      winningTrades += 1;
    } else if (trade.pnlDollars < 0) {
      losingTrades += 1;
    }
    if (trade.ambiguousFill) {
      ambiguousFillCount += 1;
    }
  }

  const totalTrades = session.closedTrades.length;
  const winRatePct = totalTrades > 0 ? Number(((winningTrades / totalTrades) * 100).toFixed(2)) : 0;
  const realizedPnlDollars = roundDollars(session.realizedPnlDollars);
  const netPnlDollars = roundDollars(realizedPnlDollars + unrealizedPnlDollars);

  return {
    realizedPnlDollars,
    unrealizedPnlDollars,
    netPnlDollars,
    totalTrades,
    winningTrades,
    losingTrades,
    winRatePct,
    ambiguousFillCount,
  };
}

export function filterBarsByReplayCursor(
  bars: readonly OhlcvBar[],
  cursorTime: number,
): OhlcvBar[] {
  return bars.filter((bar) => bar.time <= cursorTime);
}

export function filterZonesByReplayCursor(
  zones: readonly BacktestZone[],
  cursorTime: number,
): BacktestZone[] {
  return zones.filter((zone) => zone.detected_at_time <= cursorTime);
}

export function filterMarkersByReplayCursor(
  markers: readonly BacktestMarker[],
  cursorTime: number,
): BacktestMarker[] {
  return markers.filter((marker) => marker.time <= cursorTime);
}

export function filterTradeLinesByReplayCursor(
  lines: readonly BacktestTradeLine[],
  cursorTime: number,
): BacktestTradeLine[] {
  return lines.filter((line) => line.entry_time <= cursorTime && line.exit_time <= cursorTime);
}

export function filterIndicatorsByReplayCursor(
  indicators: readonly BacktestIndicatorSeries[],
  cursorTime: number,
): BacktestIndicatorSeries[] {
  return indicators.map((series) => ({
    ...series,
    points: series.points.filter((pt) => pt.time <= cursorTime),
  }));
}

export function filterDrawingsByReplayCursor(
  drawings: readonly DrawingRecord[],
  cursorTime: number,
): DrawingRecord[] {
  return drawings.filter((drawing) => {
    const detectedAt = drawing.metadata?.detected_at_time;
    if (typeof detectedAt === "number" && Number.isFinite(detectedAt)) {
      return detectedAt <= cursorTime;
    }
    const anchors = drawing.coordinates.anchors;
    return anchors.length > 0 && anchors.every((anchor) => anchor.time <= cursorTime);
  });
}
