"""Value-first tests for Phase 5 backtest engine, ICT FVG+Sweep strategy, and R02 worker."""

from __future__ import annotations

import asyncio
import ctypes
import json
import multiprocessing
import os
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.engine.backtester import run_backtest
from app.engine.indicators import (
    detect_fvg_zones,
    detect_liquidity_sweeps,
    detect_order_blocks,
    ema,
    rsi,
    session_vwap,
)
from app.engine.instruments import INSTRUMENTS, InstrumentSpec, StorageDatasetKey
from app.engine.ohlcv import OHLCVBar
from app.engine.strategies import create_strategy, list_strategies
from app.engine.strategies.ict_fvg_sweep import IctFvgSweepStrategy
from app.engine.strategy_base import (
    BaseStrategy,
    CausalBarView,
    IndicatorSeries,
    StrategyContext,
    StrategySignal,
    StrategyStepOutput,
    StrategyZone,
)
from app.main import create_app

TEST_PASSWORD = "backtest-test-password-123"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)
NY_TZ = ZoneInfo("America/New_York")


def _bar(
    ts: int,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 100.0,
    *,
    is_closed: bool = True,
) -> OHLCVBar:
    return OHLCVBar(
        time=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        is_closed=is_closed,
    )


def _ict_long_setup_bars(start_ts: int = 1_760_000_000, step: int = 300) -> list[OHLCVBar]:
    """Construct deterministic 12-bar series with a bullish SSL sweep + bullish FVG at bar 7."""
    # Bars 0..4: range low at 100.00 (bar 2)
    # Bar 5: bullish SSL sweep (low=99.50 < 100.00, close=100.50 > 100.00)
    # Bar 6: mid expansion (high=101.25)
    # Bar 7: displacement up leaving FVG between bar 5 high (100.75) and bar 7 low (101.50)
    # Bars 8..11: follow-through upward reaching TP
    specs = [
        (101.00, 101.50, 100.50, 101.00),  # 0
        (101.00, 101.25, 100.25, 100.50),  # 1
        (100.50, 101.00, 100.00, 100.50),  # 2: prior swing low = 100.00
        (100.50, 101.25, 100.25, 101.00),  # 3
        (101.00, 101.25, 100.25, 100.75),  # 4
        (100.75, 100.75, 99.50, 100.50),   # 5: SSL sweep (low=99.50 < 100.00, close=100.50)
        (100.50, 102.00, 100.50, 101.75),  # 6: impulse candle
        (101.75, 103.00, 101.50, 102.75),  # 7: bullish FVG confirmed (101.50 > 100.75)
        (102.75, 103.50, 102.50, 103.25),  # 8: market entry at open=102.75
        (103.25, 106.00, 103.00, 105.75),  # 9
        (105.75, 111.00, 105.50, 110.50),  # 10: hits TP
        (110.50, 111.50, 110.00, 111.00),  # 11
    ]
    return [
        _bar(start_ts + idx * step, o, h, low, c, 100.0 + idx * 10.0)
        for idx, (o, h, low, c) in enumerate(specs)
    ]


# Top-level picklable worker stubs for Windows `spawn` ProcessPoolExecutor tests.
def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        handle = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return False
        try:
            wait_res = k32.WaitForSingleObject(handle, 0)
            return wait_res == 0x00000102  # WAIT_TIMEOUT -> still running
        finally:
            k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def worker_slow_success(payload: dict[str, Any]) -> dict[str, Any]:
    """Write handshake PID marker and simulate CPU-bound backtest in isolated worker."""
    marker = Path(tempfile.gettempdir()) / f"topstep_bp5_{payload['contract_id']}.pid"
    marker.write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(0.6)
    return {
        "strategy_id": payload["strategy_id"],
        "strategy_version": "1.0.0",
        "contract_id": payload["contract_id"],
        "root_symbol": payload["root_symbol"],
        "timeframe": payload["timeframe"],
        "live_mode": payload["live_mode"],
        "params": payload["params"],
        "instrument": {
            "root_symbol": payload["root_symbol"],
            "tick_size": 0.25,
            "tick_value": 1.25,
        },
        "bar_count": len(payload["bars"]),
        "start_time": payload["bars"][0][0] if payload["bars"] else None,
        "end_time": payload["bars"][-1][0] if payload["bars"] else None,
        "excluded_provisional_count": 0,
        "trades": [],
        "markers": [],
        "trade_lines": [],
        "zones": [],
        "indicators": [],
        "summary": {
            "net_profit_dollars": 0.0,
            "gross_profit_dollars": 0.0,
            "gross_loss_dollars": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_dollars": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "breakeven_trades": 0,
            "ambiguous_fill_count": 0,
            "equity_curve": [],
        },
        "worker_pid": os.getpid(),
    }


def worker_hang_or_fast(payload: dict[str, Any]) -> dict[str, Any]:
    """Sleep long when strategy param max_wait_bars == 49, else finish immediately."""
    if int(payload["params"].get("max_wait_bars", 5)) == 49:
        marker = Path(tempfile.gettempdir()) / f"topstep_hang_{payload['contract_id']}.pid"
        marker.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(15.0)
    return {
        "strategy_id": payload["strategy_id"],
        "strategy_version": "1.0.0",
        "contract_id": payload["contract_id"],
        "root_symbol": payload["root_symbol"],
        "timeframe": payload["timeframe"],
        "live_mode": payload["live_mode"],
        "params": payload["params"],
        "instrument": {
            "root_symbol": payload["root_symbol"],
            "tick_size": 0.25,
            "tick_value": 1.25,
        },
        "bar_count": len(payload["bars"]),
        "start_time": None,
        "end_time": None,
        "excluded_provisional_count": 0,
        "trades": [],
        "markers": [],
        "trade_lines": [],
        "zones": [],
        "indicators": [],
        "summary": {
            "net_profit_dollars": 0.0,
            "gross_profit_dollars": 0.0,
            "gross_loss_dollars": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_dollars": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "breakeven_trades": 0,
            "ambiguous_fill_count": 0,
            "equity_curve": [],
        },
        "worker_pid": os.getpid(),
    }


def _sleep_child_target() -> None:
    time.sleep(30.0)


def worker_spawns_child_and_sleeps(payload: dict[str, Any]) -> dict[str, Any]:
    """Spawn a real child Python process, record both PIDs, and sleep for tree-kill tests."""
    mp_ctx = multiprocessing.get_context("spawn")
    child = mp_ctx.Process(target=_sleep_child_target)
    child.start()
    marker = Path(tempfile.gettempdir()) / f"topstep_tree_{payload['contract_id']}.json"
    marker.write_text(
        json.dumps({"worker_pid": os.getpid(), "child_pid": child.pid}),
        encoding="utf-8",
    )
    time.sleep(30.0)
    return {"child_pid": child.pid}


def worker_oversized_payload(_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a huge payload to test backtest_max_result_bytes enforcement."""
    return {
        "strategy_id": "ict_fvg_sweep",
        "summary": {"total_trades": 0},
        "blob": "X" * 10_000,
    }


def worker_crash_once_or_ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Hard-exit worker process when max_wait_bars == 48 to test BrokenProcessPool respawn."""
    if int(payload["params"].get("max_wait_bars", 5)) == 48:
        os._exit(1)
    return worker_hang_or_fast(payload)


# Test-only causal strategy stubs for exact F08 fill/SL-TP/intrabar contract tests.
class _LookaheadProbingStrategy(BaseStrategy):
    strategy_id = "_test_lookahead_probe"
    strategy_version = "1.0.0"
    name = "Lookahead Probe"
    description = "Attempts to index bar_index + 1 to verify CausalBarView barrier."

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return dict(params or {})

    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        _ = ctx.bars[ctx.bar_index + 1]
        return StrategyStepOutput()

    def compute_indicators(
        self, bars: Sequence[OHLCVBar], instrument: InstrumentSpec
    ) -> list[IndicatorSeries]:
        return []


class _SingleSignalStrategy(BaseStrategy):
    strategy_id = "_test_single_signal"
    strategy_version = "1.0.0"
    name = "Single Signal"
    description = "Emits one deterministic signal on a target bar index."

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {
            "signal_index": 0,
            "direction": "LONG",
            "order_type": "market",
            "limit_price": None,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "quantity": 1,
            "max_wait_bars": 5,
        }

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        merged = cls.default_params()
        if params:
            merged.update(params)
        return merged

    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        if ctx.bar_index != int(self.params["signal_index"]):
            return StrategyStepOutput()
        return StrategyStepOutput(
            signal=StrategySignal(
                direction=self.params["direction"],
                signal_bar_index=ctx.bar_index,
                signal_time=ctx.bars[ctx.bar_index].time,
                order_type=self.params["order_type"],
                limit_price=self.params["limit_price"],
                stop_loss=float(self.params["stop_loss"]),
                take_profit=float(self.params["take_profit"]),
                quantity=int(self.params["quantity"]),
                max_wait_bars=int(self.params["max_wait_bars"]),
                reason="Test signal",
            )
        )

    def compute_indicators(
        self, bars: Sequence[OHLCVBar], instrument: InstrumentSpec
    ) -> list[IndicatorSeries]:
        return []


class _MultiSignalScriptStrategy(BaseStrategy):
    strategy_id = "_test_multi_signal"
    strategy_version = "1.0.0"
    name = "Multi Signal"
    description = "Emits scripted signals at specified bar indices."

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"scripts": {}}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"scripts": dict((params or {}).get("scripts", {}))}

    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        spec = self.params["scripts"].get(ctx.bar_index)
        if spec is None or ctx.has_open_position or ctx.has_pending_order:
            return StrategyStepOutput()
        return StrategyStepOutput(
            signal=StrategySignal(
                direction=spec["direction"],
                signal_bar_index=ctx.bar_index,
                signal_time=ctx.bars[ctx.bar_index].time,
                order_type="market",
                stop_loss=float(spec["stop_loss"]),
                take_profit=float(spec["take_profit"]),
                quantity=int(spec.get("quantity", 1)),
                reason=spec.get("reason", "Scripted"),
            )
        )

    def compute_indicators(
        self, bars: Sequence[OHLCVBar], instrument: InstrumentSpec
    ) -> list[IndicatorSeries]:
        return []


class _FutureZoneLeakStrategy(BaseStrategy):
    strategy_id = "_test_zone_leak"
    strategy_version = "1.0.0"
    name = "Zone Leak"
    description = "Emits a zone with detected_at_time in the future."

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {}

    def on_bar(self, ctx: StrategyContext) -> StrategyStepOutput:
        bar = ctx.bars[ctx.bar_index]
        return StrategyStepOutput(
            zones=(
                StrategyZone(
                    kind="fvg",
                    direction="bullish",
                    start_time=bar.time,
                    end_time=bar.time,
                    detected_at_time=bar.time + 60,
                    top=bar.high,
                    bottom=bar.low,
                ),
            )
        )

    def compute_indicators(
        self, bars: Sequence[OHLCVBar], instrument: InstrumentSpec
    ) -> list[IndicatorSeries]:
        return []


@asynccontextmanager
async def _async_client(
    tmp_path: Path,
    *,
    backtest_enabled: bool = True,
    backtest_max_queue_depth: int = 3,
    backtest_max_bars: int = 50000,
    backtest_max_date_range_days: int = 365,
    backtest_timeout_sec: float = 30.0,
    backtest_max_result_bytes: int = 5_000_000,
    backtest_max_jobs_retained: int = 64,
    worker_fn: Any = None,
):
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
        backtest_enabled=backtest_enabled,
        backtest_max_queue_depth=backtest_max_queue_depth,
        backtest_max_bars=backtest_max_bars,
        backtest_max_date_range_days=backtest_max_date_range_days,
        backtest_timeout_sec=backtest_timeout_sec,
        backtest_max_result_bytes=backtest_max_result_bytes,
        backtest_max_jobs_retained=backtest_max_jobs_retained,
    )
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={}))
    )
    tg_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
    )
    app = create_app(
        settings=settings,
        http_client=upstream,
        telegram_http_client=tg_client,
        backtest_worker_fn=worker_fn,
    )
    try:
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
            async with httpx.AsyncClient(
                transport=asgi,
                base_url="http://127.0.0.1:8000",
            ) as client:
                yield client, app
    finally:
        await tg_client.aclose()
        await upstream.aclose()


