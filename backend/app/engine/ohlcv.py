"""Strict OHLCV validation gate and TopstepX bar parser (R04, ARCH-001)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

DEFAULT_MIN_BAR_TIMESTAMP = 946684800  # 2000-01-01T00:00:00Z
MAX_QUERY_TIMESTAMP = 4102444800  # 2100-01-01T00:00:00Z
DEFAULT_MAX_FUTURE_SKEW_SEC = 300

_FRACTIONAL_SEC_RE = re.compile(r"[.,](\d+)")


def validate_query_timestamp(ts: int, field_name: str = "timestamp") -> int:
    """Validate that a query timestamp is a non-bool integer within supported bounds."""
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError(f"{field_name} must be an integer Unix timestamp")
    if ts < DEFAULT_MIN_BAR_TIMESTAMP or ts > MAX_QUERY_TIMESTAMP:
        raise ValueError(
            f"{field_name} ({ts}) out of supported range "
            f"[{DEFAULT_MIN_BAR_TIMESTAMP}, {MAX_QUERY_TIMESTAMP}]"
        )
    return ts


@dataclass(frozen=True)
class OHLCVBar:
    """Validated canonical OHLCV candle (UTC bar-open Unix seconds)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of strict OHLCV validation."""

    bar: OHLCVBar | None
    issue_code: str | None = None
    issue_detail: str | None = None

    @property
    def is_valid(self) -> bool:
        """Return True iff validation succeeded without issues."""
        return self.bar is not None and self.issue_code is None


def parse_topstep_bar(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map TopstepX wire keys {t, o, h, l, c, v} to canonical OHLCV keys without coercion."""
    parsed: dict[str, Any] = {}
    key_map = (
        ("time", "t"),
        ("open", "o"),
        ("high", "h"),
        ("low", "l"),
        ("close", "c"),
        ("volume", "v"),
    )
    for canonical, wire in key_map:
        if canonical in raw:
            parsed[canonical] = raw[canonical]
        elif wire in raw:
            parsed[canonical] = raw[wire]

    if "is_closed" in raw:
        parsed["is_closed"] = raw["is_closed"]
    return parsed


def _parse_timestamp_strict(raw_ts: Any) -> tuple[int | None, str | None, str | None]:
    if raw_ts is None:
        return None, "invalid_timestamp", "Timestamp is missing or null"
    if isinstance(raw_ts, bool):
        return None, "invalid_timestamp", "Timestamp cannot be a boolean"
    if isinstance(raw_ts, int):
        return raw_ts, None, None
    if isinstance(raw_ts, str):
        cleaned = raw_ts.strip()
        if not cleaned:
            return None, "invalid_timestamp", "Timestamp string is empty"
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            return None, "invalid_timestamp", f"Unparseable ISO timestamp: {raw_ts!r}"
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None, "naive_timestamp", f"Naive ISO timestamp missing timezone: {raw_ts!r}"
        frac_match = _FRACTIONAL_SEC_RE.search(cleaned)
        if (frac_match and set(frac_match.group(1)) != {"0"}) or dt.microsecond != 0:
            return None, "invalid_timestamp", f"Fractional timestamp unsupported: {raw_ts!r}"
        return int(dt.timestamp()), None, None
    return None, "invalid_timestamp", f"Unsupported timestamp type: {type(raw_ts).__name__}"


def extract_raw_bar_timestamp(raw: Mapping[str, Any]) -> int | None:
    """Safely parse `time` or `t` to UTC seconds without coercing invalid values."""
    if not isinstance(raw, Mapping):
        return None
    if "time" in raw:
        raw_ts = raw["time"]
    elif "t" in raw:
        raw_ts = raw["t"]
    else:
        return None
    ts, issue_code, _ = _parse_timestamp_strict(raw_ts)
    if issue_code is not None:
        return None
    return ts


