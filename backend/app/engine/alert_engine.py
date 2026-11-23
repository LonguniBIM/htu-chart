"""Pure alert evaluation, formatting, and CME weekend window helpers (F03, F07, ARCH-001)."""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from app.engine.bar_aggregator import AggregatedBarEvent
from app.engine.instruments import infer_root_symbol
from app.engine.session_calendar import NY_TZ, normalize_timeframe

AlertDirection = Literal["LONG", "SHORT", "INFO"]
AlertKind = Literal["ENTRY", "KEY_ROTATION", "SYSTEM", "TEST"]

_DIRECTION_MAP: dict[str, AlertDirection] = {
    "LONG": "LONG",
    "SHORT": "SHORT",
    "INFO": "INFO",
}
_KIND_MAP: dict[str, AlertKind] = {
    "ENTRY": "ENTRY",
    "KEY_ROTATION": "KEY_ROTATION",
    "SYSTEM": "SYSTEM",
    "TEST": "TEST",
}
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class AlertCandidate:
    """Pure strategy or system alert candidate emitted before outbox persistence."""

    strategy_id: str
    strategy_version: str
    live_mode: bool
    contract_id: str
    root_symbol: str | None
    timeframe: str
    bar_time: int
    direction: AlertDirection
    alert_kind: AlertKind = "ENTRY"
    reason: str = ""
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    message_text: str | None = None
    ttl_seconds: int | None = None


class BarSignalEvaluator(Protocol):
    """Pure callable evaluating an AggregatedBarEvent into zero or more AlertCandidates."""

    def __call__(self, event: AggregatedBarEvent) -> Sequence[AlertCandidate]:
        """Evaluate bar event and return normalized alert candidates."""
        ...


def normalize_candidate(
    candidate: AlertCandidate,
    *,
    default_strategy_id: str = "",
) -> AlertCandidate:
    """Validate and normalize an AlertCandidate, inferring root_symbol when omitted."""
    strategy_id = (candidate.strategy_id or default_strategy_id).strip()
    if not strategy_id:
        raise ValueError("AlertCandidate.strategy_id must be non-empty")
    strategy_version = (candidate.strategy_version or "1").strip()
    if not strategy_version:
        raise ValueError("AlertCandidate.strategy_version must be non-empty")
    contract_id = candidate.contract_id.strip().upper()
    if not contract_id:
        raise ValueError("AlertCandidate.contract_id must be non-empty")
    timeframe = normalize_timeframe(candidate.timeframe)
    raw_dir = candidate.direction.strip().upper()
    if raw_dir not in _DIRECTION_MAP:
        raise ValueError(f"Unsupported AlertDirection: {candidate.direction!r}")
    raw_kind = candidate.alert_kind.strip().upper()
    if raw_kind not in _KIND_MAP:
        raise ValueError(f"Unsupported AlertKind: {candidate.alert_kind!r}")
    if isinstance(candidate.bar_time, bool) or not isinstance(candidate.bar_time, int):
        raise ValueError("AlertCandidate.bar_time must be an integer Unix timestamp")
    if candidate.ttl_seconds is not None and candidate.ttl_seconds < 1:
        raise ValueError("AlertCandidate.ttl_seconds must be >= 1 when provided")

    eff_root = (
        candidate.root_symbol.strip().upper()
        if candidate.root_symbol and candidate.root_symbol.strip()
        else infer_root_symbol(contract_id)
    )
    return AlertCandidate(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        live_mode=bool(candidate.live_mode),
        contract_id=contract_id,
        root_symbol=eff_root,
        timeframe=timeframe,
        bar_time=candidate.bar_time,
        direction=_DIRECTION_MAP[raw_dir],
        alert_kind=_KIND_MAP[raw_kind],
        reason=candidate.reason.strip(),
        entry_price=candidate.entry_price,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        risk_reward=candidate.risk_reward,
        message_text=candidate.message_text,
        ttl_seconds=candidate.ttl_seconds,
    )


