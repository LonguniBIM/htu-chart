"""Deterministic strictly causal F08 backtest engine (F04, F08, ARCH-001, ARCH-005)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from app.engine.instruments import (
    InstrumentSpec,
    get_instrument_spec,
    validate_root_symbol,
)
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import normalize_timeframe
from app.engine.strategies import create_strategy
from app.engine.strategy_base import (
    BaseStrategy,
    CausalBarView,
    IndicatorSeries,
    StrategyContext,
    StrategySignal,
    StrategyZone,
)

ExitReason = Literal["stop_loss", "take_profit", "end_of_data"]


@dataclass(frozen=True)
class BacktestTrade:
    """Closed simulated trade record with exact CME tick/dollar P&L and F08 flags."""

    trade_id: str
    direction: Literal["LONG", "SHORT"]
    signal_time: int
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    stop_loss: float
    take_profit: float
    quantity: int
    pnl_ticks: float
    pnl_dollars: float
    exit_reason: ExitReason
    ambiguous_fill: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize BacktestTrade to a JSON-safe dictionary."""
        return {
            "trade_id": self.trade_id,
            "direction": self.direction,
            "signal_time": self.signal_time,
            "entry_time": self.entry_time,
            "entry_price": float(self.entry_price),
            "exit_time": self.exit_time,
            "exit_price": float(self.exit_price),
            "stop_loss": float(self.stop_loss),
            "take_profit": float(self.take_profit),
            "quantity": self.quantity,
            "pnl_ticks": float(self.pnl_ticks),
            "pnl_dollars": float(self.pnl_dollars),
            "exit_reason": self.exit_reason,
            "ambiguous_fill": bool(self.ambiguous_fill),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BacktestResult:
    """Complete deterministic backtest payload ready for JSON serialization and LWC rendering."""

    strategy_id: str
    strategy_version: str
    contract_id: str
    root_symbol: str
    timeframe: str
    live_mode: bool
    params: dict[str, Any]
    instrument: InstrumentSpec
    bar_count: int
    start_time: int | None
    end_time: int | None
    excluded_provisional_count: int
    trades: tuple[BacktestTrade, ...]
    markers: tuple[dict[str, Any], ...]
    trade_lines: tuple[dict[str, Any], ...]
    zones: tuple[StrategyZone, ...]
    indicators: tuple[IndicatorSeries, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize BacktestResult to a JSON-safe dictionary."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "contract_id": self.contract_id,
            "root_symbol": self.root_symbol,
            "timeframe": self.timeframe,
            "live_mode": self.live_mode,
            "params": dict(self.params),
            "instrument": {
                "root_symbol": self.instrument.root_symbol,
                "name": self.instrument.name,
                "exchange": self.instrument.exchange,
                "tick_size": self.instrument.tick_size,
                "tick_value": self.instrument.tick_value,
                "currency": self.instrument.currency,
            },
            "bar_count": self.bar_count,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "excluded_provisional_count": self.excluded_provisional_count,
            "trades": [t.to_dict() for t in self.trades],
            "markers": [dict(m) for m in self.markers],
            "trade_lines": [dict(line) for line in self.trade_lines],
            "zones": [z.to_dict() for z in self.zones],
            "indicators": [ind.to_dict() for ind in self.indicators],
            "summary": dict(self.summary),
        }


@dataclass
class _PendingOrder:
    signal: StrategySignal


@dataclass
class _OpenPosition:
    direction: Literal["LONG", "SHORT"]
    signal_time: int
    entry_time: int
    entry_bar_index: int
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: int
    reason: str


def calculate_trade_pnl_decimal(
    *,
    direction: Literal["LONG", "SHORT"],
    entry_price: float,
    exit_price: float,
    quantity: int,
    instrument: InstrumentSpec,
) -> tuple[Decimal, Decimal]:
    """Compute exact (pnl_ticks, pnl_dollars) using Decimal arithmetic (zero float drift)."""
    entry_d = Decimal(str(entry_price))
    exit_d = Decimal(str(exit_price))
    tick_size_d = Decimal(str(instrument.tick_size))
    tick_value_d = Decimal(str(instrument.tick_value))
    qty_d = Decimal(quantity)

    diff = (exit_d - entry_d) if direction == "LONG" else (entry_d - exit_d)
    pnl_ticks = diff / tick_size_d
    pnl_dollars = pnl_ticks * tick_value_d * qty_d
    return pnl_ticks, pnl_dollars


def _validate_input_bars(bars: Sequence[OHLCVBar]) -> None:
    prev_time: int | None = None
    for idx, bar in enumerate(bars):
        if isinstance(bar.time, bool) or not isinstance(bar.time, int):
            raise ValueError(f"Bar[{idx}].time must be an integer Unix timestamp")
        if prev_time is not None and bar.time <= prev_time:
            raise ValueError(
                f"Bars must have strictly increasing timestamps: "
                f"bar[{idx}].time ({bar.time}) <= previous ({prev_time})"
            )
        prev_time = bar.time

        for field_name, val in (
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
        ):
            if not math.isfinite(val) or val <= 0.0:
                raise ValueError(
                    f"Bar[{idx}].{field_name} must be positive and finite, got {val!r}"
                )
        if not math.isfinite(bar.volume) or bar.volume < 0.0:
            raise ValueError(
                f"Bar[{idx}].volume must be non-negative and finite, got {bar.volume!r}"
            )
        if (
            bar.low > bar.high
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            raise ValueError(f"Bar[{idx}] violates OHLC bounds: {bar!r}")


def _evaluate_pending_fill(
    pending: _PendingOrder,
    bar: OHLCVBar,
    bar_index: int,
) -> tuple[_OpenPosition | None, bool, bool]:
    """Attempt to fill a pending order on bar[bar_index].

    Returns (open_position, intrabar_limit_fill, expired).
    """
    sig = pending.signal
    if sig.signal_bar_index >= bar_index:
        return None, False, False

    if sig.order_type == "market":
        return (
            _OpenPosition(
                direction=sig.direction,
                signal_time=sig.signal_time,
                entry_time=bar.time,
                entry_bar_index=bar_index,
                entry_price=float(bar.open),
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                quantity=sig.quantity,
                reason=sig.reason,
            ),
            False,
            False,
        )

    limit_price = sig.limit_price
    if limit_price is None:
        raise ValueError("limit_price is required for limit orders")

    if sig.direction == "LONG":
        if bar.open <= limit_price:
            return (
                _OpenPosition(
                    direction="LONG",
                    signal_time=sig.signal_time,
                    entry_time=bar.time,
                    entry_bar_index=bar_index,
                    entry_price=float(bar.open),
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    quantity=sig.quantity,
                    reason=sig.reason,
                ),
                False,
                False,
            )
        if bar.low <= limit_price <= bar.high:
            return (
                _OpenPosition(
                    direction="LONG",
                    signal_time=sig.signal_time,
                    entry_time=bar.time,
                    entry_bar_index=bar_index,
                    entry_price=float(limit_price),
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    quantity=sig.quantity,
                    reason=sig.reason,
                ),
                True,
                False,
            )
    else:
        if bar.open >= limit_price:
            return (
                _OpenPosition(
                    direction="SHORT",
                    signal_time=sig.signal_time,
                    entry_time=bar.time,
                    entry_bar_index=bar_index,
                    entry_price=float(bar.open),
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    quantity=sig.quantity,
                    reason=sig.reason,
                ),
                False,
                False,
            )
        if bar.low <= limit_price <= bar.high:
            return (
                _OpenPosition(
                    direction="SHORT",
                    signal_time=sig.signal_time,
                    entry_time=bar.time,
                    entry_bar_index=bar_index,
                    entry_price=float(limit_price),
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    quantity=sig.quantity,
                    reason=sig.reason,
                ),
                True,
                False,
            )

    expired = (bar_index - sig.signal_bar_index) >= sig.max_wait_bars
    return None, False, expired


def _evaluate_exit_on_bar(
    pos: _OpenPosition,
    bar: OHLCVBar,
    *,
    intrabar_limit_fill: bool,
) -> tuple[float, ExitReason, bool] | None:
    """Evaluate SL/TP exit on bar using conservative F08 both-hit tie-breaking."""
    if pos.direction == "LONG":
        sl_hit = bar.low <= pos.stop_loss
        tp_hit = bar.high >= pos.take_profit
        if intrabar_limit_fill:
            if sl_hit:
                return float(pos.stop_loss), "stop_loss", bool(tp_hit)
            return None
        if sl_hit and tp_hit:
            exit_px = float(bar.open) if bar.open <= pos.stop_loss else float(pos.stop_loss)
            return exit_px, "stop_loss", True
        if sl_hit:
            exit_px = float(bar.open) if bar.open <= pos.stop_loss else float(pos.stop_loss)
            return exit_px, "stop_loss", False
        if tp_hit:
            exit_px = float(bar.open) if bar.open >= pos.take_profit else float(pos.take_profit)
            return exit_px, "take_profit", False
        return None

    sl_hit = bar.high >= pos.stop_loss
    tp_hit = bar.low <= pos.take_profit
    if intrabar_limit_fill:
        if sl_hit:
            return float(pos.stop_loss), "stop_loss", bool(tp_hit)
        return None
    if sl_hit and tp_hit:
        exit_px = float(bar.open) if bar.open >= pos.stop_loss else float(pos.stop_loss)
        return exit_px, "stop_loss", True
    if sl_hit:
        exit_px = float(bar.open) if bar.open >= pos.stop_loss else float(pos.stop_loss)
        return exit_px, "stop_loss", False
    if tp_hit:
        exit_px = float(bar.open) if bar.open <= pos.take_profit else float(pos.take_profit)
        return exit_px, "take_profit", False
    return None


def _build_markers_and_lines(
    trades: Sequence[BacktestTrade],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    raw_markers: list[tuple[int, int, int, dict[str, Any]]] = []
    lines: list[dict[str, Any]] = []

    for idx, trade in enumerate(trades):
        is_long = trade.direction == "LONG"
        is_profit = trade.pnl_dollars >= 0.0
        pnl_sign = "+" if is_profit else "-"
        pnl_abs = abs(trade.pnl_dollars)

        raw_markers.append(
            (
                trade.entry_time,
                0,
                idx,
                {
                    "time": trade.entry_time,
                    "position": "belowBar" if is_long else "aboveBar",
                    "color": "#22c55e" if is_long else "#ef4444",
                    "shape": "arrowUp" if is_long else "arrowDown",
                    "text": f"{trade.direction} @{trade.entry_price:g}",
                },
            )
        )
        raw_markers.append(
            (
                trade.exit_time,
                1,
                idx,
                {
                    "time": trade.exit_time,
                    "position": "aboveBar" if is_long else "belowBar",
                    "color": "#22c55e" if is_profit else "#ef4444",
                    "shape": "arrowDown" if is_long else "arrowUp",
                    "text": f"Exit {pnl_sign}${pnl_abs:.2f}",
                },
            )
        )

        lines.append(
            {
                "trade_id": trade.trade_id,
                "direction": trade.direction,
                "entry_time": trade.entry_time,
                "entry_price": float(trade.entry_price),
                "exit_time": trade.exit_time,
                "exit_price": float(trade.exit_price),
                "pnl_dollars": float(trade.pnl_dollars),
                "pnl_ticks": float(trade.pnl_ticks),
                "color": "#22c55e" if is_profit else "#ef4444",
                "ambiguous_fill": bool(trade.ambiguous_fill),
            }
        )

    raw_markers.sort(key=lambda item: (item[0], item[1], item[2]))
    markers = tuple(item[3] for item in raw_markers)
    return markers, tuple(lines)


def _build_summary(
    trades: Sequence[BacktestTrade],
    trade_dollar_decimals: Sequence[Decimal],
) -> dict[str, Any]:
    total_trades = len(trades)
    winning_trades = 0
    losing_trades = 0
    breakeven_trades = 0
    ambiguous_fill_count = 0

    net_profit_d = Decimal("0")
    gross_profit_d = Decimal("0")
    gross_loss_d = Decimal("0")
    peak_equity_d = Decimal("0")
    max_drawdown_d = Decimal("0")
    equity_curve: list[dict[str, Any]] = []

    for trade, pnl_d in zip(trades, trade_dollar_decimals, strict=True):
        net_profit_d += pnl_d
        if pnl_d > 0:
            winning_trades += 1
            gross_profit_d += pnl_d
        elif pnl_d < 0:
            losing_trades += 1
            gross_loss_d += -pnl_d
        else:
            breakeven_trades += 1

        if trade.ambiguous_fill:
            ambiguous_fill_count += 1

        if net_profit_d > peak_equity_d:
            peak_equity_d = net_profit_d
        drawdown_d = peak_equity_d - net_profit_d
        if drawdown_d > max_drawdown_d:
            max_drawdown_d = drawdown_d

        equity_curve.append(
            {
                "time": trade.exit_time,
                "equity_dollars": float(net_profit_d),
                "trade_id": trade.trade_id,
            }
        )

    win_rate_pct = (
        (float(winning_trades) / float(total_trades)) * 100.0 if total_trades > 0 else 0.0
    )
    profit_factor = (
        float(gross_profit_d / gross_loss_d) if gross_loss_d > Decimal("0") else None
    )

    return {
        "net_profit_dollars": float(net_profit_d),
        "gross_profit_dollars": float(gross_profit_d),
        "gross_loss_dollars": float(gross_loss_d),
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "max_drawdown_dollars": float(max_drawdown_d),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "breakeven_trades": breakeven_trades,
        "ambiguous_fill_count": ambiguous_fill_count,
        "equity_curve": equity_curve,
    }


def run_backtest(
    bars: Sequence[OHLCVBar],
    *,
    contract_id: str,
    timeframe: str,
    root_symbol: str | None = None,
    live_mode: bool = False,
    strategy: BaseStrategy | None = None,
    strategy_id: str | None = None,
    params: Mapping[str, Any] | None = None,
    include_provisional: bool = False,
) -> BacktestResult:
    """Run a deterministic strictly causal F08 backtest on OHLCV bars."""
    clean_contract = contract_id.strip().upper()
    if not clean_contract:
        raise ValueError("contract_id must be a non-empty string")
    eff_tf = normalize_timeframe(timeframe)
    resolved_root = validate_root_symbol(clean_contract, root_symbol)
    if resolved_root is None:
        raise ValueError(
            f"Unable to determine CME root_symbol for contract_id {clean_contract!r}"
        )
    instrument = get_instrument_spec(resolved_root)

    if strategy is not None:
        active_strategy = strategy
        active_strategy.reset()
    else:
        active_strategy = create_strategy(strategy_id or "ict_fvg_sweep", params)

    _validate_input_bars(bars)

    if include_provisional:
        eval_bars = list(bars)
        excluded_provisional_count = 0
    else:
        eval_bars = [b for b in bars if b.is_closed]
        excluded_provisional_count = len(bars) - len(eval_bars)

    if not eval_bars:
        empty_summary = _build_summary((), ())
        indicators = tuple(active_strategy.compute_indicators(eval_bars, instrument))
        return BacktestResult(
            strategy_id=active_strategy.strategy_id,
            strategy_version=active_strategy.strategy_version,
            contract_id=clean_contract,
            root_symbol=instrument.root_symbol,
            timeframe=eff_tf,
            live_mode=bool(live_mode),
            params=dict(active_strategy.params),
            instrument=instrument,
            bar_count=0,
            start_time=None,
            end_time=None,
            excluded_provisional_count=excluded_provisional_count,
            trades=(),
            markers=(),
            trade_lines=(),
            zones=(),
            indicators=indicators,
            summary=empty_summary,
        )

    pending_order: _PendingOrder | None = None
    open_position: _OpenPosition | None = None
    trades: list[BacktestTrade] = []
    trade_dollar_decimals: list[Decimal] = []
    collected_zones: list[StrategyZone] = []
    seen_zone_keys: set[tuple[Any, ...]] = set()

    def _record_trade(
        pos: _OpenPosition,
        *,
        exit_time: int,
        exit_price: float,
        exit_reason: ExitReason,
        ambiguous_fill: bool,
    ) -> None:
        pnl_ticks_d, pnl_dollars_d = calculate_trade_pnl_decimal(
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            instrument=instrument,
        )
        trade = BacktestTrade(
            trade_id=f"T{len(trades) + 1}",
            direction=pos.direction,
            signal_time=pos.signal_time,
            entry_time=pos.entry_time,
            entry_price=float(pos.entry_price),
            exit_time=exit_time,
            exit_price=float(exit_price),
            stop_loss=float(pos.stop_loss),
            take_profit=float(pos.take_profit),
            quantity=pos.quantity,
            pnl_ticks=float(pnl_ticks_d),
            pnl_dollars=float(pnl_dollars_d),
            exit_reason=exit_reason,
            ambiguous_fill=ambiguous_fill,
            reason=pos.reason,
        )
        trades.append(trade)
        trade_dollar_decimals.append(pnl_dollars_d)

    for i, bar in enumerate(eval_bars):
        intrabar_limit_fill = False

        # 1. Pending order fill on bar[i] (earliest allowed bar is signal_bar_index + 1)
        if pending_order is not None:
            filled_pos, intrabar_limit_fill, expired = _evaluate_pending_fill(
                pending_order,
                bar,
                i,
            )
            if filled_pos is not None:
                open_position = filled_pos
                pending_order = None
            elif expired:
                pending_order = None

        # 2. Open position SL/TP evaluation on bar[i]
        if open_position is not None:
            exit_outcome = _evaluate_exit_on_bar(
                open_position,
                bar,
                intrabar_limit_fill=intrabar_limit_fill,
            )
            if exit_outcome is not None:
                exit_price, exit_reason, ambiguous_fill = exit_outcome
                _record_trade(
                    open_position,
                    exit_time=bar.time,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    ambiguous_fill=ambiguous_fill,
                )
                open_position = None

        # 3. Causal strategy evaluation at bar[i] close
        ctx = StrategyContext(
            bars=CausalBarView(eval_bars, i + 1),
            bar_index=i,
            instrument=instrument,
            timeframe=eff_tf,
            has_open_position=open_position is not None,
            has_pending_order=pending_order is not None,
            open_direction=open_position.direction if open_position is not None else None,
        )
        step_output = active_strategy.on_bar(ctx)

        for zone in step_output.zones:
            if zone.detected_at_time > bar.time:
                raise ValueError(
                    f"Causality violation: zone detected_at_time ({zone.detected_at_time}) "
                    f"exceeds current bar.time ({bar.time})"
                )
            dedup_key = zone.dedup_key()
            if dedup_key not in seen_zone_keys:
                seen_zone_keys.add(dedup_key)
                collected_zones.append(zone)

        if step_output.signal is not None and open_position is None and pending_order is None:
            sig = step_output.signal
            if sig.signal_bar_index != i or sig.signal_time != bar.time:
                raise ValueError(
                    f"Causality violation: signal index/time ({sig.signal_bar_index}, "
                    f"{sig.signal_time}) != current bar ({i}, {bar.time})"
                )
            pending_order = _PendingOrder(signal=sig)

    # 4. Close any remaining open position at final bar close
    if open_position is not None:
        last_bar = eval_bars[-1]
        _record_trade(
            open_position,
            exit_time=last_bar.time,
            exit_price=float(last_bar.close),
            exit_reason="end_of_data",
            ambiguous_fill=False,
        )

    markers, trade_lines = _build_markers_and_lines(trades)
    indicators = tuple(active_strategy.compute_indicators(eval_bars, instrument))
    summary = _build_summary(trades, trade_dollar_decimals)

    return BacktestResult(
        strategy_id=active_strategy.strategy_id,
        strategy_version=active_strategy.strategy_version,
        contract_id=clean_contract,
        root_symbol=instrument.root_symbol,
        timeframe=eff_tf,
        live_mode=bool(live_mode),
        params=dict(active_strategy.params),
        instrument=instrument,
        bar_count=len(eval_bars),
        start_time=eval_bars[0].time,
        end_time=eval_bars[-1].time,
        excluded_provisional_count=excluded_provisional_count,
        trades=tuple(trades),
        markers=markers,
        trade_lines=trade_lines,
        zones=tuple(collected_zones),
        indicators=indicators,
        summary=summary,
    )