async def _login(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    return str(resp.json()["csrf_token"])


def test_causal_bar_view_blocks_future_access_and_clamps_slices() -> None:
    """Contract: CausalBarView physically blocks indexing bars >= visible_count and clamps slices.

    Defect caught: Strategy accessing bars[i + 1] or slicing future candles during on_bar().
    """
    bars = _ict_long_setup_bars()
    view = CausalBarView(bars, 4)

    assert len(view) == 4
    assert view[0] == bars[0]
    assert view[3] == bars[3]
    assert view[-1] == bars[3]
    assert view[-4] == bars[0]
    assert list(view[1:100]) == bars[1:4]
    assert list(view[4:10]) == []
    assert list(view) == bars[:4]

    with pytest.raises(IndexError):
        _ = view[4]
    with pytest.raises(IndexError):
        _ = view[-5]

    with pytest.raises(IndexError):
        run_backtest(
            bars,
            contract_id="CON.F.US.MES.Z26",
            timeframe="5m",
            strategy=_LookaheadProbingStrategy(),
        )


def test_ict_strategy_prefix_invariance_and_causality_guard() -> None:
    """Contract: Mutating bars after index k never alters zones, signals, or indicators <= k.

    Defect caught: Future data leakage (F08) in IctFvgSweepStrategy, zones, or indicators.
    """
    base_bars = _ict_long_setup_bars()
    k = 8
    mutated_bars = list(base_bars[: k + 1]) + [
        _bar(base_bars[k].time + 300 * j, 50.0, 200.0, 10.0, 150.0, 9999.0)
        for j in range(1, 6)
    ]

    res_base = run_backtest(
        base_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy_id="ict_fvg_sweep",
        params={"swing_lookback": 5, "ema_period": 3},
    )
    res_mutated = run_backtest(
        mutated_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy_id="ict_fvg_sweep",
        params={"swing_lookback": 5, "ema_period": 3},
    )

    cutoff_time = base_bars[k].time
    zones_base = [z.to_dict() for z in res_base.zones if z.detected_at_time <= cutoff_time]
    zones_mut = [z.to_dict() for z in res_mutated.zones if z.detected_at_time <= cutoff_time]
    assert len(zones_base) >= 2
    assert zones_base == zones_mut
    assert all(z["detected_at_time"] <= cutoff_time for z in zones_base)

    # Full entry attributes for trades initiated in prefix <= k must match identically.
    def _entry_tuple(tr: Any) -> tuple[Any, ...]:
        return (
            tr.direction,
            tr.signal_time,
            tr.entry_time,
            tr.entry_price,
            tr.stop_loss,
            tr.take_profit,
            tr.quantity,
            tr.reason,
        )

    prefix_trades_base = [
        _entry_tuple(t) for t in res_base.trades if t.signal_time <= cutoff_time
    ]
    prefix_trades_mut = [
        _entry_tuple(t) for t in res_mutated.trades if t.signal_time <= cutoff_time
    ]
    assert len(prefix_trades_base) == 1
    assert prefix_trades_base == prefix_trades_mut
    assert res_base.trades[0].signal_time == base_bars[7].time
    assert res_base.trades[0].entry_time == base_bars[8].time
    assert res_base.trades[0].entry_price == base_bars[8].open

    for s_base, s_mut in zip(res_base.indicators, res_mutated.indicators, strict=True):
        pts_base = [p.to_dict() for p in s_base.points if p.time <= cutoff_time]
        pts_mut = [p.to_dict() for p in s_mut.points if p.time <= cutoff_time]
        assert pts_base == pts_mut

    with pytest.raises(ValueError, match="Causality violation"):
        run_backtest(
            base_bars[:3],
            contract_id="CON.F.US.MES.Z26",
            timeframe="5m",
            strategy=_FutureZoneLeakStrategy(),
        )


def test_market_signal_fills_at_next_bar_open_never_signal_bar_close() -> None:
    """Contract: Signal at bar[i] close fills strictly at bar[i+1].open and bar[i+1].time.

    Defect caught: Look-ahead fill at bar[i].close instead of bar[i+1].open (F08).
    """
    t0 = 1_760_000_000
    bars = [
        _bar(t0, 100.0, 101.0, 99.5, 100.5),         # i=0: signal emitted at close=100.5
        _bar(t0 + 300, 101.25, 101.5, 100.75, 101.0), # i=1: must fill at open=101.25
        _bar(t0 + 600, 101.0, 102.5, 100.75, 102.25), # i=2: hits TP=102.0
    ]
    strat = _SingleSignalStrategy(
        {
            "signal_index": 0,
            "direction": "LONG",
            "order_type": "market",
            "stop_loss": 99.0,
            "take_profit": 102.0,
        }
    )
    res = run_backtest(
        bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy=strat,
    )
    assert len(res.trades) == 1
    trade = res.trades[0]
    assert trade.signal_time == bars[0].time
    assert trade.entry_time == bars[1].time
    assert trade.entry_price == bars[1].open
    assert trade.entry_price != bars[0].close
    assert trade.exit_time == bars[2].time
    assert trade.exit_price == 102.0
    assert trade.exit_reason == "take_profit"


def test_both_hit_sl_tp_resolves_conservatively_sl_first_and_intrabar_limit_rule() -> None:
    """Contract: Both-hit resolves SL-first (ambiguous_fill=True); intrabar limit skips same-bar TP.

    Defect caught: Optimistic TP-first tie-breaking or omitting ambiguous_fill counters (F08).
    """
    t0 = 1_760_000_000
    # 1) LONG both-hit on bar 2 + SHORT both-hit on bar 4 -> both resolve SL-first.
    bars = [
        _bar(t0, 100.0, 101.0, 99.5, 100.5),         # 0: emit LONG
        _bar(t0 + 300, 100.5, 101.0, 100.0, 100.75),  # 1: fill LONG @ 100.5
        _bar(t0 + 600, 100.75, 104.0, 98.0, 101.0),   # 2: hits BOTH SL=99.0 & TP=103.0; emit SHORT
        _bar(t0 + 900, 101.0, 101.5, 100.5, 101.0),   # 3: fill SHORT @ 101.0
        _bar(t0 + 1200, 101.0, 104.0, 97.0, 100.0),  # 4: hits BOTH SL=103.0 & TP=98.0
    ]
    strat = _MultiSignalScriptStrategy(
        {
            "scripts": {
                0: {"direction": "LONG", "stop_loss": 99.0, "take_profit": 103.0},
                2: {"direction": "SHORT", "stop_loss": 103.0, "take_profit": 98.0},
            }
        }
    )
    res = run_backtest(
        bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy=strat,
    )
    assert len(res.trades) == 2
    assert res.trades[0].exit_reason == "stop_loss"
    assert res.trades[0].exit_price == 99.0
    assert res.trades[0].ambiguous_fill is True
    assert res.trades[1].exit_reason == "stop_loss"
    assert res.trades[1].exit_price == 103.0
    assert res.trades[1].ambiguous_fill is True
    assert res.summary["ambiguous_fill_count"] == 2

    # 2) Intrabar limit fill: bar[1] dips to 100.0 then rallies past TP=102.0 -> no bar[1] TP exit.
    limit_bars = [
        _bar(t0, 101.0, 101.5, 100.5, 101.0),         # 0: emit LONG limit @ 100.0
        _bar(t0 + 300, 101.0, 102.5, 99.75, 101.5),   # 1: intrabar fill @ 100.0; TP touched
        _bar(t0 + 600, 101.5, 102.25, 101.0, 102.0),  # 2: TP=102.0 validly hit on bar[2]
    ]
    limit_strat = _SingleSignalStrategy(
        {
            "signal_index": 0,
            "direction": "LONG",
            "order_type": "limit",
            "limit_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
        }
    )
    limit_res = run_backtest(
        limit_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy=limit_strat,
    )
    assert len(limit_res.trades) == 1
    assert limit_res.trades[0].entry_time == limit_bars[1].time
    assert limit_res.trades[0].entry_price == 100.0
    assert limit_res.trades[0].exit_time == limit_bars[2].time
    assert limit_res.trades[0].exit_reason == "take_profit"
    assert limit_res.trades[0].ambiguous_fill is False


def test_cme_decimal_pnl_across_all_instruments_and_summary_metrics() -> None:
    """Contract: Exact Decimal tick/dollar P&L across MES, MNQ, MYM, MGC and summary metrics.

    Defect caught: Binary float drift (MGC 0.10) or wrong profit_factor / drawdown / marker order.
    """
    t0 = 1_760_000_000

    # Hand-calculated multi-trade test on MGC (tick_size=0.10, tick_value=$1.00):
    # Trade 1 (qty=2): entry=1850.0, exit=1850.3 (+3 ticks * $1.00 * 2 = +$6.00)
    # Trade 2 (qty=3): entry=1850.4, exit=1850.0 (-4 ticks * $1.00 * 3 = -$12.00) -> DD = $12.00
    # Trade 3 (qty=1): entry=1850.2, exit=1851.2 (+10 ticks * $1.00 * 1 = +$10.00)
    # Net = +6 - 12 + 10 = +4.00; Gross profit = 16.00; Gross loss = 12.00; PF = 16/12 = 4/3
    mgc_bars = [
        _bar(t0 + 0 * 60, 1850.0, 1850.2, 1849.8, 1850.0),
        _bar(t0 + 1 * 60, 1850.0, 1850.4, 1849.9, 1850.3),  # T1 entry=1850.0, TP=1850.3
        _bar(t0 + 2 * 60, 1850.3, 1850.5, 1850.2, 1850.4),
        _bar(t0 + 3 * 60, 1850.4, 1850.5, 1849.9, 1850.0),  # T2 entry=1850.4, SL=1850.0
        _bar(t0 + 4 * 60, 1850.0, 1850.3, 1849.9, 1850.2),
        _bar(t0 + 5 * 60, 1850.2, 1851.3, 1850.1, 1851.2),  # T3 entry=1850.2, TP=1851.2
    ]
    mgc_strat = _MultiSignalScriptStrategy(
        {
            "scripts": {
                0: {"direction": "LONG", "stop_loss": 1849.5, "take_profit": 1850.3, "quantity": 2},
                2: {"direction": "LONG", "stop_loss": 1850.0, "take_profit": 1851.0, "quantity": 3},
                4: {"direction": "LONG", "stop_loss": 1849.5, "take_profit": 1851.2, "quantity": 1},
            }
        }
    )
    mgc_res = run_backtest(
        mgc_bars,
        contract_id="CON.F.US.MGC.Z26",
        timeframe="1m",
        strategy=mgc_strat,
    )
    assert len(mgc_res.trades) == 3
    assert mgc_res.trades[0].pnl_ticks == 3.0
    assert mgc_res.trades[0].pnl_dollars == 6.0
    assert mgc_res.trades[1].pnl_ticks == -4.0
    assert mgc_res.trades[1].pnl_dollars == -12.0
    assert mgc_res.trades[2].pnl_ticks == 10.0
    assert mgc_res.trades[2].pnl_dollars == 10.0

    summary = mgc_res.summary
    assert summary["net_profit_dollars"] == 4.0
    assert summary["gross_profit_dollars"] == 16.0
    assert summary["gross_loss_dollars"] == 12.0
    assert summary["max_drawdown_dollars"] == 12.0
    assert summary["profit_factor"] == pytest.approx(16.0 / 12.0)
    assert summary["total_trades"] == 3
    assert summary["winning_trades"] == 2
    assert summary["losing_trades"] == 1
    assert summary["win_rate_pct"] == pytest.approx(200.0 / 3.0)

    # Markers strictly ascending by time (entry before exit on same bar).
    marker_times = [m["time"] for m in mgc_res.markers]
    assert marker_times == sorted(marker_times)
    assert len(mgc_res.trade_lines) == 3
    assert mgc_res.trade_lines[0]["color"] == "#22c55e"
    assert mgc_res.trade_lines[1]["color"] == "#ef4444"

    # Verify MES, MNQ, MYM exact tick math and profit_factor=None when zero losses.
    expected_cases = [
        ("CON.F.US.MES.Z26", 5000.0, 5001.0, 2, 4.0, 10.0),   # 4 ticks * $1.25 * 2 = $10.00
        ("CON.F.US.MNQ.Z26", 20000.0, 20001.0, 3, 4.0, 6.0),  # 4 ticks * $0.50 * 3 = $6.00
        ("CON.F.US.MYM.Z26", 40000.0, 40005.0, 2, 5.0, 5.0),  # 5 ticks * $0.50 * 2 = $5.00
    ]
    for cid, entry_px, tp_px, qty, exp_ticks, exp_dollars in expected_cases:
        sym_bars = [
            _bar(t0, entry_px, entry_px + 1.0, entry_px - 1.0, entry_px),
            _bar(t0 + 60, entry_px, tp_px + 1.0, entry_px - 0.5, tp_px),
        ]
        sym_res = run_backtest(
            sym_bars,
            contract_id=cid,
            timeframe="1m",
            strategy=_SingleSignalStrategy(
                {
                    "signal_index": 0,
                    "direction": "LONG",
                    "order_type": "market",
                    "stop_loss": entry_px - 5.0,
                    "take_profit": tp_px,
                    "quantity": qty,
                }
            ),
        )
        assert sym_res.trades[0].pnl_ticks == exp_ticks
        assert sym_res.trades[0].pnl_dollars == exp_dollars
        assert sym_res.summary["profit_factor"] is None


def test_provisional_bars_excluded_and_invalid_timestamps_rejected() -> None:
    """Contract: Provisional bars (is_closed=False) excluded by default; non-monotonic times fail.

    Defect caught: Open candle leaking into closed-bar backtest or unsorted bars breaking causality.
    """
    t0 = 1_760_000_000
    bars = [
        _bar(t0, 100.0, 101.0, 99.0, 100.5, is_closed=True),
        _bar(t0 + 60, 100.5, 102.0, 100.0, 101.5, is_closed=True),
        _bar(t0 + 120, 101.5, 105.0, 95.0, 104.0, is_closed=False),
    ]
    res = run_backtest(
        bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "LONG",
                "order_type": "market",
                "stop_loss": 98.0,
                "take_profit": 103.0,
            }
        ),
    )
    assert res.bar_count == 2
    assert res.excluded_provisional_count == 1
    # Exits at bar[1].close via end_of_data because bar[2] provisional was excluded.
    assert res.trades[0].exit_time == bars[1].time
    assert res.trades[0].exit_reason == "end_of_data"

    with pytest.raises(ValueError, match="strictly increasing"):
        run_backtest(
            [_bar(t0 + 60, 100.0, 101.0, 99.0, 100.5), _bar(t0 + 60, 100.0, 101.0, 99.0, 100.5)],
            contract_id="CON.F.US.MES.Z26",
            timeframe="1m",
        )


