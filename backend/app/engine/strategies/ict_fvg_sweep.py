"""ICT FVG + Liquidity Sweep sample strategy (F08, R05, ARCH-001)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

from app.engine.indicators import (
    LiquiditySweepEvent,
    detect_fvg_at_index,
    detect_liquidity_sweeps_at_index,
    ema,
    session_vwap,
)
from app.engine.instruments import InstrumentSpec
from app.engine.ohlcv import OHLCVBar
from app.engine.strategy_base import (
    BaseStrategy,
    IndicatorPoint,
    IndicatorSeries,
    StrategyContext,
    StrategySignal,
    StrategyStepOutput,
    StrategyZone,
)

_INT_PARAM_BOUNDS: dict[str, tuple[int, int, int]] = {
    "swing_lookback": (5, 2, 100),
    "sweep_window_bars": (5, 0, 50),
    "max_wait_bars": (5, 1, 50),
    "quantity": (1, 1, 100),
    "ema_period": (20, 2, 200),
}

_FLOAT_PARAM_BOUNDS: dict[str, tuple[float, float, float]] = {
    "min_fvg_ticks": (1.0, 0.0, 1000.0),
    "risk_reward": (2.0, 0.5, 20.0),
    "sl_buffer_ticks": (1.0, 0.0, 100.0),
}


def _snap_decimal_to_tick(price_d: Decimal, tick_d: Decimal) -> Decimal:
    steps = (price_d / tick_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return steps * tick_d


class IctFvgSweepStrategy(BaseStrategy):
    """Causal ICT Fair Value Gap + Liquidity Sweep strategy."""

    strategy_id: ClassVar[str] = "ict_fvg_sweep"
    strategy_version: ClassVar[str] = "1.0.0"
    name: ClassVar[str] = "ICT FVG + Liquidity Sweep"
    description: ClassVar[str] = (
        "Enters in the direction of a 3-candle Fair Value Gap confirmed within "
        "sweep_window_bars after a causal prior swing high/low liquidity sweep."
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        self._last_bullish_sweep: LiquiditySweepEvent | None = None
        self._bullish_sweep_consumed: bool = False
        self._last_bearish_sweep: LiquiditySweepEvent | None = None
        self._bearish_sweep_consumed: bool = False

    def reset(self) -> None:
        """Clear tracked sweep state before a new causal backtest pass."""
        self._last_bullish_sweep = None
        self._bullish_sweep_consumed = False
        self._last_bearish_sweep = None
        self._bearish_sweep_consumed = False

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """Return default strategy parameters."""
        return {
            "swing_lookback": 5,
            "sweep_window_bars": 5,
            "min_fvg_ticks": 1.0,
            "risk_reward": 2.0,
            "sl_buffer_ticks": 1.0,
            "entry_mode": "market",
            "max_wait_bars": 5,
            "quantity": 1,
            "ema_period": 20,
            "include_vwap": True,
        }

    @classmethod
    def param_schema(cls) -> dict[str, Any]:
        """Return parameter bounds and UI metadata."""
        return {
            "swing_lookback": {"type": "integer", "min": 2, "max": 100, "default": 5},
            "sweep_window_bars": {"type": "integer", "min": 0, "max": 50, "default": 5},
            "min_fvg_ticks": {"type": "number", "min": 0.0, "max": 1000.0, "default": 1.0},
            "risk_reward": {"type": "number", "min": 0.5, "max": 20.0, "default": 2.0},
            "sl_buffer_ticks": {"type": "number", "min": 0.0, "max": 100.0, "default": 1.0},
            "entry_mode": {"type": "string", "enum": ["market", "fvg_ce"], "default": "market"},
            "max_wait_bars": {"type": "integer", "min": 1, "max": 50, "default": 5},
            "quantity": {"type": "integer", "min": 1, "max": 100, "default": 1},
            "ema_period": {"type": "integer", "min": 2, "max": 200, "default": 20},
            "include_vwap": {"type": "boolean", "default": True},
        }

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Strictly validate strategy parameters, rejecting unknown keys or out-of-range values."""
        validated = cls.default_params()
        if params is None:
            return validated
        if not isinstance(params, Mapping):
            raise ValueError(f"params must be a mapping, got {type(params).__name__}")

        unknown = sorted(set(params.keys()) - set(validated.keys()))
        if unknown:
            raise ValueError(f"Unknown parameter(s) for {cls.strategy_id}: {unknown}")

        for key, (default_val, min_v, max_v) in _INT_PARAM_BOUNDS.items():
            raw = params.get(key, default_val)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError(f"Parameter '{key}' must be an integer, got {raw!r}")
            if raw < min_v or raw > max_v:
                raise ValueError(
                    f"Parameter '{key}' ({raw}) out of allowed range [{min_v}, {max_v}]"
                )
            validated[key] = raw

        for key, (default_val, min_f, max_f) in _FLOAT_PARAM_BOUNDS.items():
            raw = params.get(key, default_val)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"Parameter '{key}' must be a number, got {raw!r}")
            num = float(raw)
            if not math.isfinite(num) or num < min_f or num > max_f:
                raise ValueError(
                    f"Parameter '{key}' ({raw!r}) must be finite within [{min_f}, {max_f}]"
                )
            validated[key] = num

        raw_entry_mode = params.get("entry_mode", validated["entry_mode"])
        if not isinstance(raw_entry_mode, str) or raw_entry_mode not in {"market", "fvg_ce"}:
            raise ValueError(
                f"Parameter 'entry_mode' must be 'market' or 'fvg_ce', got {raw_entry_mode!r}"
            )
        validated["entry_mode"] = raw_entry_mode

        raw_vwap = params.get("include_vwap", validated["include_vwap"])
        if not isinstance(raw_vwap, bool):
            raise ValueError(
                f"Parameter 'include_vwap' must be a boolean, got {raw_vwap!r}"
            )
        validated["include_vwap"] = raw_vwap
        return validated

    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        """Evaluate bar[ctx.bar_index] close strictly using ctx.bars[0..ctx.bar_index]."""
        i = ctx.bar_index
        bars = ctx.bars
        current = bars[i]
        tick_size = ctx.instrument.tick_size

        swing_lookback = int(self.params["swing_lookback"])
        sweep_window = int(self.params["sweep_window_bars"])
        min_fvg_ticks = float(self.params["min_fvg_ticks"])
        risk_reward = float(self.params["risk_reward"])
        sl_buffer_ticks = float(self.params["sl_buffer_ticks"])
        entry_mode = str(self.params["entry_mode"])
        max_wait_bars = int(self.params["max_wait_bars"])
        quantity = int(self.params["quantity"])

        zones: list[StrategyZone] = []

        sweeps = detect_liquidity_sweeps_at_index(bars, i, swing_lookback=swing_lookback)
        for sweep in sweeps:
            if sweep.direction == "bullish":
                self._last_bullish_sweep = sweep
                self._bullish_sweep_consumed = False
                zones.append(
                    StrategyZone(
                        kind="liquidity_level",
                        direction="bullish",
                        start_time=sweep.level_time,
                        end_time=current.time,
                        detected_at_time=current.time,
                        top=sweep.level_price,
                        bottom=sweep.sweep_extreme,
                        midpoint=sweep.level_price,
                        label="SSL Sweep",
                    )
                )
            else:
                self._last_bearish_sweep = sweep
                self._bearish_sweep_consumed = False
                zones.append(
                    StrategyZone(
                        kind="liquidity_level",
                        direction="bearish",
                        start_time=sweep.level_time,
                        end_time=current.time,
                        detected_at_time=current.time,
                        top=sweep.sweep_extreme,
                        bottom=sweep.level_price,
                        midpoint=sweep.level_price,
                        label="BSL Sweep",
                    )
                )

        fvg = detect_fvg_at_index(
            bars,
            i,
            min_gap_ticks=min_fvg_ticks,
            tick_size=tick_size,
        )
        if fvg is not None:
            zones.append(
                StrategyZone(
                    kind="fvg",
                    direction=fvg.direction,
                    start_time=fvg.start_time,
                    end_time=fvg.end_time,
                    detected_at_time=current.time,
                    top=fvg.top,
                    bottom=fvg.bottom,
                    midpoint=fvg.midpoint,
                    label="Bullish FVG" if fvg.direction == "bullish" else "Bearish FVG",
                )
            )

        signal: StrategySignal | None = None
        if (
            fvg is not None
            and not ctx.has_open_position
            and not ctx.has_pending_order
            and i >= 2
        ):
            tick_d = Decimal(str(tick_size))
            sl_buf_d = Decimal(str(sl_buffer_ticks))
            rr_d = Decimal(str(risk_reward))
            if (
                fvg.direction == "bullish"
                and self._last_bullish_sweep is not None
                and not self._bullish_sweep_consumed
                and (i - self._last_bullish_sweep.bar_index) <= sweep_window
            ):
                ref_entry_d = (
                    Decimal(str(fvg.midpoint))
                    if entry_mode == "fvg_ce"
                    else Decimal(str(current.close))
                )
                min_low_d = min(
                    Decimal(str(self._last_bullish_sweep.sweep_extreme)),
                    Decimal(str(bars[i - 2].low)),
                    Decimal(str(bars[i - 1].low)),
                )
                raw_sl_d = min_low_d - (sl_buf_d * tick_d)
                stop_loss_d = _snap_decimal_to_tick(raw_sl_d, tick_d)
                if Decimal("0") < stop_loss_d < ref_entry_d:
                    raw_tp_d = ref_entry_d + (ref_entry_d - stop_loss_d) * rr_d
                    take_profit_d = _snap_decimal_to_tick(raw_tp_d, tick_d)
                    if take_profit_d > ref_entry_d:
                        self._bullish_sweep_consumed = True
                        signal = StrategySignal(
                            direction="LONG",
                            signal_bar_index=i,
                            signal_time=current.time,
                            order_type="limit" if entry_mode == "fvg_ce" else "market",
                            limit_price=fvg.midpoint if entry_mode == "fvg_ce" else None,
                            stop_loss=float(stop_loss_d),
                            take_profit=float(take_profit_d),
                            quantity=quantity,
                            max_wait_bars=max_wait_bars,
                            reason="Bullish FVG + Liquidity Sweep",
                        )
            elif (
                fvg.direction == "bearish"
                and self._last_bearish_sweep is not None
                and not self._bearish_sweep_consumed
                and (i - self._last_bearish_sweep.bar_index) <= sweep_window
            ):
                ref_entry_d = (
                    Decimal(str(fvg.midpoint))
                    if entry_mode == "fvg_ce"
                    else Decimal(str(current.close))
                )
                max_high_d = max(
                    Decimal(str(self._last_bearish_sweep.sweep_extreme)),
                    Decimal(str(bars[i - 2].high)),
                    Decimal(str(bars[i - 1].high)),
                )
                raw_sl_d = max_high_d + (sl_buf_d * tick_d)
                stop_loss_d = _snap_decimal_to_tick(raw_sl_d, tick_d)
                if stop_loss_d > ref_entry_d:
                    raw_tp_d = ref_entry_d - (stop_loss_d - ref_entry_d) * rr_d
                    take_profit_d = _snap_decimal_to_tick(raw_tp_d, tick_d)
                    if Decimal("0") < take_profit_d < ref_entry_d:
                        self._bearish_sweep_consumed = True
                        signal = StrategySignal(
                            direction="SHORT",
                            signal_bar_index=i,
                            signal_time=current.time,
                            order_type="limit" if entry_mode == "fvg_ce" else "market",
                            limit_price=fvg.midpoint if entry_mode == "fvg_ce" else None,
                            stop_loss=float(stop_loss_d),
                            take_profit=float(take_profit_d),
                            quantity=quantity,
                            max_wait_bars=max_wait_bars,
                            reason="Bearish FVG + Liquidity Sweep",
                        )

        return StrategyStepOutput(signal=signal, zones=tuple(zones))

    def compute_indicators(
        self,
        bars: Sequence[OHLCVBar],
        instrument: InstrumentSpec,
    ) -> list[IndicatorSeries]:
        """Compute EMA(ema_period) and optional CME 18:00 ET session VWAP."""
        _ = instrument
        ema_period = int(self.params["ema_period"])
        series: list[IndicatorSeries] = [
            IndicatorSeries(
                id=f"ema_{ema_period}",
                name=f"EMA ({ema_period})",
                color="#f59e0b",
                overlay=True,
                points=tuple(
                    IndicatorPoint(time=ts, value=val) for ts, val in ema(bars, ema_period)
                ),
            )
        ]
        if bool(self.params["include_vwap"]):
            series.append(
                IndicatorSeries(
                    id="session_vwap",
                    name="Session VWAP (18:00 ET)",
                    color="#38bdf8",
                    overlay=True,
                    points=tuple(
                        IndicatorPoint(time=ts, value=val) for ts, val in session_vwap(bars)
                    ),
                )
            )
        return series
