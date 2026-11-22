"""Pure alert rule evaluation, recovery suppression, and message formatting (F03, F07, ARCH-001)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from app.engine.bar_aggregator import AggregatedBarEvent
from app.engine.instruments import infer_root_symbol

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class AlertSignal:
    """Pure strategy alert signal ready for formatting and outbox persistence."""

    strategy_id: str
    strategy_version: str
    live_mode: bool
    contract_id: str
    root_symbol: str | None
    timeframe: str
    bar_time: int
    direction: str
    reason: str
    entry_price: float
    stop_loss: float
    take_profit: float
    rr_ratio: float
    signal_tag: str | None = None


@dataclass(frozen=True)
class PreparedAlert:
    """Evaluated alert paired with delivery suppression decision and formatted message."""

    signal: AlertSignal
    suppress_delivery: bool
    message_text: str


class AlertScannerRule(Protocol):
    """Protocol for pure live bar alert scanner rules."""

    def evaluate(self, event: AggregatedBarEvent) -> list[AlertSignal]:
        """Evaluate an AggregatedBarEvent and return zero or more candidate signals."""
        ...


def format_vietnam_time(timestamp_utc_sec: int) -> str:
    """Format a UTC Unix timestamp in Asia/Ho_Chi_Minh (UTC+7)."""
    dt_vn = datetime.fromtimestamp(int(timestamp_utc_sec), tz=UTC).astimezone(VN_TZ)
    return dt_vn.strftime("%Y-%m-%d %H:%M:%S UTC+7")


def format_alert_message(signal: AlertSignal) -> str:
    """Render a concise human-readable Telegram entry alert message."""
    symbol = signal.root_symbol or infer_root_symbol(signal.contract_id) or signal.contract_id
    mode_label = "LIVE" if signal.live_mode else "SIM"
    vn_time = format_vietnam_time(signal.bar_time)
    direction = signal.direction.strip().upper()
    return (
        f"[{mode_label}] {symbol} ({signal.contract_id}) | {signal.timeframe} | {direction}\n"
        f"Strategy: {signal.strategy_id} v{signal.strategy_version}\n"
        f"Reason: {signal.reason}\n"
        f"Entry: {signal.entry_price:.4f} | SL: {signal.stop_loss:.4f} | "
        f"TP: {signal.take_profit:.4f} | R:R {signal.rr_ratio:.2f}\n"
        f"Bar Time (VN): {vn_time}"
    )


class AlertEngine:
    """Coordinates pure closed-bar scanner rules and F03 recovery suppression."""

    def __init__(
        self,
        rules: Sequence[AlertScannerRule] = (),
        *,
        enabled: bool = True,
    ) -> None:
        self._rules: list[AlertScannerRule] = list(rules)
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        """Return whether live alert scanning is enabled."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        """Enable or disable the live alert scanner."""
        self._enabled = bool(enabled)
        return self._enabled

    def register_rule(self, rule: AlertScannerRule) -> None:
        """Register an additional scanner rule."""
        self._rules.append(rule)

    def evaluate_bar_event(
        self,
        event: AggregatedBarEvent,
        *,
        signal_tag: str | None = None,
    ) -> list[PreparedAlert]:
        """Evaluate closed bar events and enforce F03 historical suppression."""
        if not self._enabled:
            return []
        if not event.bar.is_closed:
            return []

        is_recovered = event.is_recovered_historical or signal_tag is not None
        effective_tag: str | None = None
        if is_recovered:
            effective_tag = (
                "RECOVERED_HISTORICAL"
                if (event.is_recovered_historical or signal_tag == "RECOVERED_HISTORICAL")
                else signal_tag
            )

        prepared: list[PreparedAlert] = []
        for rule in self._rules:
            signals = rule.evaluate(event)
            for raw_signal in signals:
                resolved_tag = effective_tag if effective_tag is not None else raw_signal.signal_tag
                suppress_delivery = resolved_tag is not None
                if suppress_delivery and resolved_tag != "RECOVERED_HISTORICAL":
                    # Normalize recovery/non-live tags for F03 auditability.
                    resolved_tag = (
                        "RECOVERED_HISTORICAL"
                        if event.is_recovered_historical or signal_tag == "RECOVERED_HISTORICAL"
                        else resolved_tag
                    )
                normalized_signal = AlertSignal(
                    strategy_id=raw_signal.strategy_id,
                    strategy_version=raw_signal.strategy_version,
                    live_mode=raw_signal.live_mode,
                    contract_id=raw_signal.contract_id,
                    root_symbol=raw_signal.root_symbol or event.root_symbol,
                    timeframe=raw_signal.timeframe,
                    bar_time=raw_signal.bar_time,
                    direction=raw_signal.direction.strip().upper(),
                    reason=raw_signal.reason,
                    entry_price=float(raw_signal.entry_price),
                    stop_loss=float(raw_signal.stop_loss),
                    take_profit=float(raw_signal.take_profit),
                    rr_ratio=float(raw_signal.rr_ratio),
                    signal_tag=resolved_tag,
                )
                prepared.append(
                    PreparedAlert(
                        signal=normalized_signal,
                        suppress_delivery=suppress_delivery,
                        message_text=format_alert_message(normalized_signal),
                    )
                )
        return prepared