def test_session_vwap_resets_at_1800_et_not_0000_utc() -> None:
    """Contract: session_vwap resets strictly at 18:00 America/New_York, never at 00:00 UTC.

    Defect caught: Naive UTC midnight VWAP reset splitting overnight CME Globex sessions (R05).
    """
    # Winter EST (UTC-5): 18:00 ET == 23:00 UTC.
    # Jan 14 23:30 UTC (18:30 ET) and Jan 15 00:15 UTC (19:15 ET) belong to the SAME Globex session.
    # Jan 15 23:00 UTC (18:00 ET) starts the NEXT Globex session.
    t_same_1 = int(datetime(2026, 1, 14, 23, 30, tzinfo=UTC).timestamp())
    t_same_2 = int(datetime(2026, 1, 15, 0, 15, tzinfo=UTC).timestamp())
    t_next_sess = int(datetime(2026, 1, 15, 23, 0, tzinfo=UTC).timestamp())

    bars = [
        _bar(t_same_1, 100.0, 100.0, 100.0, 100.0, v=10.0),
        _bar(t_same_2, 110.0, 110.0, 110.0, 110.0, v=30.0),
        _bar(t_next_sess, 200.0, 200.0, 200.0, 200.0, v=0.0),
    ]
    vwap_pts = session_vwap(bars)
    assert len(vwap_pts) == 3
    assert vwap_pts[0] == (t_same_1, 100.0)
    # Cumulative across 00:00 UTC: (100*10 + 110*30) / 40 = 4300 / 40 = 107.5 (NOT 110.0).
    assert vwap_pts[1] == (t_same_2, 107.5)
    # Reset at 18:00 ET even with zero volume -> falls back to finite typical_price = 200.0.
    assert vwap_pts[2] == (t_next_sess, 200.0)


def test_indicators_and_strategy_param_validation() -> None:
    """Contract: EMA, RSI, FVG, OB, Sweep match hand-calculated values; invalid params fail.

    Defect caught: Off-by-one indicator windows or accepting invalid/bool/NaN strategy params.
    """
    t0 = 1_760_000_000
    closes = [10.0, 11.0, 12.0, 13.0, 14.0]
    bars = [_bar(t0 + i * 60, c, c, c, c) for i, c in enumerate(closes)]

    ema_pts = ema(bars, 3)
    # SMA(10,11,12)=11.0; k=0.5 -> next=13*0.5+11*0.5=12.0 -> next=14*0.5+12*0.5=13.0
    assert ema_pts == [(t0 + 120, 11.0), (t0 + 180, 12.0), (t0 + 240, 13.0)]

    rsi_pts = rsi(bars, 3)
    assert all(val == 100.0 for _, val in rsi_pts)

    # Alternating up/down Wilder-smoothing RSI check against hand-calculated values:
    # closes = [100, 102, 101, 104, 102, 105], period=3
    # bar[3]: avg_gain=5/3, avg_loss=1/3 -> RS=5 -> 100 - 100/6
    # bar[4]: avg_gain=10/9, avg_loss=8/9 -> RS=1.25 -> 100 - 100/2.25
    # bar[5]: avg_gain=47/27, avg_loss=16/27 -> RS=47/16 -> 100 * 47 / 63
    alt_closes = [100.0, 102.0, 101.0, 104.0, 102.0, 105.0]
    alt_bars = [_bar(t0 + i * 60, c, c, c, c) for i, c in enumerate(alt_closes)]
    alt_rsi = rsi(alt_bars, 3)
    assert [ts for ts, _ in alt_rsi] == [t0 + 180, t0 + 240, t0 + 300]
    assert alt_rsi[0][1] == pytest.approx(100.0 - (100.0 / 6.0))
    assert alt_rsi[1][1] == pytest.approx(100.0 - (100.0 / 2.25))
    assert alt_rsi[2][1] == pytest.approx((47.0 / 63.0) * 100.0)

    flat_bars = [_bar(t0 + i * 60, 100.0, 100.0, 100.0, 100.0) for i in range(5)]
    assert rsi(flat_bars, 3)[-1][1] == 50.0

    # FVG, OB, and Sweep on setup bars with exact independent value checks
    setup_bars = _ict_long_setup_bars()
    fvgs = detect_fvg_zones(setup_bars, min_gap_ticks=1.0, tick_size=0.25)
    assert len(fvgs) == 4
    assert fvgs[0].direction == "bullish"
    assert fvgs[0].bottom == 100.75
    assert fvgs[0].top == 101.50
    assert fvgs[0].midpoint == 101.125
    assert fvgs[0].start_time == setup_bars[5].time
    assert fvgs[0].end_time == setup_bars[7].time
    assert fvgs[0].detected_at_time == setup_bars[7].time

    sweeps = detect_liquidity_sweeps(setup_bars, swing_lookback=5)
    assert len(sweeps) == 1
    assert sweeps[0].direction == "bullish"
    assert sweeps[0].level_price == 100.00
    assert sweeps[0].level_time == setup_bars[2].time
    assert sweeps[0].sweep_extreme == 99.50
    assert sweeps[0].detected_at_time == setup_bars[5].time

    ob_bars = [
        _bar(t0 + 0 * 60, 101.0, 101.5, 100.5, 101.0),
        _bar(t0 + 1 * 60, 101.0, 101.5, 100.5, 100.75),  # bearish candle
        _bar(t0 + 2 * 60, 100.75, 101.25, 100.25, 100.5), # last bearish candle
        _bar(t0 + 3 * 60, 100.5, 103.0, 100.5, 102.75),   # displacement > prior 3 high (101.5)
    ]
    obs = detect_order_blocks(ob_bars, lookback=3)
    assert len(obs) == 1
    assert obs[0].direction == "bullish"
    assert obs[0].top == 101.25
    assert obs[0].bottom == 100.25
    assert obs[0].start_time == ob_bars[2].time
    assert obs[0].end_time == ob_bars[3].time
    assert obs[0].detected_at_time == ob_bars[3].time

    # Static registry & parameter validation
    reg_list = list_strategies()
    assert [s["strategy_id"] for s in reg_list] == ["ict_fvg_sweep"]
    strat = create_strategy("ict_fvg_sweep", {"entry_mode": "fvg_ce", "quantity": 2})
    assert isinstance(strat, IctFvgSweepStrategy)

    for bad_params in (
        {"unknown_key": 1},
        {"quantity": True},
        {"quantity": 0},
        {"risk_reward": float("nan")},
        {"risk_reward": 10**400},
        {"risk_reward": 0.1},
        {"entry_mode": "invalid"},
        {"include_vwap": "true"},
    ):
        with pytest.raises(ValueError):
            IctFvgSweepStrategy.validate_params(bad_params)

    with pytest.raises(ValueError):
        create_strategy("non_existent_strategy")


async def test_overflow_param_returns_400_without_queuing(tmp_path: Path) -> None:
    """Contract: Huge integer parameter (10**400) returns HTTP 400 and never queues a job.

    Defect caught: OverflowError on float(10**400) leaking as HTTP 500 on POST /api/backtest.
    """
    async with _async_client(tmp_path) as (client, _app):
        csrf = await _login(client)
        resp = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
                "params": {"risk_reward": 10**400},
            },
        )
        assert resp.status_code == 400
        assert (await client.get("/api/backtest")).json()["count"] == 0