class AlertEngine:
    """Pure SignalR-gated strategy evaluator enforcing F03 suppression invariants."""

    def __init__(self, *, scanner_enabled: bool = True) -> None:
        self._scanner_enabled = bool(scanner_enabled)
        self._evaluators: dict[str, tuple[BarSignalEvaluator, bool]] = {}
        self._suppressed_signals = 0
        self._recovered_historical_skipped = 0
        self._lock = threading.Lock()

    @property
    def scanner_enabled(self) -> bool:
        """Whether live strategy signal scanning is currently enabled."""
        with self._lock:
            return self._scanner_enabled

    def set_scanner_enabled(self, enabled: bool) -> None:
        """Enable or disable live strategy signal scanning."""
        with self._lock:
            self._scanner_enabled = bool(enabled)

    @property
    def suppressed_signals(self) -> int:
        """Count of bar events suppressed due to non-LIVE state or scanner kill-switch."""
        with self._lock:
            return self._suppressed_signals

    @property
    def recovered_historical_skipped(self) -> int:
        """Count of RECOVERED_HISTORICAL bar events skipped from live alerting."""
        with self._lock:
            return self._recovered_historical_skipped

    def register_evaluator(
        self,
        strategy_id: str,
        fn: BarSignalEvaluator,
        *,
        allow_provisional: bool = False,
    ) -> None:
        """Register or replace a named pure BarSignalEvaluator."""
        clean_id = strategy_id.strip() if isinstance(strategy_id, str) else ""
        if not clean_id:
            raise ValueError("strategy_id must be a non-empty string")
        if not callable(fn):
            raise ValueError("evaluator fn must be callable")
        with self._lock:
            self._evaluators[clean_id] = (fn, bool(allow_provisional))

    def unregister_evaluator(self, strategy_id: str) -> bool:
        """Remove a registered evaluator by strategy_id; return True if removed."""
        clean_id = strategy_id.strip() if isinstance(strategy_id, str) else ""
        if not clean_id:
            return False
        with self._lock:
            return self._evaluators.pop(clean_id, None) is not None

    def evaluate_bar_signal(
        self,
        event: AggregatedBarEvent,
        tag: str | None,
    ) -> tuple[AlertCandidate, ...]:
        """Evaluate an emitted bar event strictly obeying F03 LIVE-only gating."""
        if tag == "RECOVERED_HISTORICAL" or event.is_recovered_historical:
            with self._lock:
                self._recovered_historical_skipped += 1
            return ()

        with self._lock:
            if tag is not None or not self._scanner_enabled:
                self._suppressed_signals += 1
                return ()
            evaluators = list(self._evaluators.items())

        if not evaluators:
            return ()

        candidates: list[AlertCandidate] = []
        for strategy_id, (fn, allow_provisional) in evaluators:
            if not event.bar.is_closed and not allow_provisional:
                continue
            emitted = fn(event)
            for item in emitted:
                candidates.append(
                    normalize_candidate(item, default_strategy_id=strategy_id)
                )
        return tuple(candidates)


def _format_price(value: float | None) -> str | None:
    if value is None or not math.isfinite(value):
        return None
    return f"{value:.4f}".rstrip("0").rstrip(".")


def format_vn_timestamp(ts_utc: int) -> str:
    """Format UTC Unix seconds in Asia/Ho_Chi_Minh (UTC+7) display format."""
    vn_dt = datetime.fromtimestamp(ts_utc, tz=UTC).astimezone(VN_TZ)
    return f"{vn_dt.strftime('%Y-%m-%d %H:%M:%S')} ICT (UTC+7)"


