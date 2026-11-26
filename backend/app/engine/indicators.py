"""Pure causal technical indicators and ICT market structure detectors (F08, R05, ARCH-001)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import vwap_session_open_utc


@dataclass(frozen=True)
class FVGZone:
    """Strictly causal 3-candle Fair Value Gap confirmed on bar i close."""

    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    midpoint: float
    start_time: int
    end_time: int
    detected_at_time: int
    bar_index: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize FVGZone to a JSON-safe dictionary."""
        return {
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "midpoint": self.midpoint,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "detected_at_time": self.detected_at_time,
            "bar_index": self.bar_index,
        }


@dataclass(frozen=True)
class OrderBlockZone:
    """Strictly causal Order Block confirmed at displacement bar i."""

    direction: Literal["bullish", "bearish"]
    top: float
    bottom: float
    start_time: int
    end_time: int
    detected_at_time: int
    bar_index: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize OrderBlockZone to a JSON-safe dictionary."""
        return {
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "detected_at_time": self.detected_at_time,
            "bar_index": self.bar_index,
        }


@dataclass(frozen=True)
class LiquiditySweepEvent:
    """Strictly causal prior swing high/low sweep event confirmed at bar i."""

    direction: Literal["bullish", "bearish"]
    level_price: float
    level_time: int
    sweep_extreme: float
    detected_at_time: int
    bar_index: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize LiquiditySweepEvent to a JSON-safe dictionary."""
        return {
            "direction": self.direction,
            "level_price": self.level_price,
            "level_time": self.level_time,
            "sweep_extreme": self.sweep_extreme,
            "detected_at_time": self.detected_at_time,
            "bar_index": self.bar_index,
        }


def _validate_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
    return value


def ema(bars: Sequence[OHLCVBar], period: int) -> list[tuple[int, float]]:
    """Compute strictly causal Exponential Moving Average initialized via SMA(period)."""
    p = _validate_positive_int(period, "period")
    if len(bars) < p:
        return []

    sma = sum(float(bars[idx].close) for idx in range(p)) / float(p)
    current = sma
    result: list[tuple[int, float]] = [(bars[p - 1].time, current)]
    k = 2.0 / float(p + 1)
    one_minus_k = 1.0 - k
    for idx in range(p, len(bars)):
        current = float(bars[idx].close) * k + current * one_minus_k
        result.append((bars[idx].time, current))
    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    value = 100.0 - (100.0 / (1.0 + rs))
    return max(0.0, min(100.0, value))


def rsi(bars: Sequence[OHLCVBar], period: int = 14) -> list[tuple[int, float]]:
    """Compute strictly causal Wilder-smoothed Relative Strength Index in [0.0, 100.0]."""
    p = _validate_positive_int(period, "period")
    if len(bars) <= p:
        return []

    gain_sum = 0.0
    loss_sum = 0.0
    for idx in range(1, p + 1):
        delta = float(bars[idx].close) - float(bars[idx - 1].close)
        if delta > 0.0:
            gain_sum += delta
        elif delta < 0.0:
            loss_sum += -delta

    avg_gain = gain_sum / float(p)
    avg_loss = loss_sum / float(p)
    result: list[tuple[int, float]] = [(bars[p].time, _rsi_value(avg_gain, avg_loss))]

    p_minus_1 = float(p - 1)
    p_float = float(p)
    for idx in range(p + 1, len(bars)):
        delta = float(bars[idx].close) - float(bars[idx - 1].close)
        gain = delta if delta > 0.0 else 0.0
        loss = -delta if delta < 0.0 else 0.0
        avg_gain = (avg_gain * p_minus_1 + gain) / p_float
        avg_loss = (avg_loss * p_minus_1 + loss) / p_float
        result.append((bars[idx].time, _rsi_value(avg_gain, avg_loss)))
    return result


def session_vwap(bars: Sequence[OHLCVBar]) -> list[tuple[int, float]]:
    """Compute CME Globex 18:00 America/New_York session-resetting VWAP (R05)."""
    if not bars:
        return []

    result: list[tuple[int, float]] = []
    current_session: int | None = None
    cum_pv = 0.0
    cum_vol = 0.0

    for bar in bars:
        sess_open = vwap_session_open_utc(bar.time)
        if sess_open != current_session:
            current_session = sess_open
            cum_pv = 0.0
            cum_vol = 0.0

        typical_price = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        vol = float(bar.volume)
        cum_pv += typical_price * vol
        cum_vol += vol
        vwap_val = (cum_pv / cum_vol) if cum_vol > 0.0 else typical_price
        result.append((bar.time, vwap_val))

    return result


def _validate_fvg_thresholds(min_gap_ticks: float, tick_size: float) -> float:
    if isinstance(min_gap_ticks, bool) or not isinstance(min_gap_ticks, (int, float)):
        raise ValueError(f"min_gap_ticks must be numeric, got {min_gap_ticks!r}")
    if isinstance(tick_size, bool) or not isinstance(tick_size, (int, float)):
        raise ValueError(f"tick_size must be numeric, got {tick_size!r}")
    min_ticks_f = float(min_gap_ticks)
    tick_f = float(tick_size)
    if not math.isfinite(min_ticks_f) or min_ticks_f < 0.0:
        raise ValueError(f"min_gap_ticks must be finite and >= 0, got {min_gap_ticks!r}")
    if not math.isfinite(tick_f) or tick_f <= 0.0:
        raise ValueError(f"tick_size must be finite and > 0, got {tick_size!r}")
    return min_ticks_f * tick_f