async def test_backtest_routes_require_session_and_csrf(tmp_path: Path) -> None:
    """Contract: All /api/backtest routes enforce require_session (401) and require_csrf (403).

    Defect caught: Unauthenticated callers or CSRF-less mutations hitting backtest endpoints (F01).
    """
    async with _async_client(tmp_path) as (client, _app):
        assert (await client.get("/api/backtest/strategies")).status_code == 401
        assert (await client.get("/api/backtest")).status_code == 401
        assert (await client.get("/api/backtest/nonexistent")).status_code == 401
        assert (
            await client.post(
                "/api/backtest",
                json={
                    "contract_id": "CON.F.US.MES.Z26",
                    "timeframe": "5m",
                    "start_time": 1_760_000_000,
                    "end_time": 1_760_003_600,
                },
            )
        ).status_code == 401
        assert (await client.delete("/api/backtest/nonexistent")).status_code == 401

        csrf = await _login(client)
        assert (await client.get("/api/backtest/strategies")).status_code == 200
        assert (await client.get("/api/backtest")).status_code == 200

        post_no_csrf = await client.post(
            "/api/backtest",
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
            },
        )
        assert post_no_csrf.status_code == 403

        del_bad_csrf = await client.delete(
            "/api/backtest/nonexistent",
            headers={"X-CSRF-Token": "wrong-csrf"},
        )
        assert del_bad_csrf.status_code == 403

        del_missing = await client.delete(
            "/api/backtest/nonexistent",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_missing.status_code == 404


async def test_worker_subprocess_isolation_keeps_health_fast(tmp_path: Path) -> None:
    """Contract: Worker handshake proves /api/health stays fast while subprocess executes.

    Defect caught: CPU-bound backtest starving FastAPI event loop / SignalR heartbeats (R02).
    """
    cid = "CON.F.US.MES.ISO"
    marker = Path(tempfile.gettempdir()) / f"topstep_bp5_{cid}.pid"
    marker.unlink(missing_ok=True)
    try:
        async with _async_client(tmp_path, worker_fn=worker_slow_success) as (client, app):
            csrf = await _login(client)
            t0 = 1_760_000_000
            key = StorageDatasetKey("topstepx", False, cid, "5m")
            await app.state.parquet_store.upsert_bars(
                key, _ict_long_setup_bars(t0, 300), root_symbol="MES"
            )

            submit_resp = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": cid,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            assert submit_resp.status_code == 202
            job_id = submit_resp.json()["job_id"]

            # Wait until worker subprocess writes PID handshake marker (genuinely executing).
            for _ in range(100):
                if marker.is_file():
                    break
                await asyncio.sleep(0.02)
            assert marker.is_file()
            worker_pid = int(marker.read_text(encoding="utf-8").strip())
            assert worker_pid != os.getpid()

            t_start = time.monotonic()
            health_resp = await client.get("/api/health")
            elapsed = time.monotonic() - t_start
            assert health_resp.status_code == 200
            assert elapsed < 0.5

            detail: dict[str, Any] = {}
            for _ in range(100):
                detail = (await client.get(f"/api/backtest/{job_id}")).json()
                if detail["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)

            assert detail["status"] == "COMPLETED"
            assert detail["result"]["worker_pid"] == worker_pid
    finally:
        marker.unlink(missing_ok=True)


async def test_timeout_and_cancel_kill_worker_and_respawn_cleanly(tmp_path: Path) -> None:
    """Contract: Timeout and DELETE cancel kill worker process tree, respawn pool, enforce 409.

    Defect caught: Stuck/orphaned worker blocking queued jobs or overwriting TIMED_OUT/CANCELLED.
    """
    async with _async_client(
        tmp_path,
        backtest_timeout_sec=2.0,
        worker_fn=worker_hang_or_fast,
    ) as (client, _app):
        csrf = await _login(client)
        t0 = 1_760_000_000

        # 1) Timeout kills worker and respawns cleanly for next job.
        timeout_resp = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 49},
            },
        )
        assert timeout_resp.status_code == 202
        timeout_id = timeout_resp.json()["job_id"]

        timed_out_detail: dict[str, Any] = {}
        for _ in range(120):
            timed_out_detail = (await client.get(f"/api/backtest/{timeout_id}")).json()
            if timed_out_detail["status"] == "TIMED_OUT":
                break
            await asyncio.sleep(0.05)
        assert timed_out_detail["status"] == "TIMED_OUT"

        # 409 Conflict when trying to cancel already TIMED_OUT job.
        del_timed_out = await client.delete(
            f"/api/backtest/{timeout_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_timed_out.status_code == 409

        # Subsequent normal job succeeds on respawned pool.
        ok_after_timeout = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 5},
            },
        )
        assert ok_after_timeout.status_code == 202
        ok_id = ok_after_timeout.json()["job_id"]
        cur: dict[str, Any] = {}
        for _ in range(120):
            cur = (await client.get(f"/api/backtest/{ok_id}")).json()
            if cur["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert cur["status"] == "COMPLETED"
        assert (
            await client.delete(f"/api/backtest/{ok_id}", headers={"X-CSRF-Token": csrf})
        ).status_code == 409

    # 2) Cancel RUNNING job + cancel QUEUED job + verify subsequent job runs cleanly.
    async with _async_client(
        tmp_path / "cancel",
        backtest_timeout_sec=30.0,
        worker_fn=worker_hang_or_fast,
    ) as (client, _app):
        csrf = await _login(client)
        t0 = 1_760_000_000

        hang_resp = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 49},
            },
        )
        hang_id = hang_resp.json()["job_id"]
        for _ in range(80):
            if (await client.get(f"/api/backtest/{hang_id}")).json()["status"] == "RUNNING":
                break
            await asyncio.sleep(0.02)

        queued_resp = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 5},
            },
        )
        queued_id = queued_resp.json()["job_id"]
        assert (await client.get(f"/api/backtest/{queued_id}")).json()["status"] == "QUEUED"

        # Cancel QUEUED job first.
        del_q = await client.delete(
            f"/api/backtest/{queued_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_q.status_code == 200
        assert del_q.json()["status"] == "CANCELLED"

        # Cancel RUNNING job -> kills worker & respawns.
        del_r = await client.delete(
            f"/api/backtest/{hang_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_r.status_code == 200
        assert del_r.json()["status"] == "CANCELLED"

        # Idempotent DELETE on CANCELLED returns 200.
        del_again = await client.delete(
            f"/api/backtest/{hang_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert del_again.status_code == 200
        assert del_again.json()["status"] == "CANCELLED"

        # New job runs on respawned pool.
        after_cancel = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 5},
            },
        )
        assert after_cancel.status_code == 202
        after_id = after_cancel.json()["job_id"]
        for _ in range(80):
            st = (await client.get(f"/api/backtest/{after_id}")).json()
            if st["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert st["status"] == "COMPLETED"
        assert (await client.get(f"/api/backtest/{hang_id}")).json()["status"] == "CANCELLED"


async def test_admission_limits_kill_switch_crash_recovery_and_result_cap(tmp_path: Path) -> None:
    """Contract: BACKTEST_ENABLED=false returns 503; queue overflow 429; size cap & crash fail.

    Defect caught: Unbounded queue memory, bypassed kill-switch, or unrecovered worker pool (R02).
    """
    # 1) Kill-switch disabled -> 503
    async with _async_client(tmp_path / "disabled", backtest_enabled=False) as (client, _app):
        csrf = await _login(client)
        r = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
            },
        )
        assert r.status_code == 503
        status_json = (await client.get("/api/status")).json()
        assert status_json["backtest"]["enabled"] is False

    # 2) Queue depth limit -> 429
    async with _async_client(
        tmp_path / "queue",
        backtest_max_queue_depth=2,
        worker_fn=worker_hang_or_fast,
    ) as (client, _app):
        csrf = await _login(client)
        t0 = 1_760_000_000
        r_run = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 49},
            },
        )
        assert r_run.status_code == 202
        running_id = r_run.json()["job_id"]
        for _ in range(50):
            if (await client.get(f"/api/backtest/{running_id}")).json()["status"] == "RUNNING":
                break
            await asyncio.sleep(0.02)

        base_req = {
            "contract_id": "CON.F.US.MES.Z26",
            "timeframe": "5m",
            "start_time": t0,
            "end_time": t0 + 3600,
        }
        q1 = await client.post("/api/backtest", headers={"X-CSRF-Token": csrf}, json=base_req)
        q2 = await client.post("/api/backtest", headers={"X-CSRF-Token": csrf}, json=base_req)
        q3 = await client.post("/api/backtest", headers={"X-CSRF-Token": csrf}, json=base_req)
        assert q1.status_code == 202
        assert q2.status_code == 202
        assert q3.status_code == 429

        status_snap = (await client.get("/api/status")).json()["backtest"]
        assert status_snap == {
            "enabled": True,
            "running_job_id": running_id,
            "queue_depth": 2,
            "max_queue_depth": 2,
        }

        await client.delete(f"/api/backtest/{running_id}", headers={"X-CSRF-Token": csrf})

    # 3) Oversized result cap -> FAILED
    async with _async_client(
        tmp_path / "sizecap",
        backtest_max_result_bytes=1024,
        worker_fn=worker_oversized_payload,
    ) as (client, _app):
        csrf = await _login(client)
        r = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
            },
        )
        jid = r.json()["job_id"]
        for _ in range(80):
            st = (await client.get(f"/api/backtest/{jid}")).json()
            if st["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert st["status"] == "FAILED"
        assert "exceeds cap" in st["error"]

    # 4) Worker process crash -> FAILED and clean respawn
    async with _async_client(
        tmp_path / "crash",
        worker_fn=worker_crash_once_or_ok,
    ) as (client, _app):
        csrf = await _login(client)
        r_crash = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
                "params": {"max_wait_bars": 48},
            },
        )
        crash_id = r_crash.json()["job_id"]
        for _ in range(80):
            st_c = (await client.get(f"/api/backtest/{crash_id}")).json()
            if st_c["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert st_c["status"] == "FAILED"

        r_ok = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": 1_760_000_000,
                "end_time": 1_760_003_600,
                "params": {"max_wait_bars": 5},
            },
        )
        ok_id = r_ok.json()["job_id"]
        for _ in range(80):
            st_ok = (await client.get(f"/api/backtest/{ok_id}")).json()
            if st_ok["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert st_ok["status"] == "COMPLETED"


async def test_end_to_end_parquet_resample_backtest_api(tmp_path: Path) -> None:
    """Contract: Real worker runs ICT FVG+Sweep on ParquetStore resampled 1m->5m closed bars.

    Defect caught: Serialization breakage across spawn worker or leaking provisional bars.
    """
    async with _async_client(tmp_path) as (client, app):
        csrf = await _login(client)

        # Build 1m bars aligned to a CME 5m boundary in the past (2025-10-10T14:00:00Z = 10:00 ET).
        t_open = int(datetime(2025, 10, 10, 14, 0, tzinfo=UTC).timestamp())
        five_min_bars = _ict_long_setup_bars(t_open, 300)
        one_min_bars: list[OHLCVBar] = []
        for idx, b5 in enumerate(five_min_bars):
            is_last_provisional = idx == len(five_min_bars) - 1
            for m in range(5):
                bar_ts = b5.time + m * 60
                o = b5.open if m == 0 else b5.close
                c = b5.close if m == 4 else b5.close
                h = b5.high if m == 2 else max(o, c)
                low = b5.low if m == 1 else min(o, c)
                one_min_bars.append(
                    _bar(bar_ts, o, h, low, c, b5.volume / 5.0, is_closed=not is_last_provisional)
                )

        m1_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
        await app.state.parquet_store.upsert_bars(m1_key, one_min_bars)

        submit = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "resample_from": "1m",
                "start_time": t_open,
                "end_time": t_open + len(five_min_bars) * 300,
                "strategy_id": "ict_fvg_sweep",
                "params": {"swing_lookback": 5, "ema_period": 3},
            },
        )
        assert submit.status_code == 202
        job_id = submit.json()["job_id"]

        detail: dict[str, Any] = {}
        for _ in range(120):
            detail = (await client.get(f"/api/backtest/{job_id}")).json()
            if detail["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)

        assert detail["status"] == "COMPLETED"
        result = detail["result"]
        assert result["bar_count"] == len(five_min_bars) - 1
        assert result["strategy_id"] == "ict_fvg_sweep"
        assert result["instrument"]["tick_size"] == INSTRUMENTS["MES"].tick_size
        assert len(result["trades"]) >= 1
        assert len(result["markers"]) == len(result["trades"]) * 2
        assert len(result["trade_lines"]) == len(result["trades"])
        assert len(result["zones"]) >= 2
        assert [ind["id"] for ind in result["indicators"]] == ["ema_3", "session_vwap"]
        # Strict JSON compliance (zero NaN/Inf)
        encoded = json.dumps(result, allow_nan=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) == detail["result_bytes"]


def test_ict_short_fvg_ce_and_mgc_decimal_snapping() -> None:
    """Contract: ICT FVG+Sweep handles SHORT + fvg_ce mode and exact Decimal MGC tick snapping.

    Defect caught: Float precision loss (1850.4499999999998 rounding down to 1850.4) or broken
    SHORT / fvg_ce limit entry logic.
    """
    t0 = 1_760_000_000
    # 1) SHORT + entry_mode="fvg_ce" on MES (tick_size=0.25, tick_value=$1.25)
    # Bars 0..4: swing high at 105.00 (bar 2)
    # Bar 5: bearish BSL sweep (high=105.50 > 105.00, close=104.50 < 105.00, low=104.25)
    # Bar 6: bearish impulse
    # Bar 7: bearish FVG (high=103.50 < bar 5 low=104.25 -> FVG [103.50, 104.25], CE=103.875)
    # Bar 8: pullback into CE=103.875 (high=104.00, low=103.50) -> limit fill at 103.875
    # Bar 9: drop hitting TP
    short_specs = [
        (104.00, 104.50, 103.50, 104.00),  # 0
        (104.00, 104.75, 103.75, 104.50),  # 1
        (104.50, 105.00, 104.00, 104.50),  # 2: prior swing high = 105.00
        (104.50, 104.75, 103.75, 104.00),  # 3
        (104.00, 104.50, 103.75, 104.25),  # 4
        (104.25, 105.50, 104.25, 104.50),  # 5: BSL sweep (high=105.50 > 105.00)
        (104.50, 104.50, 103.25, 103.50),  # 6
        (103.25, 103.50, 102.75, 103.00),  # 7: bearish FVG [103.50, 104.25], midpoint=103.875
        (103.00, 104.00, 102.75, 103.25),  # 8: intrabar limit fill @ 103.875
        (103.25, 103.50, 99.00, 99.50),    # 9: hits TP
    ]
    short_bars = [_bar(t0 + i * 300, o, h, low, c) for i, (o, h, low, c) in enumerate(short_specs)]
    short_res = run_backtest(
        short_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="5m",
        strategy_id="ict_fvg_sweep",
        params={
            "swing_lookback": 5,
            "entry_mode": "fvg_ce",
            "sl_buffer_ticks": 1.0,
            "risk_reward": 2.0,
            "quantity": 2,
        },
    )
    assert len(short_res.trades) == 1
    tr = short_res.trades[0]
    assert tr.direction == "SHORT"
    assert tr.quantity == 2
    assert tr.entry_price == 103.875
    # SL = 105.50 + 0.25 = 105.75; risk = 105.75 - 103.875 = 1.875;
    # raw_tp = 103.875 - 3.75 = 100.125 -> snaps ROUND_HALF_UP on 0.25 tick to 100.25
    assert tr.stop_loss == 105.75
    assert tr.take_profit == 100.25
    assert tr.exit_price == 100.25
    assert tr.exit_reason == "take_profit"
    assert tr.pnl_ticks == 14.5
    assert tr.pnl_dollars == 36.25

    # 2) MGC half-tick Decimal regression: sweep_extreme=1850.0, close=1850.3, sl_buf=0, rr=0.5
    # Exact raw_tp = 1850.3 + (1850.3 - 1850.0) * 0.5 = 1850.45 -> snaps ROUND_HALF_UP to 1850.5
    mgc_specs = [
        (1850.2, 1850.4, 1850.1, 1850.2),   # 0
        (1850.2, 1850.3, 1850.1, 1850.2),   # 1
        (1850.2, 1850.4, 1850.1, 1850.2),   # 2: prior swing low = 1850.1
        (1850.2, 1850.3, 1850.1, 1850.2),   # 3
        (1850.2, 1850.3, 1850.1, 1850.15),  # 4
        (1850.15, 1850.15, 1850.0, 1850.15),# 5: SSL sweep (low=1850.0 < 1850.1)
        (1850.15, 1850.3, 1850.1, 1850.25), # 6
        (1850.25, 1850.4, 1850.25, 1850.3), # 7: bullish FVG (1850.25 > 1850.15), close=1850.3
        (1850.3, 1850.6, 1850.2, 1850.5),   # 8: entry @ 1850.3, hits TP=1850.5
    ]
    mgc_bars = [_bar(t0 + i * 60, o, h, low, c) for i, (o, h, low, c) in enumerate(mgc_specs)]
    mgc_res = run_backtest(
        mgc_bars,
        contract_id="CON.F.US.MGC.Z26",
        timeframe="1m",
        strategy_id="ict_fvg_sweep",
        params={
            "swing_lookback": 5,
            "sl_buffer_ticks": 0.0,
            "risk_reward": 0.5,
            "min_fvg_ticks": 1.0,
        },
    )
    assert len(mgc_res.trades) == 1
    assert mgc_res.trades[0].stop_loss == 1850.0
    assert mgc_res.trades[0].take_profit == 1850.5


async def test_cancel_cleanup_serializes_before_next_queued_job_starts(tmp_path: Path) -> None:
    """Contract: Dispatcher waits for cancel worker cleanup before starting the next queued job.

    Defect caught: Concurrent cancel + dispatcher race spawning next job while old worker lives.
    """
    async with _async_client(
        tmp_path,
        backtest_timeout_sec=30.0,
        worker_fn=worker_hang_or_fast,
    ) as (client, app):
        csrf = await _login(client)
        t0 = 1_760_000_000
        svc = app.state.backtest_service

        cleanup_entered = threading.Event()
        cleanup_release = threading.Event()
        real_cleanup = svc._terminate_and_reset_executor_sync

        def _gated_cleanup() -> None:
            cleanup_entered.set()
            cleanup_release.wait(timeout=5.0)
            real_cleanup()

        svc._terminate_and_reset_executor_sync = _gated_cleanup

        r1 = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.RACE",
                "root_symbol": "MES",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
                "params": {"max_wait_bars": 49},
            },
        )
        job1_id = r1.json()["job_id"]
        marker = Path(tempfile.gettempdir()) / "topstep_hang_CON.F.US.MES.RACE.pid"
        try:
            for _ in range(100):
                if marker.is_file():
                    break
                await asyncio.sleep(0.02)
            assert marker.is_file()

            r2 = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.RACE",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                    "params": {"max_wait_bars": 5},
                },
            )
            job2_id = r2.json()["job_id"]

            cancel_task = asyncio.create_task(
                client.delete(f"/api/backtest/{job1_id}", headers={"X-CSRF-Token": csrf})
            )
            for _ in range(100):
                if cleanup_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert cleanup_entered.is_set()

            # While cleanup is blocked behind barrier, job2 must remain QUEUED.
            await asyncio.sleep(0.15)
            assert (await client.get(f"/api/backtest/{job2_id}")).json()["status"] == "QUEUED"

            cleanup_release.set()
            del_resp = await cancel_task
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "CANCELLED"

            for _ in range(100):
                st2 = (await client.get(f"/api/backtest/{job2_id}")).json()
                if st2["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)
            assert st2["status"] == "COMPLETED"
        finally:
            cleanup_release.set()
            marker.unlink(missing_ok=True)


async def test_load_closed_bars_timeout_and_idle_worker_crash_recovery(tmp_path: Path) -> None:
    """Contract: Wall-clock timeout covers _load_closed_bars and broken idle pools respawn.

    Defect caught: Stalled Parquet reads hanging RUNNING or idle pool crashes bricking queue.
    """
    # 1) _load_closed_bars stall triggers TIMED_OUT and allows next job to complete.
    async with _async_client(
        tmp_path / "load_timeout",
        backtest_timeout_sec=2.0,
        worker_fn=worker_hang_or_fast,
    ) as (client, app):
        csrf = await _login(client)
        t0 = 1_760_000_000
        orig_load_closed = app.state.backtest_service._load_closed_bars
        slow_flag = {"slow": True}

        async def _maybe_slow_load_closed(*args: Any, **kwargs: Any) -> Any:
            if slow_flag["slow"]:
                await asyncio.sleep(5.0)
            return await orig_load_closed(*args, **kwargs)

        app.state.backtest_service._load_closed_bars = _maybe_slow_load_closed
        r_slow = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
            },
        )
        slow_id = r_slow.json()["job_id"]
        for _ in range(80):
            st_slow = (await client.get(f"/api/backtest/{slow_id}")).json()
            if st_slow["status"] == "TIMED_OUT":
                break
            await asyncio.sleep(0.05)
        assert st_slow["status"] == "TIMED_OUT"

        slow_flag["slow"] = False
        r_next = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
            },
        )
        next_id = r_next.json()["job_id"]
        for _ in range(100):
            st_next = (await client.get(f"/api/backtest/{next_id}")).json()
            if st_next["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert st_next["status"] == "COMPLETED"

    # 2) Idle worker killed between jobs -> B recovers or fails cleanly, C always COMPLETED.
    async with _async_client(
        tmp_path / "idle_kill",
        worker_fn=worker_hang_or_fast,
    ) as (client, app):
        csrf = await _login(client)
        t0 = 1_760_000_000
        ra = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
            },
        )
        ja = ra.json()["job_id"]
        for _ in range(100):
            sta = (await client.get(f"/api/backtest/{ja}")).json()
            if sta["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert sta["status"] == "COMPLETED"
        idle_pid = int(sta["result"]["worker_pid"])

        # Kill the idle worker process directly behind the pool's back.
        from app.services.backtest_service import _terminate_pid_tree

        _terminate_pid_tree(idle_pid)
        for _ in range(40):
            if not _is_pid_alive(idle_pid):
                break
            await asyncio.sleep(0.05)
        assert not _is_pid_alive(idle_pid)

        rb = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
            },
        )
        rc = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 3600,
            },
        )
        jb = rb.json()["job_id"]
        jc = rc.json()["job_id"]
        for _ in range(100):
            stb = (await client.get(f"/api/backtest/{jb}")).json()
            stc = (await client.get(f"/api/backtest/{jc}")).json()
            if stb["status"] in {"COMPLETED", "FAILED"} and stc["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert stb["status"] in {"COMPLETED", "FAILED"}
        assert stc["status"] == "COMPLETED"
        assert stc["result"]["worker_pid"] != idle_pid


async def test_metadata_mismatch_max_bars_pruning_and_worker_pid_death(tmp_path: Path) -> None:
    """Contract: Non-resampled root_symbol mismatch, max_bars cap, job retention pruning, PID death.

    Defect caught: Cross-symbol Parquet metadata bypass, unbounded bars/jobs, or zombie workers.
    """
    # 1) Opaque contract_id root_symbol mismatch on non-resampled read_bars + max_bars cap
    async with _async_client(
        tmp_path / "meta",
        backtest_max_bars=10,
        backtest_max_jobs_retained=2,
    ) as (client, app):
        csrf = await _login(client)
        t0 = int(datetime(2025, 10, 10, 14, 0, tzinfo=UTC).timestamp())
        bars_12 = _ict_long_setup_bars(t0, 300)
        opaque_key = StorageDatasetKey("topstepx", False, "OPAQUE.12345", "5m")
        await app.state.parquet_store.upsert_bars(opaque_key, bars_12, root_symbol="MES")

        # Requesting MNQ against stored MES metadata must fail cleanly.
        r_mismatch = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "OPAQUE.12345",
                "root_symbol": "MNQ",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 12 * 300,
            },
        )
        assert r_mismatch.status_code == 202
        mismatch_id = r_mismatch.json()["job_id"]
        for _ in range(100):
            st_m = (await client.get(f"/api/backtest/{mismatch_id}")).json()
            if st_m["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert st_m["status"] == "FAILED"
        assert "conflicts with stored root_symbol" in st_m["error"]

        # Requesting 12 bars with max_bars=5 must fail at max_bars cap.
        r_cap = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "OPAQUE.12345",
                "root_symbol": "MES",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 12 * 300,
                "max_bars": 5,
            },
        )
        cap_id = r_cap.json()["job_id"]
        for _ in range(100):
            st_cap = (await client.get(f"/api/backtest/{cap_id}")).json()
            if st_cap["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert st_cap["status"] == "FAILED"
        assert "exceeds max_bars cap" in st_cap["error"]

        # Submit a 3rd valid job -> backtest_max_jobs_retained=2 prunes oldest terminal job.
        r_ok = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "OPAQUE.12345",
                "root_symbol": "MES",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 4 * 300,
                "max_bars": 5,
            },
        )
        ok_id = r_ok.json()["job_id"]
        for _ in range(100):
            st_ok = (await client.get(f"/api/backtest/{ok_id}")).json()
            if st_ok["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert st_ok["status"] == "COMPLETED"
        listed = (await client.get("/api/backtest")).json()
        assert listed["count"] == 2
        assert (await client.get(f"/api/backtest/{mismatch_id}")).status_code == 404

    # 2) Real execute_backtest_in_worker checks max_result_bytes in subprocess without blocking.
    async with _async_client(
        tmp_path / "real_cap",
        backtest_max_result_bytes=1024,
    ) as (client, app):
        csrf = await _login(client)
        t0 = int(datetime(2025, 10, 10, 14, 0, tzinfo=UTC).timestamp())
        mes_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "5m")
        await app.state.parquet_store.upsert_bars(mes_key, _ict_long_setup_bars(t0, 300))
        r_big = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 12 * 300,
            },
        )
        big_id = r_big.json()["job_id"]
        for _ in range(100):
            st_big = (await client.get(f"/api/backtest/{big_id}")).json()
            if st_big["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert st_big["status"] == "FAILED"
        assert "exceeds cap" in st_big["error"]

    # 3) Explicit old worker PID death check after timeout and after cancel
    async with _async_client(
        tmp_path / "pid_death",
        backtest_timeout_sec=1.5,
        worker_fn=worker_hang_or_fast,
    ) as (client, _app):
        csrf = await _login(client)
        t0 = 1_760_000_000
        cid_to = "CON.F.US.MES.PIDTO"
        marker_to = Path(tempfile.gettempdir()) / f"topstep_hang_{cid_to}.pid"
        marker_to.unlink(missing_ok=True)
        try:
            r_to = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": cid_to,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                    "params": {"max_wait_bars": 49},
                },
            )
            to_id = r_to.json()["job_id"]
            for _ in range(100):
                if marker_to.is_file():
                    break
                await asyncio.sleep(0.02)
            assert marker_to.is_file()
            to_pid = int(marker_to.read_text(encoding="utf-8").strip())

            for _ in range(100):
                if (await client.get(f"/api/backtest/{to_id}")).json()["status"] == "TIMED_OUT":
                    break
                await asyncio.sleep(0.05)
            for _ in range(40):
                if not _is_pid_alive(to_pid):
                    break
                await asyncio.sleep(0.05)
            assert not _is_pid_alive(to_pid)
        finally:
            marker_to.unlink(missing_ok=True)

        cid_del = "CON.F.US.MES.PIDDEL"
        marker_del = Path(tempfile.gettempdir()) / f"topstep_hang_{cid_del}.pid"
        marker_del.unlink(missing_ok=True)
        try:
            r_del = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": cid_del,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                    "params": {"max_wait_bars": 49},
                },
            )
            del_id = r_del.json()["job_id"]
            for _ in range(100):
                if marker_del.is_file():
                    break
                await asyncio.sleep(0.02)
            assert marker_del.is_file()
            del_pid = int(marker_del.read_text(encoding="utf-8").strip())

            resp_del = await client.delete(
                f"/api/backtest/{del_id}",
                headers={"X-CSRF-Token": csrf},
            )
            assert resp_del.status_code == 200
            assert resp_del.json()["status"] == "CANCELLED"
            for _ in range(40):
                if not _is_pid_alive(del_pid):
                    break
                await asyncio.sleep(0.05)
            assert not _is_pid_alive(del_pid)
        finally:
            marker_del.unlink(missing_ok=True)


