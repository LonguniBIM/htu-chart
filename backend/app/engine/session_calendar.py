"""Shared CME Globex session calendar and timeframe bucketing (R05, ARCH-001)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

TOPSTEP_UNITS: dict[str, tuple[int, int]] = {
    "1m": (2, 1),
    "3m": (2, 3),
    "5m": (2, 5),
    "15m": (2, 15),
    "30m": (2, 30),
    "1h": (3, 1),
    "4h": (3, 4),
    "1d": (4, 1),
}

_UNIT_TO_TIMEFRAME: dict[tuple[int, int], str] = {
    pair: tf for tf, pair in TOPSTEP_UNITS.items()
}


@dataclass(frozen=True)
class HolidaySpec:
    """CME Globex holiday schedule specification for a single trading date."""

    name: str
    is_full_close: bool
    early_close_et: time | None = None


@dataclass(frozen=True)
class SessionSchedule:
    """Resolved CME Globex session boundaries (UTC Unix seconds) for a trading date."""

    trading_date: date
    open_utc: int
    close_utc: int | None
    vwap_reset_utc: int
    is_holiday: bool
    is_early_close: bool
    holiday_name: str | None


def normalize_timeframe(tf: str) -> str:
    """Normalize a timeframe string and reject unsupported intervals."""
    if not isinstance(tf, str):
        raise ValueError(f"Timeframe must be a string, got {type(tf).__name__}")
    normalized = tf.strip().lower()
    if normalized not in TOPSTEP_UNITS:
        raise ValueError(f"Unsupported timeframe: {tf!r}")
    return normalized


def timeframe_to_topstep_unit(tf: str) -> tuple[int, int]:
    """Convert canonical timeframe string into TopstepX (unit, unitNumber) pair."""
    normalized = normalize_timeframe(tf)
    return TOPSTEP_UNITS[normalized]


def timeframe_from_topstep_unit(unit: int, unit_number: int) -> str:
    """Convert TopstepX (unit, unitNumber) pair into canonical timeframe string."""
    if isinstance(unit, bool) or isinstance(unit_number, bool):
        raise ValueError("TopstepX unit and unitNumber must be integers, not booleans")
    pair = (int(unit), int(unit_number))
    if pair not in _UNIT_TO_TIMEFRAME:
        raise ValueError(
            f"Unsupported TopstepX unit pair: unit={unit!r}, unitNumber={unit_number!r}"
        )
    return _UNIT_TO_TIMEFRAME[pair]


def _gregorian_easter(year: int) -> date:
    """Compute Gregorian Easter Sunday using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_weekday(target: date) -> date:
    """Shift weekend fixed-date holidays to Friday (Sat) or Monday (Sun)."""
    wd = target.weekday()
    if wd == 5:
        return target - timedelta(days=1)
    if wd == 6:
        return target + timedelta(days=1)
    return target


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th (1-indexed) occurrence of weekday (0=Mon..6=Sun) in month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in month."""
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _is_metals_symbol(root_symbol: str | None) -> bool:
    return root_symbol is not None and root_symbol.strip().upper() == "MGC"


@lru_cache(maxsize=64)
def _cme_holidays_for_year(year: int, is_metals: bool) -> dict[date, HolidaySpec]:
    """Build algorithmic US / CME Globex holiday specs for a calendar year."""
    holidays: dict[date, HolidaySpec] = {}
    standard_early = time(14, 30) if is_metals else time(13, 0)
    eve_early = time(13, 45) if is_metals else time(13, 15)

    # Early-close weekday holidays
    mlk = _nth_weekday_of_month(year, 1, 0, 3)
    holidays[mlk] = HolidaySpec(
        name="Martin Luther King Jr. Day",
        is_full_close=False,
        early_close_et=standard_early,
    )
    presidents = _nth_weekday_of_month(year, 2, 0, 3)
    holidays[presidents] = HolidaySpec(
        name="Presidents' Day",
        is_full_close=False,
        early_close_et=standard_early,
    )
    memorial = _last_weekday_of_month(year, 5, 0)
    holidays[memorial] = HolidaySpec(
        name="Memorial Day",
        is_full_close=False,
        early_close_et=standard_early,
    )
    if year >= 2021:
        juneteenth = _observed_weekday(date(year, 6, 19))
        if juneteenth.year == year:
            holidays[juneteenth] = HolidaySpec(
                name="Juneteenth",
                is_full_close=False,
                early_close_et=standard_early,
            )
    independence = _observed_weekday(date(year, 7, 4))
    if independence.year == year:
        holidays[independence] = HolidaySpec(
            name="Independence Day",
            is_full_close=False,
            early_close_et=standard_early,
        )
    labor = _nth_weekday_of_month(year, 9, 0, 1)
    holidays[labor] = HolidaySpec(
        name="Labor Day",
        is_full_close=False,
        early_close_et=standard_early,
    )
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    holidays[thanksgiving] = HolidaySpec(
        name="Thanksgiving Day",
        is_full_close=False,
        early_close_et=standard_early,
    )
    day_after_thanksgiving = thanksgiving + timedelta(days=1)
    holidays[day_after_thanksgiving] = HolidaySpec(
        name="Day After Thanksgiving",
        is_full_close=False,
        early_close_et=eve_early,
    )
    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 4:
        holidays[christmas_eve] = HolidaySpec(
            name="Christmas Eve",
            is_full_close=False,
            early_close_et=eve_early,
        )

    # Full-closure holidays take precedence over any coinciding early close.
    for target_year in (year, year + 1):
        nyd = _observed_weekday(date(target_year, 1, 1))
        if nyd.year == year:
            holidays[nyd] = HolidaySpec(name="New Year's Day", is_full_close=True)

    good_friday = _gregorian_easter(year) - timedelta(days=2)
    holidays[good_friday] = HolidaySpec(name="Good Friday", is_full_close=True)

    christmas = _observed_weekday(date(year, 12, 25))
    if christmas.year == year:
        holidays[christmas] = HolidaySpec(name="Christmas Day", is_full_close=True)

    return holidays


def _globex_anchor_date(ts: int) -> date:
    ny_dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(NY_TZ)
    if ny_dt.hour >= 18:
        return ny_dt.date()
    return ny_dt.date() - timedelta(days=1)


def globex_day_open_utc(ts: int) -> int:
    """Return the 18:00 America/New_York open timestamp (UTC seconds) containing ts."""
    anchor_date = _globex_anchor_date(ts)
    open_dt = datetime.combine(anchor_date, time(18, 0), tzinfo=NY_TZ)
    return int(open_dt.timestamp())


def vwap_session_open_utc(ts: int) -> int:
    """Return the 18:00 America/New_York VWAP reset timestamp (UTC seconds) for ts."""
    return globex_day_open_utc(ts)


def session_trading_date(ts: int) -> date:
    """Return the CME Globex trading date containing ts (anchor_date + 1 day)."""
    return _globex_anchor_date(ts) + timedelta(days=1)


def _next_globex_day_open_utc(anchor_date: date) -> int:
    next_dt = datetime.combine(anchor_date + timedelta(days=1), time(18, 0), tzinfo=NY_TZ)
    return int(next_dt.timestamp())


def get_session_schedule(
    trading_date: date,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> SessionSchedule:
    """Return the CME Globex session schedule for a trading date."""
    open_dt = datetime.combine(trading_date - timedelta(days=1), time(18, 0), tzinfo=NY_TZ)
    open_utc = int(open_dt.timestamp())

    spec: HolidaySpec | None = None
    if holiday_overrides is not None and trading_date in holiday_overrides:
        spec = holiday_overrides[trading_date]
    else:
        spec = _cme_holidays_for_year(trading_date.year, _is_metals_symbol(root_symbol)).get(
            trading_date
        )

    if trading_date.weekday() >= 5:
        return SessionSchedule(
            trading_date=trading_date,
            open_utc=open_utc,
            close_utc=None,
            vwap_reset_utc=open_utc,
            is_holiday=bool(spec is not None and spec.is_full_close),
            is_early_close=False,
            holiday_name=spec.name if spec is not None else None,
        )

    if spec is not None:
        if spec.is_full_close:
            return SessionSchedule(
                trading_date=trading_date,
                open_utc=open_utc,
                close_utc=None,
                vwap_reset_utc=open_utc,
                is_holiday=True,
                is_early_close=False,
                holiday_name=spec.name,
            )
        if spec.early_close_et is None:
            raise ValueError(
                f"HolidaySpec {spec.name!r} requires early_close_et when not full close"
            )
        close_dt = datetime.combine(trading_date, spec.early_close_et, tzinfo=NY_TZ)
        return SessionSchedule(
            trading_date=trading_date,
            open_utc=open_utc,
            close_utc=int(close_dt.timestamp()),
            vwap_reset_utc=open_utc,
            is_holiday=True,
            is_early_close=True,
            holiday_name=spec.name,
        )

    close_dt = datetime.combine(trading_date, time(17, 0), tzinfo=NY_TZ)
    return SessionSchedule(
        trading_date=trading_date,
        open_utc=open_utc,
        close_utc=int(close_dt.timestamp()),
        vwap_reset_utc=open_utc,
        is_holiday=False,
        is_early_close=False,
        holiday_name=None,
    )


def is_market_open(
    ts: int,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> bool:
    """Return True iff ts falls inside an open CME Globex session [open_utc, close_utc)."""
    schedule = get_session_schedule(
        session_trading_date(ts),
        root_symbol=root_symbol,
        holiday_overrides=holiday_overrides,
    )
    return schedule.close_utc is not None and schedule.open_utc <= ts < schedule.close_utc


def has_open_session_in_range(
    start_ts: int,
    end_ts: int,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> bool:
    """Return True iff [start_ts, end_ts) intersects at least 1s of an open Globex session."""
    if start_ts >= end_ts:
        return False
    first_td = session_trading_date(start_ts)
    last_td = session_trading_date(end_ts - 1)
    cursor = first_td
    while cursor <= last_td:
        schedule = get_session_schedule(
            cursor,
            root_symbol=root_symbol,
            holiday_overrides=holiday_overrides,
        )
        if (
            schedule.close_utc is not None
            and schedule.open_utc < end_ts
            and schedule.close_utc > start_ts
        ):
            return True
        cursor += timedelta(days=1)
    return False


def bucket_open_utc(ts: int, timeframe: str) -> int:
    """Compute canonical CME Globex bar-open timestamp (UTC seconds) for ts."""
    tf = normalize_timeframe(timeframe)
    session_open = globex_day_open_utc(ts)
    if tf == "1d":
        return session_open
    step = TIMEFRAME_SECONDS[tf]
    return session_open + ((ts - session_open) // step) * step


def first_bucket_open_at_or_after(ts: int, timeframe: str) -> int:
    """Return the first canonical CME Globex bar-open timestamp >= ts for timeframe."""
    tf = normalize_timeframe(timeframe)
    b_start = bucket_open_utc(ts, tf)
    if b_start == ts:
        return ts
    anchor = _globex_anchor_date(ts)
    next_day_open = _next_globex_day_open_utc(anchor)
    if tf == "1d":
        return next_day_open
    step = TIMEFRAME_SECONDS[tf]
    return min(b_start + step, next_day_open)


def assign_bucket_open_utc(timestamps: Sequence[int], timeframe: str) -> list[int]:
    """Batch-assign canonical bar-open timestamps identical to scalar bucket_open_utc."""
    tf = normalize_timeframe(timeframe)
    if not timestamps:
        return []

    is_daily = tf == "1d"
    step = TIMEFRAME_SECONDS[tf]
    out: list[int] = []
    cur_open = 0
    cur_next_open = 0
    has_window = False

    for ts in timestamps:
        if not has_window or not (cur_open <= ts < cur_next_open):
            anchor = _globex_anchor_date(ts)
            cur_open = int(datetime.combine(anchor, time(18, 0), tzinfo=NY_TZ).timestamp())
            cur_next_open = _next_globex_day_open_utc(anchor)
            has_window = True
        if is_daily:
            out.append(cur_open)
        else:
            out.append(cur_open + ((ts - cur_open) // step) * step)
    return out


def bucket_bounds(
    ts: int,
    timeframe: str,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> tuple[int, int]:
    """Return [start, end) bounds for the bucket containing ts, clamped to session close."""
    tf = normalize_timeframe(timeframe)
    start = bucket_open_utc(ts, tf)
    anchor_date = _globex_anchor_date(ts)
    next_open = _next_globex_day_open_utc(anchor_date)
    schedule = get_session_schedule(
        anchor_date + timedelta(days=1),
        root_symbol=root_symbol,
        holiday_overrides=holiday_overrides,
    )

    if tf == "1d":
        if schedule.close_utc is not None and start <= ts < schedule.close_utc:
            return start, schedule.close_utc
        return start, next_open

    step = TIMEFRAME_SECONDS[tf]
    nominal_end = min(start + step, next_open)
    if (
        schedule.close_utc is not None
        and start < schedule.close_utc < nominal_end
        and ts < schedule.close_utc
    ):
        return start, schedule.close_utc
    return start, nominal_end


def is_bar_closed(
    bar_open_utc: int,
    timeframe: str,
    now_utc: int,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> bool:
    """Return True iff the candle starting at bar_open_utc has closed by now_utc."""
    _, end_utc = bucket_bounds(
        bar_open_utc,
        timeframe,
        root_symbol=root_symbol,
        holiday_overrides=holiday_overrides,
    )
    return now_utc >= end_utc


def latest_closed_bucket_end_utc(
    horizon_utc: int,
    timeframe: str,
    *,
    root_symbol: str | None = None,
    holiday_overrides: Mapping[date, HolidaySpec] | None = None,
) -> int:
    """Return the latest finalized bar-close boundary <= horizon_utc."""
    start, end = bucket_bounds(
        horizon_utc,
        timeframe,
        root_symbol=root_symbol,
        holiday_overrides=holiday_overrides,
    )
    if horizon_utc >= end:
        return end

    schedule = get_session_schedule(
        session_trading_date(horizon_utc),
        root_symbol=root_symbol,
        holiday_overrides=holiday_overrides,
    )
    if (
        not is_market_open(
            horizon_utc,
            root_symbol=root_symbol,
            holiday_overrides=holiday_overrides,
        )
        and schedule.close_utc is not None
        and horizon_utc >= schedule.close_utc
    ):
        return schedule.close_utc
    return start
