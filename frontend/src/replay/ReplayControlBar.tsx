import { useMemo, useState } from "react";
import {
  INSTRUMENT_SPECS,
  REPLAY_SPEEDS,
  computeReplayStats,
  type ReplayDirection,
  type ReplaySpeed,
} from "./replayEngine";
import { useReplayStore, type ReplayTarget } from "./replayStore";

export interface ReplayControlBarProps {
  activeTarget: ReplayTarget | null;
}

function formatUtcTimestamp(sec: number | undefined): string {
  if (sec === undefined || !Number.isFinite(sec)) {
    return "--";
  }
  const iso = new Date(sec * 1000).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
}

function snapPrice(price: number, tickSize: number): number {
  const str = tickSize.toString();
  const dot = str.indexOf(".");
  const decimals = dot === -1 ? 0 : Math.min(8, str.length - dot - 1);
  return Number((Math.round(price / tickSize) * tickSize).toFixed(decimals));
}

function formatSignedDollars(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

export function ReplayControlBar({ activeTarget }: ReplayControlBarProps) {
  const isOpen = useReplayStore((s) => s.isOpen);
  const pickStartArmed = useReplayStore((s) => s.pickStartArmed);
  const isPlaying = useReplayStore((s) => s.isPlaying);
  const speed = useReplayStore((s) => s.speed);
  const target = useReplayStore((s) => s.target);
  const barsByPane = useReplayStore((s) => s.barsByPane);
  const session = useReplayStore((s) => s.session);
  const error = useReplayStore((s) => s.error);

  const armPickStart = useReplayStore((s) => s.armPickStart);
  const cancelPickStart = useReplayStore((s) => s.cancelPickStart);
  const startReplayAtBar = useReplayStore((s) => s.startReplayAtBar);
  const play = useReplayStore((s) => s.play);
  const pause = useReplayStore((s) => s.pause);
  const setSpeed = useReplayStore((s) => s.setSpeed);
  const stepForward = useReplayStore((s) => s.stepForward);
  const stepBack = useReplayStore((s) => s.stepBack);
  const openPaperTrade = useReplayStore((s) => s.openPaperTrade);
  const flattenPaperTrade = useReplayStore((s) => s.flattenPaperTrade);
  const exitReplay = useReplayStore((s) => s.exitReplay);

  const [qtyInput, setQtyInput] = useState<string>("1");
  const [slTicksInput, setSlTicksInput] = useState<string>("8");
  const [tpTicksInput, setTpTicksInput] = useState<string>("16");
  const [showTrades, setShowTrades] = useState<boolean>(false);

  const stats = useMemo(
    () => (session !== null ? computeReplayStats(session) : null),
    [session],
  );

  if (!isOpen && session === null && !pickStartArmed) {
    return null;
  }

  const activeBars = activeTarget !== null ? (barsByPane[activeTarget.paneId] ?? []) : [];
  const currentBar = session !== null ? session.bars[session.cursorIndex] : undefined;
  const atStart = session === null || session.cursorIndex <= session.startIndex;
  const atEnd = session === null || session.cursorIndex >= session.bars.length - 1;

  const handlePaperOrder = (direction: ReplayDirection) => {
    if (session === null || currentBar === undefined) {
      return;
    }
    const quantity = Number.parseInt(qtyInput, 10);
    const slTicks = Number.parseFloat(slTicksInput);
    const tpTicks = Number.parseFloat(tpTicksInput);
    if (!Number.isFinite(slTicks) || slTicks <= 0 || !Number.isFinite(tpTicks) || tpTicks <= 0) {
      return;
    }
    const tick = INSTRUMENT_SPECS[session.rootSymbol].tickSize;
    const entry = currentBar.close;
    const stopLoss =
      direction === "LONG"
        ? snapPrice(entry - slTicks * tick, tick)
        : snapPrice(entry + slTicks * tick, tick);
    const takeProfit =
      direction === "LONG"
        ? snapPrice(entry + tpTicks * tick, tick)
        : snapPrice(entry - tpTicks * tick, tick);
    openPaperTrade({
      direction,
      quantity: Number.isFinite(quantity) ? quantity : 1,
      stopLoss,
      takeProfit,
    });
  };

  return (
    <section className="replay-bar" aria-label="Bar Replay Controls">
      <div className="replay-bar__row">
        <div className="replay-bar__group">
          <span className="replay-bar__title">Bar Replay</span>
          <button
            type="button"
            className={`terminal-button ${pickStartArmed ? "replay-bar__btn--active" : ""}`}
            onClick={() => {
              if (pickStartArmed) {
                cancelPickStart();
              } else {
                armPickStart();
              }
            }}
          >
            {pickStartArmed ? "Cancel Pick" : "Click Bar to Start"}
          </button>
          <button
            type="button"
            className="terminal-button"
            disabled={activeTarget === null || activeBars.length === 0}
            onClick={() => {
              if (activeTarget !== null && activeBars.length > 0) {
                const defaultStart = Math.max(
                  0,
                  activeBars.length - Math.min(20, Math.floor(activeBars.length / 2)),
                );
                startReplayAtBar(activeTarget, activeBars, defaultStart);
              }
            }}
          >
            Start Replay
          </button>
          {target !== null && (
            <span className="terminal-badge">
              {target.rootSymbol} · {target.timeframe} ({target.paneId})
            </span>
          )}
        </div>

        <div className="replay-bar__group">
          <button
            type="button"
            className="terminal-button"
            disabled={atStart}
            onClick={stepBack}
            aria-label="Step Back"
          >
            -1 Step
          </button>
          <button
            type="button"
            className="terminal-button"
            disabled={session === null || (!isPlaying && atEnd)}
            onClick={() => {
              if (isPlaying) {
                pause();
              } else {
                play();
              }
            }}
            aria-label={isPlaying ? "Pause Replay" : "Play Replay"}
          >
            {isPlaying ? "Pause" : "Play"}
          </button>
          <button
            type="button"
            className="terminal-button"
            disabled={atEnd}
            onClick={stepForward}
            aria-label="Step Forward"
          >
            +1 Step
          </button>
          <select
            aria-label="Replay speed"
            className="terminal-topbar__select"
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value) as ReplaySpeed)}
          >
            {REPLAY_SPEEDS.map((spd) => (
              <option key={spd} value={spd}>
                {spd}x
              </option>
            ))}
          </select>
          <button
            type="button"
            className="terminal-button"
            disabled={session === null && !pickStartArmed}
            onClick={exitReplay}
          >
            Exit Replay
          </button>
        </div>

        <div className="replay-bar__group replay-bar__readout">
          <span>
            {session !== null
              ? `Bar ${session.cursorIndex + 1} / ${session.bars.length}`
              : "No active replay"}
          </span>
          <span>{formatUtcTimestamp(currentBar?.time)}</span>
          {currentBar !== undefined && <span>Close: {currentBar.close.toFixed(2)}</span>}
        </div>
      </div>

      <div className="replay-bar__row replay-bar__paper-row">
        <div className="replay-bar__group">
          <label className="replay-bar__field">
            <span>Qty</span>
            <input
              type="number"
              aria-label="Paper Qty"
              min={1}
              step={1}
              value={qtyInput}
              onChange={(e) => setQtyInput(e.target.value)}
            />
          </label>
          <label className="replay-bar__field">
            <span>SL Ticks</span>
            <input
              type="number"
              aria-label="Paper SL Ticks"
              min={1}
              step={1}
              value={slTicksInput}
              onChange={(e) => setSlTicksInput(e.target.value)}
            />
          </label>
          <label className="replay-bar__field">
            <span>TP Ticks</span>
            <input
              type="number"
              aria-label="Paper TP Ticks"
              min={1}
              step={1}
              value={tpTicksInput}
              onChange={(e) => setTpTicksInput(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="terminal-button replay-bar__btn--buy"
            disabled={session === null || session.openPosition !== null}
            onClick={() => handlePaperOrder("LONG")}
          >
            Buy Market
          </button>
          <button
            type="button"
            className="terminal-button replay-bar__btn--sell"
            disabled={session === null || session.openPosition !== null}
            onClick={() => handlePaperOrder("SHORT")}
          >
            Sell Market
          </button>
          <button
            type="button"
            className="terminal-button"
            disabled={session === null || session.openPosition === null}
            onClick={flattenPaperTrade}
          >
            Flatten
          </button>
        </div>

        {stats !== null && session !== null && (
          <div className="replay-bar__stats">
            <span>
              Open:{" "}
              {session.openPosition !== null
                ? `${session.openPosition.direction} x${session.openPosition.quantity} @ ${session.openPosition.entryPrice.toFixed(2)}`
                : "Flat"}
            </span>
            <span>Unrealized: {formatSignedDollars(stats.unrealizedPnlDollars)}</span>
            <span>Realized: {formatSignedDollars(stats.realizedPnlDollars)}</span>
            <span>Net P&amp;L: {formatSignedDollars(stats.netPnlDollars)}</span>
            <span>
              Trades: {stats.totalTrades} ({stats.winRatePct.toFixed(1)}% win)
            </span>
            {stats.ambiguousFillCount > 0 && (
              <span className="chart-pane__quality-warning">
                Ambiguous: {stats.ambiguousFillCount}
              </span>
            )}
            <button
              type="button"
              className="terminal-button"
              onClick={() => setShowTrades((prev) => !prev)}
            >
              {showTrades ? "Hide Trades" : `Trades (${session.closedTrades.length})`}
            </button>
          </div>
        )}
      </div>

      {error !== null && (
        <div className="login-error" role="alert">
          {error}
        </div>
      )}

      {showTrades && session !== null && session.closedTrades.length > 0 && (
        <div className="replay-bar__trades">
          <table className="backtest-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Dir</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Ticks</th>
                <th>P&amp;L</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {session.closedTrades.map((t) => (
                <tr key={t.tradeId}>
                  <td>{t.tradeId}</td>
                  <td>{t.direction}</td>
                  <td>{t.entryPrice.toFixed(2)}</td>
                  <td>{t.exitPrice.toFixed(2)}</td>
                  <td>{t.pnlTicks}</td>
                  <td>{formatSignedDollars(t.pnlDollars)}</td>
                  <td>
                    {t.exitReason}
                    {t.ambiguousFill && (
                      <span className="backtest-badge backtest-badge--warn">AMBIGUOUS</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