async def test_real_read_bars_sync_barrier_timeout_unblocks_queue_and_stop(tmp_path: Path) -> None:
    """Contract: Blocked _read_bars_sync times out Job A, unblocks Job B, and bounds stop().

    Defect caught: ParquetStore._run_locked shield hanging asyncio.wait_for, dispatcher, or stop().
    """
    barrier = threading.Event()
    slow_entered = threading.Event()
    t0 = 1_760_000_000

    try:
        async with _async_client(
            tmp_path,
            backtest_timeout_sec=2.0,
            worker_fn=worker_hang_or_fast,
        ) as (client, app):
            csrf = await _login(client)
            store = app.state.parquet_store
            key_slow = StorageDatasetKey("topstepx", False, "CON.F.US.MES.SLOWDS", "5m")
            key_fast = StorageDatasetKey("topstepx", False, "CON.F.US.MES.FASTDS", "5m")
            await store.upsert_bars(
                key_slow, _ict_long_setup_bars(t0, 300), root_symbol="MES"
            )
            await store.upsert_bars(
                key_fast, _ict_long_setup_bars(t0, 300), root_symbol="MES"
            )

            orig_sync = store._read_bars_sync

            def _guarded_read_bars_sync(
                target: Path,
                key: StorageDatasetKey,
                **kwargs: Any,
            ) -> list[OHLCVBar]:
                if key.contract_id == "CON.F.US.MES.SLOWDS":
                    slow_entered.set()
                    barrier.wait(timeout=15.0)
                return orig_sync(target, key, **kwargs)

            store._read_bars_sync = _guarded_read_bars_sync

            ra = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.SLOWDS",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            assert ra.status_code == 202
            job_a = ra.json()["job_id"]

            for _ in range(100):
                if slow_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert slow_entered.is_set()

            rb = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.FASTDS",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            assert rb.status_code == 202
            job_b = rb.json()["job_id"]

            # Job A must time out BEFORE barrier is released.
            st_a: dict[str, Any] = {}
            for _ in range(100):
                st_a = (await client.get(f"/api/backtest/{job_a}")).json()
                if st_a["status"] == "TIMED_OUT":
                    break
                await asyncio.sleep(0.05)
            assert st_a["status"] == "TIMED_OUT"
            assert not barrier.is_set()

            # Job B on dataset 2 must reach COMPLETED while dataset 1's thread is still blocked.
            st_b: dict[str, Any] = {}
            for _ in range(100):
                st_b = (await client.get(f"/api/backtest/{job_b}")).json()
                if st_b["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)
            assert st_b["status"] == "COMPLETED"
            assert not barrier.is_set()

            # Verify stop() completes within bounded time even while Job A's thread is wedged.
            t_stop_start = time.monotonic()
            await app.state.backtest_service.stop()
            stop_elapsed = time.monotonic() - t_stop_start
            assert stop_elapsed < 4.0
    finally:
        barrier.set()


async def test_child_process_tree_killed_on_timeout_and_cancel(tmp_path: Path) -> None:
    """Contract: Timeout and DELETE cancel terminate both worker_pid and spawned child_pid.

    Defect caught: Descendant processes surviving worker kill on timeout or cancellation.
    """
    from app.services.backtest_service import _terminate_pid_tree

    async with _async_client(
        tmp_path,
        backtest_timeout_sec=2.0,
        worker_fn=worker_spawns_child_and_sleeps,
    ) as (client, _app):
        csrf = await _login(client)
        t0 = 1_760_000_000

        # 1) Timeout kills both worker_pid and child_pid
        cid_to = "CON.F.US.MES.TREE_TO"
        marker_to = Path(tempfile.gettempdir()) / f"topstep_tree_{cid_to}.json"
        marker_to.unlink(missing_ok=True)
        pids_to: dict[str, int] = {}
        try:
            r_to = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": cid_to,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            to_id = r_to.json()["job_id"]
            for _ in range(120):
                if marker_to.is_file():
                    break
                await asyncio.sleep(0.05)
            assert marker_to.is_file()
            pids_to = json.loads(marker_to.read_text(encoding="utf-8"))

            for _ in range(120):
                if (await client.get(f"/api/backtest/{to_id}")).json()["status"] == "TIMED_OUT":
                    break
                await asyncio.sleep(0.05)

            for _ in range(40):
                if not _is_pid_alive(pids_to["worker_pid"]) and not _is_pid_alive(
                    pids_to["child_pid"]
                ):
                    break
                await asyncio.sleep(0.05)
            assert not _is_pid_alive(pids_to["worker_pid"])
            assert not _is_pid_alive(pids_to["child_pid"])
        finally:
            if pids_to.get("child_pid"):
                _terminate_pid_tree(pids_to["child_pid"])
            if pids_to.get("worker_pid"):
                _terminate_pid_tree(pids_to["worker_pid"])
            marker_to.unlink(missing_ok=True)

        # 2) DELETE cancel kills both worker_pid and child_pid
        cid_del = "CON.F.US.MES.TREE_DEL"
        marker_del = Path(tempfile.gettempdir()) / f"topstep_tree_{cid_del}.json"
        marker_del.unlink(missing_ok=True)
        pids_del: dict[str, int] = {}
        try:
            r_del = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": cid_del,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            del_id = r_del.json()["job_id"]
            for _ in range(120):
                if marker_del.is_file():
                    break
                await asyncio.sleep(0.05)
            assert marker_del.is_file()
            pids_del = json.loads(marker_del.read_text(encoding="utf-8"))

            resp_del = await client.delete(
                f"/api/backtest/{del_id}",
                headers={"X-CSRF-Token": csrf},
            )
            assert resp_del.status_code == 200
            assert resp_del.json()["status"] == "CANCELLED"

            for _ in range(40):
                if not _is_pid_alive(pids_del["worker_pid"]) and not _is_pid_alive(
                    pids_del["child_pid"]
                ):
                    break
                await asyncio.sleep(0.05)
            assert not _is_pid_alive(pids_del["worker_pid"])
            assert not _is_pid_alive(pids_del["child_pid"])
        finally:
            if pids_del.get("child_pid"):
                _terminate_pid_tree(pids_del["child_pid"])
            if pids_del.get("worker_pid"):
                _terminate_pid_tree(pids_del["worker_pid"])
            marker_del.unlink(missing_ok=True)


async def test_resample_issues_quarantine_and_fail_backtest(tmp_path: Path) -> None:
    """Contract: Resample Float64 overflow quarantines issue in SQLite and fails backtest job.

    Defect caught: Silently dropping corrupted resampled bars instead of quarantining (F04/R04).
    """
    async with _async_client(tmp_path) as (client, app):
        csrf = await _login(client)
        t0 = int(datetime(2025, 10, 10, 14, 0, tzinfo=UTC).timestamp())
        m1_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
        m5_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "5m")

        # Seed valid 1m bars whose 5m volume sum overflows Float64 (1e308 + 1e308 -> inf).
        overflow_bars = [
            _bar(t0, 5000.0, 5001.0, 4999.0, 5000.5, v=1e308, is_closed=True),
            _bar(t0 + 60, 5000.5, 5002.0, 5000.0, 5001.5, v=1e308, is_closed=True),
        ]
        await app.state.parquet_store.upsert_bars(m1_key, overflow_bars)
        await app.state.sqlite_store.record_coverage(m5_key, t0, t0 + 300, status="COMPLETE")

        resp = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "resample_from": "1m",
                "start_time": t0,
                "end_time": t0 + 300,
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        st: dict[str, Any] = {}
        for _ in range(100):
            st = (await client.get(f"/api/backtest/{job_id}")).json()
            if st["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)

        assert st["status"] == "FAILED"
        assert "corrupted" in st["error"].lower()

        quality = await app.state.sqlite_store.get_quality_summary()
        assert quality["quarantined_bars"] >= 1
        assert quality["corrupted_intervals"] >= 1
        assert await app.state.sqlite_store.is_interval_complete(m5_key, t0, t0 + 300) is False


def test_session_vwap_large_volume_decimal_prevents_float64_overflow() -> None:
    """Contract: Decimal accumulation keeps session_vwap exact and finite for 1e308 volumes.

    Defect caught: Float64 overflow turning session_vwap into inf/nan across multiple bars.
    """
    t0 = int(datetime(2026, 1, 15, 15, 0, tzinfo=UTC).timestamp())
    bars = [
        _bar(t0, 100.0, 100.0, 100.0, 100.0, v=1e308),
        _bar(t0 + 60, 100.0, 100.0, 100.0, 100.0, v=1e308),
        _bar(t0 + 120, 100.0, 100.0, 100.0, 100.0, v=1e308),
    ]
    vwap_one = session_vwap(bars[:1])
    vwap_two = session_vwap(bars[:2])
    assert vwap_one == [(t0, 100.0)]
    assert vwap_two == [(t0, 100.0), (t0 + 60, 100.0)]

    bt_res = run_backtest(
        bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy_id="ict_fvg_sweep",
        params={"ema_period": 2, "include_vwap": True},
    )
    payload = bt_res.to_dict()
    encoded = json.dumps(payload, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded


def test_mgc_fvg_ce_decimal_midpoint_and_tp_snapping() -> None:
    """Contract: FVG midpoint and fvg_ce TP snapping use Decimal without binary float drift.

    Defect caught: (1850.1 + 1850.3)/2 drifting to 1850.1999999999998 on MGC limit fills.
    """
    t0 = 1_760_000_000
    mgc_specs = [
        (1850.2, 1850.3, 1850.1, 1850.2),   # 0
        (1850.2, 1850.3, 1850.1, 1850.2),   # 1
        (1850.2, 1850.3, 1850.05, 1850.2),  # 2: swing low = 1850.05
        (1850.2, 1850.3, 1850.1, 1850.2),   # 3
        (1850.2, 1850.3, 1850.1, 1850.1),   # 4
        (1850.1, 1850.1, 1850.0, 1850.1),   # 5: SSL sweep=1850.0; close=1850.1 > 1850.05
        (1850.1, 1850.4, 1850.1, 1850.35),  # 6
        (1850.35, 1850.5, 1850.3, 1850.45), # 7: bullish FVG [1850.1, 1850.3], midpoint=1850.2
        (1850.3, 1850.35, 1850.2, 1850.3),  # 8: touches limit=1850.2 exactly
        (1850.3, 1850.45, 1850.25, 1850.4), # 9: hits TP=1850.4
    ]
    mgc_bars = [_bar(t0 + i * 60, o, h, low, c) for i, (o, h, low, c) in enumerate(mgc_specs)]
    fvgs = detect_fvg_zones(mgc_bars[:8], min_gap_ticks=1.0, tick_size=0.10)
    assert len(fvgs) == 1
    assert fvgs[0].bottom == 1850.1
    assert fvgs[0].top == 1850.3
    assert fvgs[0].midpoint == 1850.2

    res = run_backtest(
        mgc_bars,
        contract_id="CON.F.US.MGC.Z26",
        timeframe="1m",
        strategy_id="ict_fvg_sweep",
        params={
            "swing_lookback": 5,
            "entry_mode": "fvg_ce",
            "sl_buffer_ticks": 1.0,
            "risk_reward": 0.5,
            "min_fvg_ticks": 1.0,
        },
    )
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.entry_price == 1850.2
    assert tr.stop_loss == 1849.9
    assert tr.take_profit == 1850.4
    assert tr.exit_price == 1850.4
    assert tr.exit_reason == "take_profit"


async def test_fallback_json_sizing_cancel_race_preserves_cancelled(tmp_path: Path) -> None:
    """Contract: Cancelling while fallback _measure_result_bytes awaits preserves CANCELLED.

    Defect caught: Fallback JSON sizing completion overwriting CANCELLED with COMPLETED result.
    """
    import app.services.backtest_service as bs_mod

    sizing_entered = threading.Event()
    sizing_release = threading.Event()
    orig_measure = bs_mod._measure_result_bytes

    def _gated_measure(result_dict: dict[str, Any]) -> int:
        sizing_entered.set()
        sizing_release.wait(timeout=5.0)
        return orig_measure(result_dict)

    bs_mod._measure_result_bytes = _gated_measure
    try:
        async with _async_client(tmp_path, worker_fn=worker_hang_or_fast) as (client, app):
            csrf = await _login(client)
            t0 = 1_760_000_000
            resp = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.Z26",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            for _ in range(100):
                if sizing_entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert sizing_entered.is_set()

            del_resp = await client.delete(
                f"/api/backtest/{job_id}",
                headers={"X-CSRF-Token": csrf},
            )
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "CANCELLED"

            sizing_release.set()
            await asyncio.sleep(0.15)
            detail = (await client.get(f"/api/backtest/{job_id}")).json()
            assert detail["status"] == "CANCELLED"
            assert detail["result"] is None
            assert app.state.backtest_service.get_job(job_id).result is None
    finally:
        sizing_release.set()
        bs_mod._measure_result_bytes = orig_measure


async def test_default_threadpool_saturation_does_not_hang_stop(tmp_path: Path) -> None:
    """Contract: Saturated default ThreadPoolExecutor(max_workers=1) does not stall stop().

    Defect caught: Using asyncio.to_thread for executor teardown deadlocking behind blocked I/O.
    """
    loop = asyncio.get_running_loop()
    orig_executor = getattr(loop, "_default_executor", None)
    tiny_pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(tiny_pool)
    barrier = threading.Event()
    blocked_entered = threading.Event()
    t0 = 1_760_000_000

    try:
        async with _async_client(
            tmp_path,
            backtest_timeout_sec=1.5,
            worker_fn=worker_hang_or_fast,
        ) as (client, app):
            csrf = await _login(client)
            store = app.state.parquet_store
            key_ok = StorageDatasetKey("topstepx", False, "CON.F.US.MES.POOL1", "5m")
            key_sat = StorageDatasetKey("topstepx", False, "CON.F.US.MES.POOL2", "5m")
            await store.upsert_bars(key_ok, _ict_long_setup_bars(t0, 300), root_symbol="MES")
            await store.upsert_bars(key_sat, _ict_long_setup_bars(t0, 300), root_symbol="MES")

            # 1) Warm up and create the ProcessPoolExecutor worker via Job 1.
            r1 = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.POOL1",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            j1 = r1.json()["job_id"]
            for _ in range(100):
                if (await client.get(f"/api/backtest/{j1}")).json()["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)

            # 2) Saturate the 1-thread default pool on Job 2's _read_bars_sync.
            orig_sync = store._read_bars_sync

            def _blocking_sync(
                target: Path,
                key: StorageDatasetKey,
                **kwargs: Any,
            ) -> list[OHLCVBar]:
                if key.contract_id == "CON.F.US.MES.POOL2":
                    blocked_entered.set()
                    barrier.wait(timeout=15.0)
                return orig_sync(target, key, **kwargs)

            store._read_bars_sync = _blocking_sync
            r2 = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.POOL2",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            j2 = r2.json()["job_id"]
            for _ in range(100):
                if blocked_entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert blocked_entered.is_set()

            st2: dict[str, Any] = {}
            for _ in range(100):
                st2 = (await client.get(f"/api/backtest/{j2}")).json()
                if st2["status"] == "TIMED_OUT":
                    break
                await asyncio.sleep(0.05)
            assert st2["status"] == "TIMED_OUT"

            # 3) Call stop() BEFORE releasing barrier while default thread pool is saturated.
            t_stop = time.monotonic()
            await app.state.backtest_service.stop()
            stop_elapsed = time.monotonic() - t_stop
            assert stop_elapsed < 4.0
            assert not barrier.is_set()
    finally:
        barrier.set()
        loop.set_default_executor(
            orig_executor if orig_executor is not None else ThreadPoolExecutor()
        )
        tiny_pool.shutdown(wait=True)


async def test_preexisting_defective_coverage_fails_backtest(tmp_path: Path) -> None:
    """Contract: Defective SQLite coverage fails overlapping direct/resampled backtests.

    Defect caught: Executing backtests over ranges marked CORRUPTED/TRUNCATED/PROVIDER_GAP.
    """
    async with _async_client(tmp_path) as (client, app):
        csrf = await _login(client)
        t0 = int(datetime(2025, 10, 10, 14, 0, tzinfo=UTC).timestamp())
        m1_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
        m5_key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "5m")

        five_min_bars = _ict_long_setup_bars(t0, 300)
        one_min_bars = [
            _bar(b.time + m * 60, b.open, b.high, b.low, b.close, b.volume / 5.0)
            for b in five_min_bars
            for m in range(5)
        ]
        await app.state.parquet_store.upsert_bars(m5_key, five_min_bars)
        await app.state.parquet_store.upsert_bars(m1_key, one_min_bars)

        # Mark [t0, t0 + 300) on 5m as CORRUPTED and [t0 + 600, t0 + 900) on 1m as PROVIDER_GAP.
        await app.state.sqlite_store.record_coverage(
            m5_key, t0, t0 + 300, status="CORRUPTED", quarantined_count=1
        )
        await app.state.sqlite_store.record_coverage(
            m1_key, t0 + 600, t0 + 900, status="PROVIDER_GAP", provider_gap=True
        )
        # Non-overlapping clean range [t0 + 1200, t0 + 3000) is COMPLETE.
        await app.state.sqlite_store.record_coverage(
            m5_key, t0 + 1200, t0 + 3000, status="COMPLETE"
        )

        # 1) Direct read_bars intersecting CORRUPTED 5m coverage -> FAILED
        r_direct = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0,
                "end_time": t0 + 600,
            },
        )
        jd = r_direct.json()["job_id"]
        for _ in range(100):
            std = (await client.get(f"/api/backtest/{jd}")).json()
            if std["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert std["status"] == "FAILED"
        assert "defective coverage" in std["error"].lower()

        # 2) Resampled 1m->5m intersecting PROVIDER_GAP 1m coverage -> FAILED
        r_res = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "resample_from": "1m",
                "start_time": t0 + 600,
                "end_time": t0 + 1200,
            },
        )
        jr = r_res.json()["job_id"]
        for _ in range(100):
            str_ = (await client.get(f"/api/backtest/{jr}")).json()
            if str_["status"] == "FAILED":
                break
            await asyncio.sleep(0.05)
        assert str_["status"] == "FAILED"
        assert "defective coverage" in str_["error"].lower()

        # 3) Non-overlapping clean range -> COMPLETED
        r_clean = await client.post(
            "/api/backtest",
            headers={"X-CSRF-Token": csrf},
            json={
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "5m",
                "start_time": t0 + 1200,
                "end_time": t0 + 3000,
            },
        )
        jc = r_clean.json()["job_id"]
        for _ in range(100):
            stc = (await client.get(f"/api/backtest/{jc}")).json()
            if stc["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert stc["status"] == "COMPLETED"


def test_order_blocks_beyond_lookback_bullish_and_bearish() -> None:
    """Contract: detect_order_blocks locates the last prior opposing candle even > lookback away.

    Defect caught: Bounding opposing-candle search to [idx - lookback, idx) missing real OBs.
    """
    t0 = 1_760_000_000
    # 1) Bullish OB: bar 0 bearish, bars 1..3 bullish consolidation, bar 4 breaks prior 3-bar high.
    bull_bars = [
        _bar(t0 + 0 * 60, 102.0, 103.0, 100.0, 100.5),  # 0: bearish OB candidate
        _bar(t0 + 1 * 60, 100.5, 102.0, 100.25, 101.0), # 1: bullish
        _bar(t0 + 2 * 60, 101.0, 102.25, 100.75, 101.5),# 2: bullish
        _bar(t0 + 3 * 60, 101.5, 102.5, 101.0, 102.0),  # 3: bullish (prior 3 high = 102.5)
        _bar(t0 + 4 * 60, 102.0, 104.0, 101.75, 103.5), # 4: displacement close > 102.5
    ]
    bull_obs = detect_order_blocks(bull_bars, lookback=3)
    assert len(bull_obs) == 1
    assert bull_obs[0].direction == "bullish"
    assert bull_obs[0].start_time == bull_bars[0].time
    assert bull_obs[0].end_time == bull_bars[4].time
    assert bull_obs[0].detected_at_time == bull_bars[4].time
    assert bull_obs[0].top == 103.0
    assert bull_obs[0].bottom == 100.0

    # 2) Bearish OB: bar 0 bullish, bars 1..3 bearish consolidation, bar 4 breaks prior 3-bar low.
    bear_bars = [
        _bar(t0 + 0 * 60, 100.5, 103.0, 100.0, 102.5),  # 0: bullish OB candidate
        _bar(t0 + 1 * 60, 102.5, 102.75, 101.0, 102.0), # 1: bearish
        _bar(t0 + 2 * 60, 102.0, 102.25, 100.75, 101.5),# 2: bearish
        _bar(t0 + 3 * 60, 101.5, 102.0, 100.5, 101.0),  # 3: bearish (prior 3 low = 100.5)
        _bar(t0 + 4 * 60, 101.0, 101.25, 99.0, 99.5),   # 4: displacement close < 100.5
    ]
    bear_obs = detect_order_blocks(bear_bars, lookback=3)
    assert len(bear_obs) == 1
    assert bear_obs[0].direction == "bearish"
    assert bear_obs[0].start_time == bear_bars[0].time
    assert bear_obs[0].end_time == bear_bars[4].time
    assert bear_obs[0].detected_at_time == bear_bars[4].time
    assert bear_obs[0].top == 103.0
    assert bear_obs[0].bottom == 100.0


async def test_fallback_json_sizing_timeout_unblocks_next_job(tmp_path: Path) -> None:
    """Contract: Hung fallback _measure_result_bytes times out Job A and unblocks Job B.

    Defect caught: Unbounded await on fallback _measure_result_bytes hanging RUNNING forever.
    """
    import app.services.backtest_service as bs_mod

    sizing_entered = threading.Event()
    sizing_release = threading.Event()
    orig_measure = bs_mod._measure_result_bytes

    def _gated_measure(result_dict: dict[str, Any]) -> int:
        if result_dict.get("contract_id") == "CON.F.US.MES.SIZE_TO":
            sizing_entered.set()
            sizing_release.wait(timeout=15.0)
        return orig_measure(result_dict)

    bs_mod._measure_result_bytes = _gated_measure
    try:
        async with _async_client(
            tmp_path,
            backtest_timeout_sec=1.5,
            worker_fn=worker_hang_or_fast,
        ) as (client, _app):
            csrf = await _login(client)
            t0 = 1_760_000_000

            ra = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.SIZE_TO",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            assert ra.status_code == 202
            ja = ra.json()["job_id"]

            for _ in range(100):
                if sizing_entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert sizing_entered.is_set()

            rb = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.SIZE_OK",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            assert rb.status_code == 202
            jb = rb.json()["job_id"]

            sta: dict[str, Any] = {}
            stb: dict[str, Any] = {}
            for _ in range(120):
                sta = (await client.get(f"/api/backtest/{ja}")).json()
                stb = (await client.get(f"/api/backtest/{jb}")).json()
                if sta["status"] == "TIMED_OUT" and stb["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)

            assert sta["status"] == "TIMED_OUT"
            assert stb["status"] == "COMPLETED"
            assert not sizing_release.is_set()
    finally:
        sizing_release.set()
        bs_mod._measure_result_bytes = orig_measure


async def test_bounded_daemon_io_cap_prevents_unbounded_thread_spawns(tmp_path: Path) -> None:
    """Contract: _run_bounded_daemon_io caps active threads at 4 and recovers after release.

    Defect caught: Unbounded orphaned daemon thread spawning when storage metadata reads stall.
    """
    import app.services.backtest_service as bs_mod

    gate = threading.Event()
    entered_count_lock = threading.Lock()
    entered_count = {"n": 0}
    orig_read_meta = bs_mod._read_parquet_stored_root

    def _gated_read_meta(target: Path) -> str | None:
        if "CON.F.US.MES.SAT" in str(target):
            with entered_count_lock:
                entered_count["n"] += 1
            gate.wait(timeout=15.0)
        return orig_read_meta(target)

    bs_mod._read_parquet_stored_root = _gated_read_meta
    try:
        async with _async_client(
            tmp_path,
            backtest_max_queue_depth=16,
            backtest_timeout_sec=0.35,
            worker_fn=worker_hang_or_fast,
        ) as (client, app):
            csrf = await _login(client)
            svc = app.state.backtest_service
            t0 = 1_760_000_000

            job_ids: list[str] = []
            for idx in range(8):
                r = await client.post(
                    "/api/backtest",
                    headers={"X-CSRF-Token": csrf},
                    json={
                        "contract_id": f"CON.F.US.MES.SAT{idx}",
                        "root_symbol": "MES",
                        "timeframe": "5m",
                        "start_time": t0,
                        "end_time": t0 + 3600,
                    },
                )
                assert r.status_code == 202
                job_ids.append(r.json()["job_id"])

            statuses: list[dict[str, Any]] = []
            for _ in range(160):
                assert svc.active_daemon_io_ops <= 4
                statuses = [
                    (await client.get(f"/api/backtest/{jid}")).json() for jid in job_ids
                ]
                if all(st["status"] in {"TIMED_OUT", "FAILED"} for st in statuses):
                    break
                await asyncio.sleep(0.05)

            assert svc.active_daemon_io_ops == 4
            assert entered_count["n"] == 4
            assert [st["status"] for st in statuses[:4]] == ["TIMED_OUT"] * 4
            assert [st["status"] for st in statuses[4:]] == ["FAILED"] * 4
            assert all("saturated" in str(st["error"]).lower() for st in statuses[4:])

            gate.set()
            for _ in range(100):
                if svc.active_daemon_io_ops == 0:
                    break
                await asyncio.sleep(0.02)
            assert svc.active_daemon_io_ops == 0

            app.state.settings.backtest_timeout_sec = 5.0
            r_ok = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": "CON.F.US.MES.CLEAN",
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 3600,
                },
            )
            j_ok = r_ok.json()["job_id"]
            st_ok: dict[str, Any] = {}
            for _ in range(100):
                st_ok = (await client.get(f"/api/backtest/{j_ok}")).json()
                if st_ok["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.05)
            assert st_ok["status"] == "COMPLETED"
    finally:
        gate.set()
        bs_mod._read_parquet_stored_root = orig_read_meta


def test_limit_order_expiry_and_open_gap_fill_exit_pricing() -> None:
    """Contract: Limit orders expire at max_wait_bars and open gaps fill/exit at bar.open.

    Defect caught: Stale limit orders filling past max_wait_bars or gap fills using limit/SL/TP.
    """
    t0 = 1_760_000_000

    # 1) Limit expiry strictly at max_wait_bars=2 (bar 3 touch must NOT fill).
    exp_bars = [
        _bar(t0 + 0 * 60, 102.0, 102.5, 101.5, 102.0),  # 0: signal limit LONG @ 100.0
        _bar(t0 + 1 * 60, 102.0, 102.5, 101.0, 101.5),  # 1: wait=1, no touch
        _bar(t0 + 2 * 60, 101.5, 102.0, 101.0, 101.5),  # 2: wait=2 == max_wait_bars -> expires
        _bar(t0 + 3 * 60, 101.5, 102.0, 99.5, 100.5),   # 3: touches 100.0 after expiry
    ]
    exp_res = run_backtest(
        exp_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "LONG",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
                "max_wait_bars": 2,
            }
        ),
    )
    assert len(exp_res.trades) == 0

    # 2) Favorable open-gap limit fill + adverse open-gap SL exit (LONG)
    long_gap_sl_bars = [
        _bar(t0 + 0 * 60, 101.0, 101.5, 100.5, 101.0),  # 0: emit LONG limit @ 100.0
        _bar(t0 + 1 * 60, 99.0, 100.0, 98.5, 99.5),     # 1: gaps open=99.0 <= 100.0 -> fill @ 99.0
        _bar(t0 + 2 * 60, 97.0, 98.5, 96.5, 97.5),      # 2: gap open <= SL=98.0 -> exit @ 97.0
    ]
    r_l_sl = run_backtest(
        long_gap_sl_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "LONG",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
            }
        ),
    )
    assert r_l_sl.trades[0].entry_price == 99.0
    assert r_l_sl.trades[0].exit_price == 97.0
    assert r_l_sl.trades[0].exit_reason == "stop_loss"

    # 3) Favorable open-gap TP exit (LONG)
    long_gap_tp_bars = [
        _bar(t0 + 0 * 60, 101.0, 101.5, 100.5, 101.0),  # 0: emit LONG market
        _bar(t0 + 1 * 60, 101.0, 102.0, 100.5, 101.5),  # 1: fill @ 101.0
        _bar(t0 + 2 * 60, 105.0, 105.5, 104.5, 105.0),  # 2: gap open >= TP=104.0 -> exit @ 105.0
    ]
    r_l_tp = run_backtest(
        long_gap_tp_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "LONG",
                "order_type": "market",
                "stop_loss": 98.0,
                "take_profit": 104.0,
            }
        ),
    )
    assert r_l_tp.trades[0].exit_price == 105.0
    assert r_l_tp.trades[0].exit_reason == "take_profit"

    # 4) Favorable open-gap limit fill + adverse open-gap SL exit (SHORT)
    short_gap_sl_bars = [
        _bar(t0 + 0 * 60, 99.0, 99.5, 98.5, 99.0),     # 0: emit SHORT limit @ 100.0
        _bar(t0 + 1 * 60, 101.0, 101.5, 100.5, 101.0),  # 1: gap open >= 100.0 -> fill @ 101.0
        _bar(t0 + 2 * 60, 103.0, 103.5, 102.5, 103.0),  # 2: gap open >= SL=102.0 -> exit @ 103.0
    ]
    r_s_sl = run_backtest(
        short_gap_sl_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 102.0,
                "take_profit": 96.0,
            }
        ),
    )
    assert r_s_sl.trades[0].entry_price == 101.0
    assert r_s_sl.trades[0].exit_price == 103.0
    assert r_s_sl.trades[0].exit_reason == "stop_loss"

    # 5) Favorable open-gap TP exit (SHORT)
    short_gap_tp_bars = [
        _bar(t0 + 0 * 60, 99.0, 99.5, 98.5, 99.0),     # 0: emit SHORT market
        _bar(t0 + 1 * 60, 99.0, 99.5, 98.0, 98.5),     # 1: fill @ 99.0
        _bar(t0 + 2 * 60, 95.0, 95.5, 94.5, 95.0),     # 2: gaps open=95.0 <= TP=96.0 -> exit @ 95.0
    ]
    r_s_tp = run_backtest(
        short_gap_tp_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "market",
                "stop_loss": 102.0,
                "take_profit": 96.0,
            }
        ),
    )
    assert r_s_tp.trades[0].exit_price == 95.0
    assert r_s_tp.trades[0].exit_reason == "take_profit"


