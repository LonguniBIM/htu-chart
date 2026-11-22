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
