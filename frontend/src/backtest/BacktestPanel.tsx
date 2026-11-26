import { useEffect, useState, type FormEvent } from "react";
import type { RootSymbol, Timeframe } from "../api/types";
import { useReplayStore } from "../replay/replayStore";
import { useBacktestStore } from "./backtestStore";

export interface BacktestPaneTarget {
  paneId: string;
  contractId: string | null;
  rootSymbol: RootSymbol;
  timeframe: Timeframe;
  liveMode: boolean;
}

export interface BacktestPanelProps {
  activeTarget: BacktestPaneTarget | null;
}

function toUtcDateTimeLocalValue(sec: number): string {
  return new Date(sec * 1000).toISOString().slice(0, 16);
}

function parseUtcDateTimeLocalValue(raw: string): number | null {
  const cleaned = raw.trim();
  if (!cleaned) {
    return null;
  }
  const iso = cleaned.endsWith("Z") ? cleaned : `${cleaned}:00Z`;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) {
    return null;
  }
  return Math.floor(ms / 1000);
}

function formatSignedDollars(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

export function BacktestPanel({ activeTarget }: BacktestPanelProps) {
  const isOpen = useBacktestStore((s) => s.isOpen);
  const strategies = useBacktestStore((s) => s.strategies);
  const strategiesLoading = useBacktestStore((s) => s.strategiesLoading);
  const activeJob = useBacktestStore((s) => s.activeJob);
  const submitting = useBacktestStore((s) => s.submitting);
  const polling = useBacktestStore((s) => s.polling);
  const error = useBacktestStore((s) => s.error);
  const showZones = useBacktestStore((s) => s.showZones);
  const showTradeLines = useBacktestStore((s) => s.showTradeLines);
  const showMarkers = useBacktestStore((s) => s.showMarkers);
  const showIndicators = useBacktestStore((s) => s.showIndicators);

  const togglePanel = useBacktestStore((s) => s.togglePanel);
  const loadStrategies = useBacktestStore((s) => s.loadStrategies);
  const runBacktest = useBacktestStore((s) => s.runBacktest);
  const cancelActiveJob = useBacktestStore((s) => s.cancelActiveJob);
  const toggleOverlayFlag = useBacktestStore((s) => s.toggleOverlayFlag);
  const clearResult = useBacktestStore((s) => s.clearResult);

  const paneBars = useReplayStore((s) =>
    activeTarget !== null ? s.barsByPane[activeTarget.paneId] : undefined,
  );

  const [strategyId, setStrategyId] = useState<string>("ict_fvg_sweep");
  const [startInput, setStartInput] = useState<string>("");
  const [endInput, setEndInput] = useState<string>("");
  const [rangeSeededKey, setRangeSeededKey] = useState<string>("");
  const [localError, setLocalError] = useState<string | null>(null);

  const [swingLookback, setSwingLookback] = useState<string>("5");
  const [sweepWindowBars, setSweepWindowBars] = useState<string>("5");
  const [minFvgTicks, setMinFvgTicks] = useState<string>("1");
  const [riskReward, setRiskReward] = useState<string>("2");
  const [slBufferTicks, setSlBufferTicks] = useState<string>("1");
  const [entryMode, setEntryMode] = useState<"market" | "fvg_ce">("market");
  const [maxWaitBars, setMaxWaitBars] = useState<string>("5");
  const [quantity, setQuantity] = useState<string>("1");
  const [emaPeriod, setEmaPeriod] = useState<string>("20");
  const [includeVwap, setIncludeVwap] = useState<boolean>(true);

  useEffect(() => {
    if (isOpen && strategies.length === 0 && !strategiesLoading) {
      void loadStrategies();
    }
  }, [isOpen, loadStrategies, strategies.length, strategiesLoading]);

  const targetKey =
    activeTarget !== null
      ? `${activeTarget.paneId}:${activeTarget.contractId ?? ""}:${activeTarget.timeframe}`
      : "";

  useEffect(() => {
    if (!isOpen || activeTarget === null) {
      return;
    }
    if (rangeSeededKey === targetKey && startInput && endInput) {
      return;
    }
    if (paneBars !== undefined && paneBars.length > 0) {
      const first = paneBars[0]!.time;
      const last = paneBars[paneBars.length - 1]!.time;
      setStartInput(toUtcDateTimeLocalValue(first));
      setEndInput(toUtcDateTimeLocalValue(last));
      setRangeSeededKey(targetKey);
    } else if (!startInput || !endInput) {
      const nowSec = Math.floor(Date.now() / 1000);
      setStartInput(toUtcDateTimeLocalValue(nowSec - 7 * 86400));
      setEndInput(toUtcDateTimeLocalValue(nowSec));
      setRangeSeededKey(targetKey);
    }
  }, [activeTarget, endInput, isOpen, paneBars, rangeSeededKey, startInput, targetKey]);

  if (!isOpen) {
    return null;
  }

  const canCancel =
    activeJob !== null && (activeJob.status === "QUEUED" || activeJob.status === "RUNNING");
  const isBusy = submitting || polling || canCancel;
  const result = activeJob?.status === "COMPLETED" ? activeJob.result : null;
  const summary = result?.summary ?? activeJob?.summary ?? null;

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError(null);
    if (activeTarget === null || !activeTarget.contractId) {
      setLocalError("Select a pane with a resolved contract before running a backtest");
      return;
    }
    const startSec = parseUtcDateTimeLocalValue(startInput);
    const endSec = parseUtcDateTimeLocalValue(endInput);
    if (startSec === null || endSec === null || startSec >= endSec) {
      setLocalError("Provide a valid UTC start and end time (start < end)");
      return;
    }

    const params: Record<string, unknown> = {
      swing_lookback: Number.parseInt(swingLookback, 10),
      sweep_window_bars: Number.parseInt(sweepWindowBars, 10),
      min_fvg_ticks: Number.parseFloat(minFvgTicks),
      risk_reward: Number.parseFloat(riskReward),
      sl_buffer_ticks: Number.parseFloat(slBufferTicks),
      entry_mode: entryMode,
      max_wait_bars: Number.parseInt(maxWaitBars, 10),
      quantity: Number.parseInt(quantity, 10),
      ema_period: Number.parseInt(emaPeriod, 10),
      include_vwap: includeVwap,
    };

    void runBacktest({
      contract_id: activeTarget.contractId,
      root_symbol: activeTarget.rootSymbol,
      timeframe: activeTarget.timeframe,
      live_mode: activeTarget.liveMode,
      start_time: startSec,
      end_time: endSec,
      strategy_id: strategyId,
      params,
    });
  };

  return (
    <aside className="backtest-drawer" aria-label="Backtest Runner">
      <div className="backtest-drawer__header">
        <div>
          <strong>Backtest Runner</strong>
          {activeTarget !== null && (
            <span className="terminal-badge">
              {activeTarget.rootSymbol} · {activeTarget.timeframe} ·{" "}
              {activeTarget.contractId ?? "Unresolved"}
            </span>
          )}
        </div>
        <button type="button" className="terminal-button" onClick={togglePanel}>
          Close
        </button>
      </div>

      <form className="backtest-drawer__form" onSubmit={handleSubmit}>
        <label className="backtest-field">
          <span>Strategy</span>
          <select
            aria-label="Backtest strategy"
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
          >
            {strategies.length > 0 ? (
              strategies.map((st) => (
                <option key={st.strategy_id} value={st.strategy_id}>
                  {st.name}
                </option>
              ))
            ) : (
              <option value="ict_fvg_sweep">ICT FVG + Liquidity Sweep</option>
            )}
          </select>
        </label>

        <div className="backtest-grid-2">
          <label className="backtest-field">
            <span>Start (UTC)</span>
            <input
              type="datetime-local"
              aria-label="Backtest start UTC"
              value={startInput}
              onChange={(e) => setStartInput(e.target.value)}
              required
            />
          </label>
          <label className="backtest-field">
            <span>End (UTC)</span>
            <input
              type="datetime-local"
              aria-label="Backtest end UTC"
              value={endInput}
              onChange={(e) => setEndInput(e.target.value)}
              required
            />
          </label>
        </div>

        <div className="backtest-grid-2">
          <label className="backtest-field">
            <span>Swing Lookback</span>
            <input
              type="number"
              aria-label="Swing lookback"
              min={2}
              max={100}
              value={swingLookback}
              onChange={(e) => setSwingLookback(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>Sweep Window</span>
            <input
              type="number"
              aria-label="Sweep window bars"
              min={0}
              max={50}
              value={sweepWindowBars}
              onChange={(e) => setSweepWindowBars(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>Min FVG Ticks</span>
            <input
              type="number"
              aria-label="Min FVG ticks"
              min={0}
              step={0.5}
              value={minFvgTicks}
              onChange={(e) => setMinFvgTicks(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>Risk / Reward</span>
            <input
              type="number"
              aria-label="Risk reward"
              min={0.5}
              step={0.5}
              value={riskReward}
              onChange={(e) => setRiskReward(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>SL Buffer Ticks</span>
            <input
              type="number"
              aria-label="SL buffer ticks"
              min={0}
              step={0.5}
              value={slBufferTicks}
              onChange={(e) => setSlBufferTicks(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>Entry Mode</span>
            <select
              aria-label="Entry mode"
              value={entryMode}
              onChange={(e) => setEntryMode(e.target.value as "market" | "fvg_ce")}
            >
              <option value="market">market</option>
              <option value="fvg_ce">fvg_ce</option>
            </select>
          </label>
          <label className="backtest-field">
            <span>Max Wait Bars</span>
            <input
              type="number"
              aria-label="Max wait bars"
              min={1}
              max={50}
              value={maxWaitBars}
              onChange={(e) => setMaxWaitBars(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>Quantity</span>
            <input
              type="number"
              aria-label="Backtest quantity"
              min={1}
              max={100}
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
            />
          </label>
          <label className="backtest-field">
            <span>EMA Period</span>
            <input
              type="number"
              aria-label="EMA period"
              min={2}
              max={200}
              value={emaPeriod}
              onChange={(e) => setEmaPeriod(e.target.value)}
            />
          </label>
          <label className="layout-picker__checkbox">
            <input
              type="checkbox"
              checked={includeVwap}
              onChange={(e) => setIncludeVwap(e.target.checked)}
            />
            <span>Include VWAP</span>
          </label>
        </div>

        <div className="backtest-drawer__actions">
          <button
            type="submit"
            className="terminal-button backtest-run-btn"
            disabled={isBusy || activeTarget === null || !activeTarget.contractId}
          >
            {submitting ? "Submitting..." : polling ? "Running..." : "Run Backtest"}
          </button>
          {canCancel && (
            <button
              type="button"
              className="terminal-button"
              onClick={() => void cancelActiveJob()}
            >
              Cancel
            </button>
          )}
          {activeJob !== null && !canCancel && (
            <button type="button" className="terminal-button" onClick={clearResult}>
              Clear
            </button>
          )}
        </div>
      </form>

      {(localError ?? error) !== null && (
        <div className="login-error" role="alert">
          {localError ?? error}
        </div>
      )}

      {activeJob !== null && (
        <div className="backtest-status-row">
          <span>Job: {activeJob.job_id.slice(0, 8)}</span>
          <span className={`backtest-badge backtest-badge--${activeJob.status.toLowerCase()}`}>
            {activeJob.status}
          </span>
        </div>
      )}

      <div className="backtest-toggles">
        <button
          type="button"
          className={`sync-controls__toggle ${showZones ? "sync-controls__toggle--active" : ""}`}
          aria-pressed={showZones}
          onClick={() => toggleOverlayFlag("showZones")}
        >
          Zones
        </button>
        <button
          type="button"
          className={`sync-controls__toggle ${showTradeLines ? "sync-controls__toggle--active" : ""}`}
          aria-pressed={showTradeLines}
          onClick={() => toggleOverlayFlag("showTradeLines")}
        >
          Trade Lines
        </button>
        <button
          type="button"
          className={`sync-controls__toggle ${showMarkers ? "sync-controls__toggle--active" : ""}`}
          aria-pressed={showMarkers}
          onClick={() => toggleOverlayFlag("showMarkers")}
        >
          Markers
        </button>
        <button
          type="button"
          className={`sync-controls__toggle ${showIndicators ? "sync-controls__toggle--active" : ""}`}
          aria-pressed={showIndicators}
          onClick={() => toggleOverlayFlag("showIndicators")}
        >
          Indicators
        </button>
      </div>

      {summary !== null && (
        <div className="backtest-metrics">
          <div className="backtest-metric">
            <span>Net Profit</span>
            <strong>{formatSignedDollars(summary.net_profit_dollars)}</strong>
          </div>
          <div className="backtest-metric">
            <span>Winrate</span>
            <strong>{summary.win_rate_pct.toFixed(1)}%</strong>
          </div>
          <div className="backtest-metric">
            <span>Profit Factor</span>
            <strong>
              {summary.profit_factor !== null ? summary.profit_factor.toFixed(2) : "--"}
            </strong>
          </div>
          <div className="backtest-metric">
            <span>Max Drawdown</span>
            <strong>${summary.max_drawdown_dollars.toFixed(2)}</strong>
          </div>
          <div className="backtest-metric">
            <span>Total Trades</span>
            <strong>{summary.total_trades}</strong>
          </div>
          <div className="backtest-metric">
            <span>Win / Loss</span>
            <strong>
              {summary.winning_trades} / {summary.losing_trades}
            </strong>
          </div>
          <div className="backtest-metric">
            <span>Ambiguous Fills</span>
            <strong>{summary.ambiguous_fill_count}</strong>
          </div>
        </div>
      )}

      {result !== null && (
        <div className="backtest-trades-wrap">
          {result.trades.length === 0 ? (
            <p className="backtest-empty">No trades generated for this window.</p>
          ) : (
            <table className="backtest-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Dir</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Ticks</th>
                  <th>P&amp;L</th>
                  <th>Exit</th>
                </tr>
              </thead>
              <tbody>
                {result.trades.map((trade) => (
                  <tr key={trade.trade_id}>
                    <td>{trade.trade_id}</td>
                    <td>{trade.direction}</td>
                    <td>{trade.entry_price.toFixed(2)}</td>
                    <td>{trade.exit_price.toFixed(2)}</td>
                    <td>{trade.pnl_ticks}</td>
                    <td>{formatSignedDollars(trade.pnl_dollars)}</td>
                    <td>
                      {trade.exit_reason}
                      {trade.ambiguous_fill && (
                        <span className="backtest-badge backtest-badge--warn">AMBIGUOUS</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </aside>
  );
}