async def test_toctou_opaque_contract_root_symbol_race_fails(tmp_path: Path) -> None:
    """Contract: Concurrent upsert after root pre-check fails opaque root_symbol mismatch.

    Defect caught: TOCTOU race between unlocked pre-check root_symbol read and Parquet read.
    """
    import app.services.backtest_service as bs_mod

    orig_read_meta = bs_mod._read_parquet_stored_root
    precheck_done = threading.Event()
    release_precheck = threading.Event()
    guard_lock = threading.Lock()
    state = {"precheck_seen": False}

    t0 = 1_760_000_000
    opaque_id = "OPAQUE.TOCTOU.1"
    key = StorageDatasetKey("topstepx", False, opaque_id, "5m")

    try:
        async with _async_client(
            tmp_path / "toctou",
            worker_fn=worker_hang_or_fast,
        ) as (client, app):
            csrf = await _login(client)
            store = app.state.parquet_store
            target_path = store.resolve_path(key)

            def _barrier_read_meta(path: Path) -> str | None:
                res = orig_read_meta(path)
                should_wait = False
                if path == target_path:
                    with guard_lock:
                        if not state["precheck_seen"] and res is None:
                            state["precheck_seen"] = True
                            should_wait = True
                if should_wait:
                    precheck_done.set()
                    release_precheck.wait(timeout=15.0)
                return res

            bs_mod._read_parquet_stored_root = _barrier_read_meta

            r_job = await client.post(
                "/api/backtest",
                headers={"X-CSRF-Token": csrf},
                json={
                    "contract_id": opaque_id,
                    "root_symbol": "MES",
                    "timeframe": "5m",
                    "start_time": t0,
                    "end_time": t0 + 5000,
                },
            )
            assert r_job.status_code == 202
            job_id = r_job.json()["job_id"]

            for _ in range(100):
                if precheck_done.is_set():
                    break
                await asyncio.sleep(0.02)
            assert precheck_done.is_set()

            await store.upsert_bars(key, _ict_long_setup_bars(t0, 300), root_symbol="MNQ")
            release_precheck.set()

            st: dict[str, Any] = {}
            for _ in range(100):
                st = (await client.get(f"/api/backtest/{job_id}")).json()
                if st["status"] in {"FAILED", "COMPLETED", "TIMED_OUT", "CANCELLED"}:
                    break
                await asyncio.sleep(0.05)

            assert st["status"] == "FAILED"
            assert "conflicts with stored root_symbol" in str(st["error"])
    finally:
        release_precheck.set()
        bs_mod._read_parquet_stored_root = orig_read_meta


