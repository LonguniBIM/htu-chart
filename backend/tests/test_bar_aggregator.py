"""Value-first tests for pure CME Globex BarAggregator and reconciliation (F03, R04, R05)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from app.engine.bar_aggregator import (
    BarAggregator,
    NormalizedTick,
)
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import NY_TZ, bucket_open_utc


def _et_ts(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=NY_TZ).timestamp())


def test_cme_bar_aggregation_across_1m_5m_and_1800_et_globex_boundary() -> None:
    """Contract: Ticks aggregate into R05 bar-open UTC bars across 1m, 5m, and 18:00 ET boundary.

    Defect caught: Naive 00:00 UTC bucketing, wrong OHLCV math, or leaking provisional bars.
    """
    agg = BarAggregator(timeframes=("1m", "5m", "1d"))
    cid = "CON.F.US.MES.Z26"

    # 2026-11-10 17:58:10 ET -> 17:58 1m bucket, 17:55 5m bucket, Nov 9 18:00 ET 1d bucket
    t1 = _et_ts(2026, 11, 10, 17, 58, 10)
    t2 = _et_ts(2026, 11, 10, 17, 58, 45)
    # 17:59:15 ET -> closes 17:58 1m bar; still inside 17:55 5m bar and same 1d session
    t3 = _et_ts(2026, 11, 10, 17, 59, 15)
    # 18:00:05 ET -> crosses 18:00 ET CME Globex boundary -> closes 17:59 1m, 17:55 5m, and 1d!
    t4 = _et_ts(2026, 11, 10, 18, 0, 5)

    r1 = agg.process_tick(NormalizedTick(cid, False, t1, 5950.25, 4.0), now_utc=t1)
    assert r1.accepted is True
    assert all(e.bar.is_closed is False for e in r1.events)

    r2 = agg.process_tick(NormalizedTick(cid, False, t2, 5952.00, 6.0), now_utc=t2)
    assert r2.accepted is True
    open_1m = agg.get_open_bar(cid, False, "1m")
    assert open_1m is not None
    assert open_1m.time == _et_ts(2026, 11, 10, 17, 58, 0)
    assert open_1m.open == 5950.25
    assert open_1m.high == 5952.00
    assert open_1m.low == 5950.25
    assert open_1m.close == 5952.00
    assert open_1m.volume == 10.0
    assert open_1m.is_closed is False

    r3 = agg.process_tick(NormalizedTick(cid, False, t3, 5949.50, 3.0), now_utc=t3)
    closed_r3 = [e for e in r3.events if e.bar.is_closed]
    assert len(closed_r3) == 1
    assert closed_r3[0].timeframe == "1m"
    assert closed_r3[0].bar == OHLCVBar(
        time=_et_ts(2026, 11, 10, 17, 58, 0),
        open=5950.25,
        high=5952.00,
        low=5950.25,
        close=5952.00,
        volume=10.0,
        is_closed=True,
    )

    # Crossing 18:00 ET must close 1m (17:59), 5m (17:55), and 1d (Nov 9 18:00 ET) simultaneously.
    r4 = agg.process_tick(NormalizedTick(cid, False, t4, 5955.00, 2.0), now_utc=t4)
    closed_by_tf = {e.timeframe: e.bar for e in r4.events if e.bar.is_closed}
    open_by_tf = {e.timeframe: e.bar for e in r4.events if not e.bar.is_closed}

    assert set(closed_by_tf) == {"1m", "5m", "1d"}
    assert closed_by_tf["1m"].time == _et_ts(2026, 11, 10, 17, 59, 0)
    assert closed_by_tf["5m"].time == _et_ts(2026, 11, 10, 17, 55, 0)
    assert closed_by_tf["5m"].open == 5950.25
    assert closed_by_tf["5m"].high == 5952.00
    assert closed_by_tf["5m"].low == 5949.50
    assert closed_by_tf["5m"].close == 5949.50
    assert closed_by_tf["5m"].volume == 13.0
    assert closed_by_tf["1d"].time == _et_ts(2026, 11, 9, 18, 0, 0)
    assert closed_by_tf["1d"].volume == 13.0

    new_globex_open = _et_ts(2026, 11, 10, 18, 0, 0)
    assert open_by_tf["1m"].time == new_globex_open
    assert open_by_tf["5m"].time == new_globex_open
    assert open_by_tf["1d"].time == new_globex_open
    assert all(b.is_closed is False and b.open == 5955.00 for b in open_by_tf.values())

    # Advancing time past 18:01:00 ET flushes 1m cleanly without needing another tick.
    flushed = agg.flush_closed_bars(now_utc=_et_ts(2026, 11, 10, 18, 1, 0))
    assert len(flushed) == 1
    assert flushed[0].timeframe == "1m"
    assert flushed[0].bar.time == new_globex_open
    assert flushed[0].bar.is_closed is True
    assert agg.get_open_bar(cid, False, "1m") is None
    assert agg.get_open_bar(cid, False, "5m") is not None


def test_invalid_tick_rejection_preserves_open_bar_state() -> None:
    """Contract: NaN, Inf, bad prices, negative volume, and stale ticks are rejected cleanly.

    Defect caught: Corrupt tick values mutating or poisoning an active provisional open candle.
    """
    agg = BarAggregator(timeframes=("1m", "5m"))
    cid = "CON.F.US.MNQ.Z26"
    t0 = int(datetime(2026, 11, 10, 14, 5, 10, tzinfo=UTC).timestamp())

    ok = agg.process_tick(NormalizedTick(cid, True, t0, 21000.0, 5.0), now_utc=t0)
    assert ok.accepted is True
    baseline_1m = agg.get_open_bar(cid, True, "1m")
    baseline_5m = agg.get_open_bar(cid, True, "5m")
    assert baseline_1m is not None and baseline_5m is not None

    bad_ticks = [
        NormalizedTick(cid, True, t0 + 5, math.nan, 1.0),
        NormalizedTick(cid, True, t0 + 5, math.inf, 1.0),
        NormalizedTick(cid, True, t0 + 5, -math.inf, 1.0),
        NormalizedTick(cid, True, t0 + 5, 0.0, 1.0),
        NormalizedTick(cid, True, t0 + 5, -10.0, 1.0),
        NormalizedTick(cid, True, t0 + 5, 21005.0, -1.0),
        NormalizedTick(cid, True, t0 + 5, 21005.0, math.nan),
        NormalizedTick(cid, True, t0 + 5, 21005.0, math.inf),
        NormalizedTick(cid, True, t0 + 3600, 21005.0, 1.0),  # future skew beyond 300s
        NormalizedTick("", True, t0 + 5, 21005.0, 1.0),
    ]
    for bad in bad_ticks:
        res = agg.process_tick(bad, now_utc=t0 + 10)
        assert res.accepted is False
        assert res.events == ()
        assert agg.get_open_bar(cid, True, "1m") == baseline_1m
        assert agg.get_open_bar(cid, True, "5m") == baseline_5m

    # Advance to next 1m bar, then send a late tick from the already-closed prior 1m bar.
    t_next = t0 + 60
    agg.process_tick(NormalizedTick(cid, True, t_next, 21010.0, 2.0), now_utc=t_next)
    closed_1m_time = agg.get_last_closed_time(cid, True, "1m")
    assert closed_1m_time == bucket_open_utc(t0, "1m")
    open_after = agg.get_open_bar(cid, True, "1m")

    stale_tick = NormalizedTick(cid, True, t0 + 20, 19999.0, 99.0)
    stale_res = agg.process_tick(stale_tick, now_utc=t_next + 5)
    assert stale_res.accepted is True
    assert agg.get_open_bar(cid, True, "1m") == open_after


def test_reconcile_recovery_gives_rest_authority_and_rebuilds_open_bar() -> None:
    """Contract: Recovery merges authoritative REST closed bars with newer buffered ticks cleanly.

    Defect caught: Buffered ticks duplicating/overwriting closed REST bars or losing open-bar ticks.
    """
    agg = BarAggregator(timeframes=("1m",))
    cid = "CON.F.US.MES.Z26"
    b0 = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    b1 = b0 + 60
    b2 = b0 + 120
    now_ts = b2 + 25

    hist_closed = [
        OHLCVBar(
            time=b0, open=5950.0, high=5952.0, low=5949.5, close=5951.0, volume=20.0, is_closed=True
        ),
        OHLCVBar(
            time=b1, open=5951.0, high=5954.0, low=5950.5, close=5953.5, volume=30.0, is_closed=True
        ),
    ]
    buffered = [
        # Stale tick inside historical closed b1 -> must be discarded in favor of REST bar
        NormalizedTick(cid, False, b1 + 40, 5999.0, 500.0),
        # Live ticks for active open bar b2
        NormalizedTick(cid, False, b2 + 5, 5953.75, 4.0),
        NormalizedTick(cid, False, b2 + 12, 5955.00, 6.0),
        NormalizedTick(cid, False, b2 + 20, 5954.25, 2.0),
    ]

    recon = agg.reconcile_recovery(
        contract_id=cid,
        live_mode=False,
        timeframe="1m",
        historical_bars=hist_closed,
        buffered_ticks=buffered,
        now_utc=now_ts,
    )

    assert len(recon.closed_bars) == 2
    assert all(evt.is_recovered_historical is True for evt in recon.closed_bars)
    assert [evt.bar.time for evt in recon.closed_bars] == [b0, b1]
    assert recon.closed_bars[1].bar.high == 5954.0
    assert recon.last_closed_bar_time == b1

    assert recon.open_bar is not None
    assert recon.open_bar.is_recovered_historical is False
    assert recon.open_bar.bar == OHLCVBar(
        time=b2,
        open=5953.75,
        high=5955.00,
        low=5953.75,
        close=5954.25,
        volume=12.0,
        is_closed=False,
    )
    assert agg.get_open_bar(cid, False, "1m") == recon.open_bar.bar


def test_reconcile_recovery_preserves_pre_disconnect_open_bar_ohlcv() -> None:
    """Contract: Reconnect in same open candle merges pre-disconnect OHLCV with buffered ticks.

    Defect caught: Wiping pre-disconnect open price, extrema, and volume on reconnect (P2-006).
    """
    agg = BarAggregator(timeframes=("1m", "5m"))
    cid = "CON.F.US.MES.Z26"
    b0 = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    b1 = b0 + 60
    now_ts = b1 + 35

    # Pre-disconnect ticks inside b1 (14:01) establish open=100, low=98, volume=5.
    agg.process_tick(NormalizedTick(cid, False, b1 + 5, 100.0, 3.0), now_utc=b1 + 5)
    agg.process_tick(NormalizedTick(cid, False, b1 + 10, 98.0, 2.0), now_utc=b1 + 10)

    # REST returns earlier closed bar b0; buffered tick arrives at b1 + 25 (price=110, vol=2).
    hist_closed = [
        OHLCVBar(time=b0, open=99.0, high=101.0, low=98.5, close=100.0, volume=10.0, is_closed=True)
    ]
    recon_1m = agg.reconcile_recovery(
        contract_id=cid,
        live_mode=False,
        timeframe="1m",
        historical_bars=hist_closed,
        buffered_ticks=[NormalizedTick(cid, False, b1 + 25, 110.0, 2.0)],
        now_utc=now_ts,
    )
    assert recon_1m.open_bar is not None
    assert recon_1m.open_bar.bar == OHLCVBar(
        time=b1,
        open=100.0,
        high=110.0,
        low=98.0,
        close=110.0,
        volume=7.0,
        is_closed=False,
    )

    recon_5m = agg.reconcile_from_1m_bars(
        contract_id=cid,
        live_mode=False,
        timeframe="5m",
        closed_1m_bars=[e.bar for e in recon_1m.closed_bars],
        open_1m_bar=recon_1m.open_bar.bar,
        now_utc=now_ts,
    )
    assert recon_5m.open_bar is not None
    assert recon_5m.open_bar.bar == OHLCVBar(
        time=b0,
        open=99.0,
        high=110.0,
        low=98.0,
        close=110.0,
        volume=17.0,
        is_closed=False,
    )


def test_cme_globex_1h_4h_session_close_and_dst_transition() -> None:
    """Contract: 1h and 4h buckets honor CME 17:00 ET close and US DST 18:00 ET shifts.

    Defect caught: 1h/4h bars anchored to 00:00 UTC or failing to close at 17:00 ET / DST shifts.
    """
    agg = BarAggregator(timeframes=("1h", "4h"))
    cid = "CON.F.US.MES.Z26"

    # Before US Spring Forward (EST, UTC-5): 2026-03-06 16:30 ET = 21:30 UTC
    # 1h bucket: 16:00 ET (21:00 UTC); 4h bucket: 14:00 ET (19:00 UTC), both close at 17:00 ET!
    est_tick = _et_ts(2026, 3, 6, 16, 30, 0)
    agg.process_tick(NormalizedTick(cid, False, est_tick, 5900.0, 5.0), now_utc=est_tick)
    assert agg.get_open_bar(cid, False, "1h").time == _et_ts(2026, 3, 6, 16, 0, 0)
    assert agg.get_open_bar(cid, False, "4h").time == _et_ts(2026, 3, 6, 14, 0, 0)

    # Flush at 17:00:00 ET -> closes BOTH 1h (16:00 ET) and 4h (14:00 ET) at session halt!
    flushed_est = agg.flush_closed_bars(now_utc=_et_ts(2026, 3, 6, 17, 0, 0))
    assert {e.timeframe: e.bar.time for e in flushed_est} == {
        "1h": _et_ts(2026, 3, 6, 16, 0, 0),
        "4h": _et_ts(2026, 3, 6, 14, 0, 0),
    }

    # After US Spring Forward (2026-03-08 18:00 EDT = 22:00 UTC, 1h shift from EST 23:00 UTC):
    edt_open_utc = int(datetime(2026, 3, 8, 22, 0, 0, tzinfo=UTC).timestamp())
    edt_tick = edt_open_utc + 600
    agg.process_tick(NormalizedTick(cid, False, edt_tick, 5910.0, 3.0), now_utc=edt_tick)
    assert agg.get_open_bar(cid, False, "1h").time == edt_open_utc
    assert agg.get_open_bar(cid, False, "4h").time == edt_open_utc


def test_reconcile_from_1m_bars_blocks_incomplete_cold_start_and_corrects_watermark() -> None:
    """Contract: Incomplete 1m buckets never finalize HTF bars; complete history can correct them.

    Defect caught: Partial 15m/1h bucket marked closed on cold start (SECINV-001) and skipping
    historical correction for buckets <= watermark (P1-006).
    """
    agg = BarAggregator(timeframes=("1m", "15m", "1h"))
    cid = "CON.F.US.MES.Z26"
    h0 = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())

    # 1) Cold-start partial 15m bucket (14:05..14:14 only, missing 14:00..14:04) at 14:16 UTC.
    partial_1m = [
        OHLCVBar(h0 + (m * 60), 100.0, 102.0, 99.0, 101.0, 1.0, is_closed=True)
        for m in range(5, 15)
    ]
    res_partial = agg.reconcile_from_1m_bars(
        contract_id=cid,
        live_mode=False,
        timeframe="15m",
        closed_1m_bars=partial_1m,
        now_utc=h0 + (16 * 60),
    )
    assert res_partial.closed_bars == ()
    assert agg.get_last_closed_time(cid, False, "15m") is None

    # 2) Seed 1h watermark at h0, then reconcile all 60 1m bars with corrected high=250.0.
    agg.set_last_closed_time(cid, False, "1h", h0)
    full_hour_1m = [
        OHLCVBar(
            h0 + (m * 60),
            100.0,
            250.0 if m == 10 else 105.0,
            95.0,
            102.0,
            2.0,
            is_closed=True,
        )
        for m in range(60)
    ]
    res_corrected = agg.reconcile_from_1m_bars(
        contract_id=cid,
        live_mode=False,
        timeframe="1h",
        closed_1m_bars=full_hour_1m,
        now_utc=h0 + 3600,
    )
    assert len(res_corrected.closed_bars) == 1
    assert res_corrected.closed_bars[0].bar.time == h0
    assert res_corrected.closed_bars[0].bar.high == 250.0
    assert res_corrected.closed_bars[0].bar.volume == 120.0
    assert res_corrected.closed_bars[0].is_recovered_historical is True


def test_reconcile_recovery_preserves_subsecond_tick_ordering() -> None:
    """Contract: Buffered ticks within the same integer second replay in exact sub-second order.

    Defect caught: Truncating tick timestamps to integer seconds before sorting, flipping open and
    close on out-of-order sub-second ticks (PASS1-005).
    """
    agg = BarAggregator(timeframes=("1m",))
    cid = "CON.F.US.MES.Z26"
    b0 = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    b1 = b0 + 60
    hist = [OHLCVBar(b0, 99.0, 101.0, 98.0, 100.0, 5.0, is_closed=True)]

    # Later sub-second tick (b1 + 5.9) arrives BEFORE earlier tick (b1 + 5.1).
    recon = agg.reconcile_recovery(
        contract_id=cid,
        live_mode=False,
        timeframe="1m",
        historical_bars=hist,
        buffered_ticks=[
            NormalizedTick(cid, False, b1 + 5.9, 110.0, 2.0),
            NormalizedTick(cid, False, b1 + 5.1, 100.0, 3.0),
        ],
        now_utc=b1 + 10,
    )
    assert recon.open_bar is not None
    assert recon.open_bar.bar == OHLCVBar(
        time=b1,
        open=100.0,
        high=110.0,
        low=100.0,
        close=110.0,
        volume=5.0,
        is_closed=False,
    )


def test_incomplete_provisional_htf_bar_does_not_finalize_on_next_bucket_or_watchdog() -> None:
    """Contract: Provisional HTF bars built from incomplete 1m history never finalize as closed.

    Defect caught: Partial 1h open bar (only 14:50) getting finalized as closed when tick crosses
    into 15:00 or watchdog flushes (P2-002).
    """
    cid = "CON.F.US.MES.Z26"
    h0 = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())

    # Case A: Subsequent tick crosses into 15:00 bucket -> 14:00 must NOT emit is_closed=True.
    agg_tick = BarAggregator(timeframes=("1h",))
    res_inc = agg_tick.reconcile_from_1m_bars(
        contract_id=cid,
        live_mode=False,
        timeframe="1h",
        closed_1m_bars=[OHLCVBar(h0 + 50 * 60, 100.0, 105.0, 99.0, 102.0, 11.0, is_closed=True)],
        now_utc=h0 + 51 * 60,
    )
    assert res_inc.open_bar is not None and res_inc.open_bar.bar.is_closed is False
    tick_res = agg_tick.process_tick(
        NormalizedTick(cid, False, h0 + 3600 + 5, 104.0, 2.0),
        now_utc=h0 + 3600 + 5,
    )
    assert [e for e in tick_res.events if e.bar.is_closed] == []
    assert agg_tick.get_last_closed_time(cid, False, "1h") is None
    assert agg_tick.get_open_bar(cid, False, "1h") == OHLCVBar(
        h0 + 3600, 104.0, 104.0, 104.0, 104.0, 2.0, is_closed=False
    )

    # Case B: Watchdog flush_closed_bars past 15:00 -> 14:00 discarded without closing.
    agg_wd = BarAggregator(timeframes=("1h",))
    agg_wd.reconcile_from_1m_bars(
        contract_id=cid,
        live_mode=False,
        timeframe="1h",
        closed_1m_bars=[OHLCVBar(h0 + 50 * 60, 100.0, 105.0, 99.0, 102.0, 11.0, is_closed=True)],
        now_utc=h0 + 51 * 60,
    )
    flushed = agg_wd.flush_closed_bars(now_utc=h0 + 3605)
    assert flushed == []
    assert agg_wd.get_last_closed_time(cid, False, "1h") is None
    assert agg_wd.get_open_bar(cid, False, "1h") is None