def _validate_price(
    raw: Mapping[str, Any], field: str
) -> tuple[float | None, str | None, str | None]:
    if field not in raw or raw[field] is None:
        return None, "missing_price", f"Missing or null price field '{field}'"
    val = raw[field]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None, "non_finite_price", f"Invalid price type for '{field}': {val!r}"
    try:
        fval = float(val)
    except OverflowError:
        return None, "non_finite_price", f"Overflow price for '{field}': {val!r}"
    if not math.isfinite(fval):
        return None, "non_finite_price", f"Non-finite price for '{field}': {val!r}"
    if fval <= 0.0:
        return None, "non_positive_price", f"Price '{field}' must be > 0, got {fval}"
    return fval, None, None


def _validate_volume(raw: Mapping[str, Any]) -> tuple[float | None, str | None, str | None]:
    if "volume" not in raw or raw["volume"] is None:
        return None, "missing_volume", "Missing or null 'volume' field"
    val = raw["volume"]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None, "non_finite_volume", f"Invalid volume type: {val!r}"
    try:
        fvol = float(val)
    except OverflowError:
        return None, "non_finite_volume", f"Overflow volume: {val!r}"
    if not math.isfinite(fvol):
        return None, "non_finite_volume", f"Non-finite volume: {val!r}"
    if fvol < 0.0:
        return None, "negative_volume", f"Volume must be >= 0, got {fvol}"
    return fvol, None, None


def validate_ohlcv_bar(
    raw: Mapping[str, Any],
    *,
    is_closed: bool | None = None,
    min_timestamp: int = DEFAULT_MIN_BAR_TIMESTAMP,
    max_timestamp: int | None = None,
    now_utc: int | None = None,
    max_future_skew_sec: int = DEFAULT_MAX_FUTURE_SKEW_SEC,
) -> ValidationResult:
    """Strictly validate a candidate OHLCV record before caching or publishing."""
    if not isinstance(raw, Mapping):
        return ValidationResult(
            bar=None,
            issue_code="invalid_record",
            issue_detail=f"Expected mapping record, got {type(raw).__name__}",
        )

    raw_ts = raw["time"] if "time" in raw else None
    ts, ts_code, ts_detail = _parse_timestamp_strict(raw_ts)
    if ts_code is not None or ts is None:
        return ValidationResult(bar=None, issue_code=ts_code, issue_detail=ts_detail)

    effective_now = now_utc if now_utc is not None else int(datetime.now(UTC).timestamp())
    effective_max = (
        max_timestamp if max_timestamp is not None else effective_now + max_future_skew_sec
    )
    if ts < min_timestamp or ts > effective_max:
        return ValidationResult(
            bar=None,
            issue_code="timestamp_out_of_bounds",
            issue_detail=f"Timestamp {ts} outside [{min_timestamp}, {effective_max}]",
        )

    prices: dict[str, float] = {}
    for field in ("open", "high", "low", "close"):
        p_val, p_code, p_detail = _validate_price(raw, field)
        if p_code is not None or p_val is None:
            return ValidationResult(bar=None, issue_code=p_code, issue_detail=p_detail)
        prices[field] = p_val

    open_v = prices["open"]
    high_v = prices["high"]
    low_v = prices["low"]
    close_v = prices["close"]

    if low_v > high_v or low_v > min(open_v, close_v) or high_v < max(open_v, close_v):
        return ValidationResult(
            bar=None,
            issue_code="ohlc_bounds_violation",
            issue_detail=(
                f"Inconsistent OHLC bounds: O={open_v}, H={high_v}, L={low_v}, C={close_v}"
            ),
        )

    vol_v, v_code, v_detail = _validate_volume(raw)
    if v_code is not None or vol_v is None:
        return ValidationResult(bar=None, issue_code=v_code, issue_detail=v_detail)

    resolved_closed = is_closed if is_closed is not None else raw.get("is_closed")
    if not isinstance(resolved_closed, bool):
        return ValidationResult(
            bar=None,
            issue_code="invalid_is_closed",
            issue_detail=f"is_closed must be bool, got {resolved_closed!r}",
        )

    return ValidationResult(
        bar=OHLCVBar(
            time=ts,
            open=open_v,
            high=high_v,
            low=low_v,
            close=close_v,
            volume=vol_v,
            is_closed=resolved_closed,
        )
    )