def format_entry_alert_message(candidate: AlertCandidate, created_at_utc: int) -> str:
    """Format a human-readable Telegram alert message with Vietnam (UTC+7) timestamp."""
    symbol_label = (
        f"{candidate.root_symbol} ({candidate.contract_id})"
        if candidate.root_symbol and candidate.root_symbol != candidate.contract_id
        else candidate.contract_id
    )
    mode_label = "LIVE" if candidate.live_mode else "SIM"
    lines = [
        f"[{mode_label}] {candidate.direction} {symbol_label} {candidate.timeframe}",
        f"Strategy: {candidate.strategy_id} v{candidate.strategy_version}",
    ]
    if candidate.reason:
        lines.append(f"Reason: {candidate.reason}")

    levels: list[str] = []
    entry_str = _format_price(candidate.entry_price)
    sl_str = _format_price(candidate.stop_loss)
    tp_str = _format_price(candidate.take_profit)
    rr_str = _format_price(candidate.risk_reward)
    if entry_str is not None:
        levels.append(f"Entry: {entry_str}")
    if sl_str is not None:
        levels.append(f"SL: {sl_str}")
    if tp_str is not None:
        levels.append(f"TP: {tp_str}")
    if rr_str is not None:
        levels.append(f"R:R: {rr_str}")
    if levels:
        lines.append(" | ".join(levels))

    lines.append(f"Bar: {format_vn_timestamp(candidate.bar_time)}")
    lines.append(f"Time: {format_vn_timestamp(created_at_utc)}")
    return "\n".join(lines)


def is_cme_weekend_window(now_utc: int) -> bool:
    """Return True iff now_utc falls in CME Globex weekend closure (Fri>=17:00..Sun<18:00 ET)."""
    ny_dt = datetime.fromtimestamp(now_utc, tz=UTC).astimezone(NY_TZ)
    wd = ny_dt.weekday()
    if wd == 4:
        return ny_dt.hour >= 17
    if wd == 5:
        return True
    if wd == 6:
        return ny_dt.hour < 18
    return False


def cme_weekend_window_bounds(now_utc: int) -> tuple[int, int] | None:
    """Return (friday_1700_et_utc, sunday_1800_et_utc) inside weekend closure, else None."""
    if not is_cme_weekend_window(now_utc):
        return None
    ny_dt = datetime.fromtimestamp(now_utc, tz=UTC).astimezone(NY_TZ)
    wd = ny_dt.weekday()
    if wd == 4:
        friday_date = ny_dt.date()
    elif wd == 5:
        friday_date = ny_dt.date() - timedelta(days=1)
    else:
        friday_date = ny_dt.date() - timedelta(days=2)
    sunday_date = friday_date + timedelta(days=2)

    fri_dt = datetime.combine(friday_date, time(17, 0), tzinfo=NY_TZ)
    sun_dt = datetime.combine(sunday_date, time(18, 0), tzinfo=NY_TZ)
    return int(fri_dt.timestamp()), int(sun_dt.timestamp())


def build_key_rotation_reminder_candidate(
    key_age_days: int,
    rotation_days: int,
    enforced: bool,
    now_utc: int,
) -> AlertCandidate | None:
    """Build a per-weekend TopstepX API key rotation reminder candidate, or None on weekdays."""
    bounds = cme_weekend_window_bounds(now_utc)
    if bounds is None:
        return None
    fri_utc, sun_utc = bounds
    ttl_seconds = max(1, sun_utc - now_utc)
    policy_label = "ENFORCED (connections blocked until rotated)" if enforced else "Soft reminder"
    message_text = "\n".join(
        [
            "TopstepX API Key Rotation Reminder (Weekend Maintenance)",
            f"Key age: {key_age_days} days (threshold: {rotation_days} days) - {policy_label}",
            "Rotate your API key safely while CME Globex is closed:",
            "https://topstepx.com/settings?tab=api",
            f"Window closes: {format_vn_timestamp(sun_utc)}",
        ]
    )
    return AlertCandidate(
        strategy_id="__system_key_rotation__",
        strategy_version="1",
        live_mode=False,
        contract_id="__SYSTEM__",
        root_symbol=None,
        timeframe="1d",
        bar_time=fri_utc,
        direction="INFO",
        alert_kind="KEY_ROTATION",
        reason=f"TopstepX API key age {key_age_days}d >= {rotation_days}d",
        message_text=message_text,
        ttl_seconds=ttl_seconds,
    )
