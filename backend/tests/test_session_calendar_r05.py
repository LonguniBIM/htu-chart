"""Value-first tests for R05 shared CME Globex session calendar and bucketing."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from app.engine.session_calendar import (
    NY_TZ,
    TIMEFRAME_SECONDS,
    HolidaySpec,
    assign_bucket_open_utc,
    bucket_bounds,
    bucket_open_utc,
    first_bucket_open_at_or_after,
    get_session_schedule,
    globex_day_open_utc,
    has_open_session_in_range,
    is_bar_closed,
    is_market_open,
    latest_closed_bucket_end_utc,
    normalize_timeframe,
    session_trading_date,
    timeframe_from_topstep_unit,
    timeframe_to_topstep_unit,
    vwap_session_open_utc,
)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def test_r05_est_and_edt_globex_reset_and_divergence_from_utc_midnight() -> None:
    """Contract: Globex day and VWAP reset at 18:00 America/New_York (23:00Z EST, 22:00Z EDT).

    Defect caught: Naive 00:00Z daily bucketing or hardcoded UTC-5 offset ignoring DST.
    """
    # EST winter date: 2026-01-14 (UTC-5) -> 18:00 EST == 23:00Z
    est_before_halt = _ts(datetime(2026, 1, 14, 16, 59, tzinfo=NY_TZ))  # 2026-01-14T21:59Z
    est_open = _ts(datetime(2026, 1, 14, 18, 0, tzinfo=NY_TZ))  # 2026-01-14T23:00Z
    est_after_utc_midnight = _ts(datetime(2026, 1, 15, 0, 30, tzinfo=UTC))  # 2026-01-14 19:30 EST

    assert est_open == _ts(datetime(2026, 1, 14, 23, 0, tzinfo=UTC))
    assert session_trading_date(est_before_halt) == date(2026, 1, 14)
    assert globex_day_open_utc(est_before_halt) == _ts(datetime(2026, 1, 13, 23, 0, tzinfo=UTC))

    assert session_trading_date(est_open) == date(2026, 1, 15)
    assert globex_day_open_utc(est_open) == est_open
    assert vwap_session_open_utc(est_after_utc_midnight) == est_open
    # Diverges from naive 00:00Z (2026-01-15T00:00Z)
    assert bucket_open_utc(est_after_utc_midnight, "1d") == est_open
    assert bucket_open_utc(est_after_utc_midnight, "1d") != _ts(
        datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    )

    # EDT summer date: 2026-07-15 (UTC-4) -> 18:00 EDT == 22:00Z
    edt_open = _ts(datetime(2026, 7, 15, 18, 0, tzinfo=NY_TZ))
    edt_after_utc_midnight = _ts(datetime(2026, 7, 16, 1, 15, tzinfo=UTC))
    assert edt_open == _ts(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    assert vwap_session_open_utc(edt_after_utc_midnight) == edt_open
    assert bucket_open_utc(edt_after_utc_midnight, "1d") == edt_open


def test_r05_dst_spring_forward_and_fall_back_transitions() -> None:
    """Contract: 2026-03-08 Spring Forward and 2026-11-01 Fall Back preserve 18:00 ET anchors.

    Defect caught: Fixed 86400s day steps drifting 1h across US DST transitions.
    """
    # Spring Forward 2026-03-08: Sat 18:00 EST (23:00Z) -> Sun 18:00 EDT (22:00Z) = 23 hours
    sat_est = _ts(datetime(2026, 3, 7, 18, 0, tzinfo=NY_TZ))
    sun_edt = _ts(datetime(2026, 3, 8, 18, 0, tzinfo=NY_TZ))
    assert sun_edt - sat_est == 23 * 3600

    mon_sched = get_session_schedule(date(2026, 3, 9))
    assert mon_sched.open_utc == sun_edt
    assert mon_sched.close_utc == _ts(datetime(2026, 3, 9, 17, 0, tzinfo=NY_TZ))
    assert mon_sched.vwap_reset_utc == sun_edt
    spring_mon_mid = _ts(datetime(2026, 3, 9, 9, 30, tzinfo=NY_TZ))
    assert bucket_open_utc(spring_mon_mid, "1d") == sun_edt
    assert vwap_session_open_utc(spring_mon_mid) == sun_edt

    # 4h bar just before Sunday 18:00 EDT clamps to 23h boundary
    pre_open = sun_edt - 60
    b_start, b_end = bucket_bounds(pre_open, "4h")
    assert b_start == sat_est + 20 * 3600
    assert b_end == sun_edt

    # Fall Back 2026-11-01: Sat 18:00 EDT (22:00Z) -> Sun 18:00 EST (23:00Z) = 25 hours
    sat_edt = _ts(datetime(2026, 10, 31, 18, 0, tzinfo=NY_TZ))
    sun_est = _ts(datetime(2026, 11, 1, 18, 0, tzinfo=NY_TZ))
    assert sun_est - sat_edt == 25 * 3600

    # 4h buckets across Fall Back 25h weekend transition
    assert bucket_open_utc(sat_edt + 24 * 3600, "4h") == sat_edt + 24 * 3600
    assert bucket_bounds(sun_est - 60, "4h") == (sat_edt + 24 * 3600, sun_est)
    assert bucket_open_utc(sun_est + 3600, "4h") == sun_est

    nov2_sched = get_session_schedule(date(2026, 11, 2))
    assert nov2_sched.open_utc == sun_est
    assert nov2_sched.close_utc == _ts(datetime(2026, 11, 2, 17, 0, tzinfo=NY_TZ))
    assert vwap_session_open_utc(_ts(datetime(2026, 11, 2, 10, 0, tzinfo=NY_TZ))) == sun_est


def test_r05_maintenance_halt_weekends_holidays_and_early_close_clamping() -> None:
    """Contract: Market halts/weekends/holidays/early closes clamp bounds and close status.

    Defect caught: Treating 17:00-18:00 ET halt or holiday early-close bars as open or unclosed.
    """
    # Daily maintenance halt 17:00-18:00 ET on Wednesday 2026-04-15
    assert is_market_open(_ts(datetime(2026, 4, 15, 16, 59, 59, tzinfo=NY_TZ))) is True
    assert is_market_open(_ts(datetime(2026, 4, 15, 17, 0, 0, tzinfo=NY_TZ))) is False
    assert is_market_open(_ts(datetime(2026, 4, 15, 17, 59, 59, tzinfo=NY_TZ))) is False
    assert is_market_open(_ts(datetime(2026, 4, 15, 18, 0, 0, tzinfo=NY_TZ))) is True

    # Weekend closure: Fri 17:00 ET through Sun 18:00 ET
    fri_close = _ts(datetime(2026, 4, 17, 17, 0, tzinfo=NY_TZ))
    sun_reopen = _ts(datetime(2026, 4, 19, 18, 0, tzinfo=NY_TZ))
    assert has_open_session_in_range(fri_close, sun_reopen) is False
    assert has_open_session_in_range(fri_close - 1, sun_reopen) is True
    assert has_open_session_in_range(fri_close, sun_reopen + 1) is True

    # Full holiday closures: Good Friday 2026-04-03 and Christmas 2026-12-25
    gf = get_session_schedule(date(2026, 4, 3))
    xmas = get_session_schedule(date(2026, 12, 25))
    assert gf.is_holiday is True and gf.close_utc is None
    assert xmas.is_holiday is True and xmas.close_utc is None
    assert is_market_open(_ts(datetime(2026, 4, 3, 10, 0, tzinfo=NY_TZ))) is False

    # Early close divergence: Thanksgiving 2026-11-26 (13:00 ET equity vs 14:30 ET MGC)
    tg_date = date(2026, 11, 26)
    mes_tg = get_session_schedule(tg_date, root_symbol="MES")
    mgc_tg = get_session_schedule(tg_date, root_symbol="MGC")
    assert mes_tg.is_early_close is True
    assert mes_tg.close_utc == _ts(datetime(2026, 11, 26, 13, 0, tzinfo=NY_TZ))
    assert mgc_tg.is_early_close is True
    assert mgc_tg.close_utc == _ts(datetime(2026, 11, 26, 14, 30, tzinfo=NY_TZ))

    # 10:00 ET 4h bar on Thanksgiving closes at 13:00 ET for MES, not 14:00 ET
    bar_10am = _ts(datetime(2026, 11, 26, 10, 0, tzinfo=NY_TZ))
    assert bucket_bounds(bar_10am, "4h", root_symbol="MES") == (
        bar_10am,
        _ts(datetime(2026, 11, 26, 13, 0, tzinfo=NY_TZ)),
    )
    assert is_bar_closed(
        bar_10am, "4h", _ts(datetime(2026, 11, 26, 13, 0, tzinfo=NY_TZ)), root_symbol="MES"
    ) is True
    assert is_bar_closed(
        bar_10am, "4h", _ts(datetime(2026, 11, 26, 13, 0, tzinfo=NY_TZ)), root_symbol="MGC"
    ) is False

    # Custom holiday_overrides support
    custom_day = date(2026, 8, 12)
    overrides = {
        custom_day: HolidaySpec(
            name="Special Exchange Early Close",
            is_full_close=False,
            early_close_et=time(12, 0),
        )
    }
    custom_sched = get_session_schedule(custom_day, holiday_overrides=overrides)
    assert custom_sched.is_early_close is True
    assert custom_sched.close_utc == _ts(datetime(2026, 8, 12, 12, 0, tzinfo=NY_TZ))


def test_r05_assign_bucket_open_matches_scalar_across_all_timeframes_and_dst() -> None:
    """Contract: assign_bucket_open_utc is bit-for-bit identical to scalar bucket_open_utc.

    Defect caught: Batch caching bug producing different bucket boundaries than scalar function.
    """
    sample_times = [
        _ts(datetime(2026, 1, 14, 17, 30, tzinfo=NY_TZ)),
        _ts(datetime(2026, 1, 14, 18, 0, tzinfo=NY_TZ)),
        _ts(datetime(2026, 1, 15, 3, 17, tzinfo=NY_TZ)),
        _ts(datetime(2026, 3, 7, 23, 55, tzinfo=NY_TZ)),
        _ts(datetime(2026, 3, 8, 18, 5, tzinfo=NY_TZ)),
        _ts(datetime(2026, 3, 9, 16, 59, tzinfo=NY_TZ)),
        _ts(datetime(2026, 11, 1, 1, 30, tzinfo=NY_TZ)),
        _ts(datetime(2026, 11, 1, 18, 45, tzinfo=NY_TZ)),
        _ts(datetime(2026, 11, 2, 14, 12, tzinfo=NY_TZ)),
    ]

    for tf in TIMEFRAME_SECONDS:
        batch = assign_bucket_open_utc(sample_times, tf)
        scalar = [bucket_open_utc(t, tf) for t in sample_times]
        assert batch == scalar


def test_r05_latest_closed_bucket_end_and_timeframe_validation() -> None:
    """Contract: latest_closed_bucket_end_utc never includes open bar; invalid TFs rejected.

    Defect caught: Marking an open candle interval COMPLETE or accepting unsupported timeframes.
    """
    # Mid 5m bar (14:02 ET) -> latest closed end is 14:00 ET
    mid_5m = _ts(datetime(2026, 4, 15, 14, 2, tzinfo=NY_TZ))
    assert latest_closed_bucket_end_utc(mid_5m, "5m") == _ts(
        datetime(2026, 4, 15, 14, 0, tzinfo=NY_TZ)
    )

    # During 17:00-18:00 ET halt -> latest closed 4h and 1d end is 17:00 ET
    halt_ts = _ts(datetime(2026, 4, 15, 17, 20, tzinfo=NY_TZ))
    close_1700 = _ts(datetime(2026, 4, 15, 17, 0, tzinfo=NY_TZ))
    assert latest_closed_bucket_end_utc(halt_ts, "4h") == close_1700
    assert latest_closed_bucket_end_utc(halt_ts, "1d") == close_1700

    # Normalization & unit conversions
    assert normalize_timeframe(" 1H ") == "1h"
    assert normalize_timeframe("1D") == "1d"
    assert timeframe_to_topstep_unit("15m") == (2, 15)
    assert timeframe_from_topstep_unit(3, 4) == "4h"

    for bad_tf in ("2m", "1w", "", "unknown"):
        with pytest.raises(ValueError):
            normalize_timeframe(bad_tf)

    for bad_unit in ((9, 1), (2, 99), (True, 1)):
        with pytest.raises(ValueError):
            timeframe_from_topstep_unit(*bad_unit)

    # first_bucket_open_at_or_after across aligned, mid-bar, pre-halt, 1d, and DST
    aligned_1400 = _ts(datetime(2026, 4, 15, 14, 0, tzinfo=NY_TZ))
    mid_1402 = _ts(datetime(2026, 4, 15, 14, 2, tzinfo=NY_TZ))
    assert first_bucket_open_at_or_after(aligned_1400, "5m") == aligned_1400
    assert first_bucket_open_at_or_after(mid_1402, "5m") == _ts(
        datetime(2026, 4, 15, 14, 5, tzinfo=NY_TZ)
    )
    pre_halt_1430 = _ts(datetime(2026, 4, 15, 14, 30, tzinfo=NY_TZ))
    day_reopen_1800 = _ts(datetime(2026, 4, 15, 18, 0, tzinfo=NY_TZ))
    assert first_bucket_open_at_or_after(pre_halt_1430, "4h") == day_reopen_1800
    assert first_bucket_open_at_or_after(pre_halt_1430, "1d") == day_reopen_1800
    assert first_bucket_open_at_or_after(day_reopen_1800, "1d") == day_reopen_1800

    # Spring Forward 2026-03-08 & Fall Back 2026-11-01 clamp to 18:00 ET
    spring_pre = _ts(datetime(2026, 3, 8, 17, 15, tzinfo=NY_TZ))
    assert first_bucket_open_at_or_after(spring_pre, "4h") == _ts(
        datetime(2026, 3, 8, 18, 0, tzinfo=NY_TZ)
    )
    fall_pre = _ts(datetime(2026, 11, 1, 17, 30, tzinfo=NY_TZ))
    assert first_bucket_open_at_or_after(fall_pre, "4h") == _ts(
        datetime(2026, 11, 1, 18, 0, tzinfo=NY_TZ)
    )