def test_short_intrabar_limit_tp_deferred_and_both_hit_sl_first() -> None:
    """Contract: SHORT intrabar limit fill ignores fill-bar TP-only and picks SL on SL+TP.

    Defect caught: Optimistic SHORT exit on intrabar fill bar or failing conservative SL-first.
    """
    t0 = 1_760_000_000

    # 1) SHORT intrabar limit fill where fill bar touches ONLY TP (no exit until bar[i+1]).
    tp_only_bars = [
        _bar(t0 + 0 * 60, 99.0, 99.5, 98.5, 99.0),     # 0: emit SHORT limit @ 100.0
        _bar(t0 + 1 * 60, 99.5, 100.5, 97.5, 98.5),    # 1: fill @ 100.0; touches TP=98.0 only
        _bar(t0 + 2 * 60, 98.5, 99.0, 97.5, 98.0),     # 2: touches TP=98.0 -> exits here
    ]
    r_tp = run_backtest(
        tp_only_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 102.0,
                "take_profit": 98.0,
            }
        ),
    )
    assert len(r_tp.trades) == 1
    assert r_tp.trades[0].entry_price == 100.0
    assert r_tp.trades[0].entry_time == tp_only_bars[1].time
    assert r_tp.trades[0].exit_price == 98.0
    assert r_tp.trades[0].exit_time == tp_only_bars[2].time
    assert r_tp.trades[0].exit_reason == "take_profit"
    assert r_tp.trades[0].ambiguous_fill is False

    # 2) SHORT intrabar limit fill where fill bar touches BOTH SL and TP -> SL + ambiguous_fill.
    both_bars = [
        _bar(t0 + 0 * 60, 99.0, 99.5, 98.5, 99.0),     # 0: emit SHORT limit @ 100.0
        _bar(t0 + 1 * 60, 99.5, 102.5, 97.5, 99.0),    # 1: fill @ 100.0 + hits SL=102 & TP=98
    ]
    r_both = run_backtest(
        both_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 102.0,
                "take_profit": 98.0,
            }
        ),
    )
    assert len(r_both.trades) == 1
    assert r_both.trades[0].entry_price == 100.0
    assert r_both.trades[0].exit_price == 102.0
    assert r_both.trades[0].exit_time == both_bars[1].time
    assert r_both.trades[0].exit_reason == "stop_loss"
    assert r_both.trades[0].ambiguous_fill is True