def detect_fvg_at_index(
    bars: Sequence[OHLCVBar],
    index: int,
    *,
    min_gap_ticks: float = 1.0,
    tick_size: float = 0.25,
) -> FVGZone | None:
    """Check whether bar at `index` confirms a causal 3-candle FVG over [index-2, index]."""
    min_gap = _validate_fvg_thresholds(min_gap_ticks, tick_size)
    if index < 2 or index >= len(bars):
        return None

    left = bars[index - 2]
    right = bars[index]
    if right.low > left.high and (right.low - left.high) + 1e-12 >= min_gap:
        bottom = float(left.high)
        top = float(right.low)
        return FVGZone(
            direction="bullish",
            top=top,
            bottom=bottom,
            midpoint=(top + bottom) / 2.0,
            start_time=left.time,
            end_time=right.time,
            detected_at_time=right.time,
            bar_index=index,
        )
    if right.high < left.low and (left.low - right.high) + 1e-12 >= min_gap:
        top = float(left.low)
        bottom = float(right.high)
        return FVGZone(
            direction="bearish",
            top=top,
            bottom=bottom,
            midpoint=(top + bottom) / 2.0,
            start_time=left.time,
            end_time=right.time,
            detected_at_time=right.time,
            bar_index=index,
        )
    return None


def detect_fvg_zones(
    bars: Sequence[OHLCVBar],
    *,
    min_gap_ticks: float = 1.0,
    tick_size: float = 0.25,
) -> list[FVGZone]:
    """Detect all strictly causal 3-candle FVG zones confirmed at bar i >= 2."""
    _validate_fvg_thresholds(min_gap_ticks, tick_size)
    zones: list[FVGZone] = []
    for idx in range(2, len(bars)):
        zone = detect_fvg_at_index(
            bars,
            idx,
            min_gap_ticks=min_gap_ticks,
            tick_size=tick_size,
        )
        if zone is not None:
            zones.append(zone)
    return zones


def detect_order_blocks(
    bars: Sequence[OHLCVBar],
    *,
    lookback: int = 3,
) -> list[OrderBlockZone]:
    """Detect strictly causal bullish/bearish Order Blocks confirmed on displacement bar i."""
    lb = _validate_positive_int(lookback, "lookback")
    zones: list[OrderBlockZone] = []
    seen_bull_ob_indices: set[int] = set()
    seen_bear_ob_indices: set[int] = set()

    for idx in range(lb, len(bars)):
        current = bars[idx]
        prior_high = max(bars[k].high for k in range(idx - lb, idx))
        prior_low = min(bars[k].low for k in range(idx - lb, idx))

        if current.close > current.open and current.close > prior_high:
            for j in range(idx - 1, idx - lb - 1, -1):
                candidate = bars[j]
                if candidate.close < candidate.open:
                    if j not in seen_bull_ob_indices:
                        seen_bull_ob_indices.add(j)
                        zones.append(
                            OrderBlockZone(
                                direction="bullish",
                                top=float(candidate.high),
                                bottom=float(candidate.low),
                                start_time=candidate.time,
                                end_time=current.time,
                                detected_at_time=current.time,
                                bar_index=idx,
                            )
                        )
                    break

        if current.close < current.open and current.close < prior_low:
            for j in range(idx - 1, idx - lb - 1, -1):
                candidate = bars[j]
                if candidate.close > candidate.open:
                    if j not in seen_bear_ob_indices:
                        seen_bear_ob_indices.add(j)
                        zones.append(
                            OrderBlockZone(
                                direction="bearish",
                                top=float(candidate.high),
                                bottom=float(candidate.low),
                                start_time=candidate.time,
                                end_time=current.time,
                                detected_at_time=current.time,
                                bar_index=idx,
                            )
                        )
                    break

    return zones


def detect_liquidity_sweeps_at_index(
    bars: Sequence[OHLCVBar],
    index: int,
    *,
    swing_lookback: int = 5,
) -> list[LiquiditySweepEvent]:
    """Detect causal SSL/BSL liquidity sweeps at bar `index` using strictly prior bars."""
    lb = _validate_positive_int(swing_lookback, "swing_lookback")
    if index < lb or index >= len(bars):
        return []

    current = bars[index]
    low_bar = bars[index - 1]
    high_bar = bars[index - 1]
    for j in range(index - 2, index - lb - 1, -1):
        cand = bars[j]
        if cand.low < low_bar.low:
            low_bar = cand
        if cand.high > high_bar.high:
            high_bar = cand

    events: list[LiquiditySweepEvent] = []
    if current.low < low_bar.low and current.close > low_bar.low:
        events.append(
            LiquiditySweepEvent(
                direction="bullish",
                level_price=float(low_bar.low),
                level_time=low_bar.time,
                sweep_extreme=float(current.low),
                detected_at_time=current.time,
                bar_index=index,
            )
        )
    if current.high > high_bar.high and current.close < high_bar.high:
        events.append(
            LiquiditySweepEvent(
                direction="bearish",
                level_price=float(high_bar.high),
                level_time=high_bar.time,
                sweep_extreme=float(current.high),
                detected_at_time=current.time,
                bar_index=index,
            )
        )
    return events


def detect_liquidity_sweeps(
    bars: Sequence[OHLCVBar],
    *,
    swing_lookback: int = 5,
) -> list[LiquiditySweepEvent]:
    """Detect all strictly causal liquidity sweeps across `bars`."""
    lb = _validate_positive_int(swing_lookback, "swing_lookback")
    events: list[LiquiditySweepEvent] = []
    for idx in range(lb, len(bars)):
        events.extend(
            detect_liquidity_sweeps_at_index(bars, idx, swing_lookback=lb)
        )
    return events