def test_short_limit_expiry_and_last_allowed_bar_fills_long_short() -> None:
    """Contract: SHORT limit expires after max_wait_bars; LONG/SHORT fill on exact last bar.

    Defect caught: Off-by-one max_wait_bars expiry dropping last valid bar or allowing expired.
    """
    t0 = 1_760_000_000

    # 1) SHORT limit expiry strictly at max_wait_bars=2 (bar 3 touch must NOT fill).
    short_exp_bars = [
        _bar(t0 + 0 * 60, 98.0, 98.5, 97.5, 98.0),     # 0: emit SHORT limit @ 100.0
        _bar(t0 + 1 * 60, 98.0, 99.0, 97.5, 98.5),     # 1: wait=1, no touch
        _bar(t0 + 2 * 60, 98.5, 99.0, 98.0, 98.5),     # 2: wait=2 == max_wait_bars -> expires
        _bar(t0 + 3 * 60, 98.5, 100.5, 98.0, 99.5),    # 3: touches 100.0 after expiry
    ]
    r_exp = run_backtest(
        short_exp_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 102.0,
                "take_profit": 96.0,
                "max_wait_bars": 2,
            }
        ),
    )
    assert len(r_exp.trades) == 0

    # 2) LONG limit fills on exact signal + max_wait_bars bar (index 2).
    long_last_bars = [
        _bar(t0 + 0 * 60, 102.0, 102.5, 101.5, 102.0),  # 0: emit LONG limit @ 100.0
        _bar(t0 + 1 * 60, 102.0, 102.5, 101.0, 101.5),  # 1: wait=1, no touch
        _bar(t0 + 2 * 60, 101.5, 102.0, 99.5, 101.0),   # 2: exact max_wait_bars=2 -> fill @ 100
        _bar(t0 + 3 * 60, 101.0, 104.5, 100.5, 104.0),  # 3: TP hit @ 104.0
    ]
    r_long_last = run_backtest(
        long_last_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "LONG",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
                "max_wait_bars": 2,
            }
        ),
    )
    assert len(r_long_last.trades) == 1
    assert r_long_last.trades[0].entry_price == 100.0
    assert r_long_last.trades[0].entry_time == long_last_bars[2].time
    assert r_long_last.trades[0].exit_reason == "take_profit"

    # 3) SHORT limit fills on exact signal + max_wait_bars bar (index 2).
    short_last_bars = [
        _bar(t0 + 0 * 60, 98.0, 98.5, 97.5, 98.0),     # 0: emit SHORT limit @ 100.0
        _bar(t0 + 1 * 60, 98.0, 99.0, 97.5, 98.5),     # 1: wait=1, no touch
        _bar(t0 + 2 * 60, 98.5, 100.5, 98.0, 99.0),    # 2: exact max_wait_bars=2 -> fill @ 100
        _bar(t0 + 3 * 60, 99.0, 99.5, 95.5, 96.0),     # 3: TP hit @ 96.0
    ]
    r_short_last = run_backtest(
        short_last_bars,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
        strategy=_SingleSignalStrategy(
            {
                "signal_index": 0,
                "direction": "SHORT",
                "order_type": "limit",
                "limit_price": 100.0,
                "stop_loss": 102.0,
                "take_profit": 96.0,
                "max_wait_bars": 2,
            }
        ),
    )
    assert len(r_short_last.trades) == 1
    assert r_short_last.trades[0].entry_price == 100.0
    assert r_short_last.trades[0].entry_time == short_last_bars[2].time
    assert r_short_last.trades[0].exit_reason == "take_profit"
