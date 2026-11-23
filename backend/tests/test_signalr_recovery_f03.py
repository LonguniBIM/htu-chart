"""Value-first tests for F03 SignalR Market Hub, recovery reconciliation, and status telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.engine.bar_aggregator import BarAggregator, NormalizedTick
from app.engine.instruments import StorageDatasetKey
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import first_bucket_open_at_or_after, has_open_session_in_range
from app.main import create_app
from app.services.live_feed_service import LiveFeedService
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import (
    SIGNALR_RECORD_SEPARATOR,
    SignalRConnectedEvent,
    SignalRDisconnectedEvent,
    SignalRErrorEvent,
    SignalRMalformedTickEvent,
    SignalRMarketHub,
    SignalRTickEvent,
    parse_market_invocation_events,
)

BASE_T = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
CID_MES = "CON.F.US.MES.Z26"


class _MockSignalRTransport:
    """In-memory SignalR WebSocket transport speaking 0x1e-delimited JSON frames."""

    def __init__(
        self,
        *,
        subscribe_error: str | None = None,
        stall_subscribe_send: bool = False,
    ) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.closed = False
        self.handshake_done = asyncio.Event()
        self._subscribe_error = subscribe_error
        self._stall_subscribe_send = stall_subscribe_send
        self._release_stalled_send = asyncio.Event()

    async def send(self, data: str) -> None:
        for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
            if chunk.strip():
                frame = json.loads(chunk)
                if self._stall_subscribe_send and frame.get("target") in {
                    "SubscribeContractQuotes",
                    "SubscribeContractTrades",
                }:
                    await self._release_stalled_send.wait()
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif (
                    frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                    and frame.get("invocationId") is not None
                ):
                    comp: dict[str, Any] = {
                        "type": 3,
                        "invocationId": frame["invocationId"],
                    }
                    if self._subscribe_error:
                        comp["error"] = self._subscribe_error
                    await self.incoming.put(json.dumps(comp) + SIGNALR_RECORD_SEPARATOR)

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        self._release_stalled_send.set()
        await self.incoming.put(ConnectionError("mock socket closed"))

    async def push_frame(self, payload: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(payload) + SIGNALR_RECORD_SEPARATOR)

    async def drop(self, exc: Exception | None = None) -> None:
        await self.incoming.put(exc or ConnectionError("simulated network blip"))


def _wire_bar(
    ts: int,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    vol: float,
) -> dict[str, Any]:
    return {"t": ts, "o": open_p, "h": high_p, "l": low_p, "c": close_p, "v": vol}


def _bars_for_history_payload(
    payload: dict[str, Any],
    *,
    overrides: dict[int, dict[str, Any]] | None = None,
    open_p: float = 100.0,
    high_p: float = 102.0,
    low_p: float = 99.0,
    close_p: float = 101.0,
    vol: float = 5.0,
) -> list[dict[str, Any]]:
    raw_start = str(payload["startTime"]).replace("Z", "+00:00")
    raw_end = str(payload["endTime"]).replace("Z", "+00:00")
    req_start = int(datetime.fromisoformat(raw_start).timestamp())
    req_end = int(datetime.fromisoformat(raw_end).timestamp())
    limit = int(payload.get("limit") or 5000)
    cursor = first_bucket_open_at_or_after(req_start, "1m")
    bars: list[dict[str, Any]] = []
    while cursor + 60 <= req_end:
        if has_open_session_in_range(cursor, cursor + 60):
            if overrides and cursor in overrides:
                bars.append(overrides[cursor])
            else:
                bars.append(_wire_bar(cursor, open_p, high_p, low_p, close_p, vol))
        cursor += 60
    return bars[-limit:] if len(bars) > limit else bars


def _quote_frame(contract_id: str, ts: int, price: float, volume: float) -> dict[str, Any]:
    return {
        "type": 1,
        "target": "GatewayTrade",
        "arguments": [
            contract_id,
            {
                "symbolId": "F.US.MES",
                "price": price,
                "volume": volume,
                "timestamp": datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z"),
            },
        ],
    }


def _market_quote_frame(
    contract_id: str,
    ts: int,
    last_price: float,
    cumulative_volume: float,
) -> dict[str, Any]:
    return {
        "type": 1,
        "target": "GatewayQuote",
        "arguments": [
            contract_id,
            {
                "symbolId": "F.US.MES",
                "lastPrice": last_price,
                "volume": cumulative_volume,
                "lastUpdated": datetime.fromtimestamp(ts, tz=UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        ],
    }


def test_signalr_hub_rejects_non_market_hub_url() -> None:
    """Contract: SignalRMarketHub rejects non-/hubs/market URLs (SEC-001 Read-Only guard).

    Defect caught: Misconfigured hub URL pointing outside the read-only Market Hub.
    """
    with pytest.raises(ValueError):
        Settings(topstepx_rtc_url="wss://rtc.topstepx.com/hubs/account")
    with pytest.raises(ValueError):
        Settings(topstepx_rtc_url="https://rtc.topstepx.com/hubs/market")


async def test_signalr_hub_backoff_redacts_secrets_and_stops_cleanly_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: SignalR reconnects with capped backoff, redacts JWT, and cancels cleanly.

    Defect caught: Unbounded/flat reconnect loops, leaked JWT in error logs, or hung CancelledError.
    """
    sleep_delays: list[float] = []
    secret_jwt = "eyJ-super-secret-signalr-jwt-987654"
    attempt = 0

    async def fake_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        # Record reconnect wait delays from SignalRMarketHub.run() while letting timeouts fire fast.
        if timeout is not None and timeout >= 1.5:
            sleep_delays.append(timeout)
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError()
        return await asyncio.wait_for(awaitable, timeout=timeout)

    async def failing_ws_factory(url: str, headers: dict[str, str]) -> _MockSignalRTransport:
        nonlocal attempt
        attempt += 1
        assert f"access_token={secret_jwt}" in url
        assert headers["Authorization"] == f"Bearer {secret_jwt}"
        if attempt <= 4:
            raise ConnectionError(f"Dial failed for {url} with Bearer {secret_jwt}")
        return _MockSignalRTransport()

    settings = Settings(
        signalr_reconnect_initial_delay_sec=2.0,
        signalr_reconnect_max_delay_sec=8.0,
        upstream_connect_timeout_sec=0.5,
    )
    events: list[Any] = []
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result=secret_jwt),
        extra_secrets_provider=lambda: (secret_jwt,),
        ws_factory=failing_ws_factory,
        on_event=events.append,
        contracts=[CID_MES],
    )

    orig_wait_for = asyncio.wait_for

    async def patched_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        if timeout in {2.0, 4.0, 8.0}:
            sleep_delays.append(timeout)
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError()
        return await orig_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr("app.topstep.signalr_hub.asyncio.wait_for", patched_wait_for)

    task = hub.start()
    for _ in range(50):
        if hub.is_connected:
            break
        await asyncio.sleep(0.01)

    assert hub.is_connected is True
    assert sleep_delays[:4] == [2.0, 4.0, 8.0, 8.0]
    err_msgs = [e.message for e in events if hasattr(e, "message")]
    assert len(err_msgs) == 4
    assert all(secret_jwt not in msg and "***REDACTED***" in msg for msg in err_msgs)

    await hub.stop()
    assert task.done()
    assert hub.is_connected is False


async def test_f03_reconnect_outage_overlapping_rest_reconciliation_and_watermark_persistence(
    tmp_path: Path,
) -> None:
    """Contract: Outage -> RECOVERING -> LIVE reconciles [wm-2, now] and rebuilds active open bar.

    Defect caught: Missing candles across drops, premature alerts, or lost watermark on restart.
    """
    b0 = BASE_T
    b1 = BASE_T + 60
    b2 = BASE_T + 120
    b3 = BASE_T + 180
    b4 = BASE_T + 240  # active open bar after reconnect
    clock_ref = [b0 + 65]

    transports: list[_MockSignalRTransport] = []
    rest_calls: list[dict[str, Any]] = []
    backfill_started = asyncio.Event()
    allow_backfill_finish = asyncio.Event()
    emitted_signals: list[tuple[str, int, str | None, bool]] = []
    states_observed_during_signal: list[str] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-f03-test-secret"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            rest_calls.append(payload)
            if len(rest_calls) == 1:
                bars = _bars_for_history_payload(
                    payload,
                    overrides={b0: _wire_bar(b0, 5950, 5952, 5949, 5951, 10)},
                    open_p=5950,
                    high_p=5952,
                    low_p=5949,
                    close_p=5951,
                    vol=10,
                )
                return httpx.Response(200, json={"success": True, "bars": bars})
            # Reconnect backfill: hold open so ticks stream into bounded recovery queue!
            backfill_started.set()
            await allow_backfill_finish.wait()
            bars = _bars_for_history_payload(
                payload,
                overrides={
                    b0: _wire_bar(b0, 5950, 5952, 5949, 5951, 10),
                    b1: _wire_bar(b1, 5951, 5955, 5950, 5954, 25),
                    b2: _wire_bar(b2, 5954, 5956, 5953, 5955, 18),
                    b3: _wire_bar(b3, 5955, 5958, 5954, 5957, 22),
                },
                open_p=5950,
                high_p=5952,
                low_p=5949,
                close_p=5951,
                vol=10,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_reconnect_initial_delay_sec=0.02,
        signalr_reconnect_max_delay_sec=0.05,
        signalr_recovery_queue_maxsize=64,
        signalr_recovery_timeout_sec=5.0,
    )
    db_path = tmp_path / "data" / "metadata.db"
    sqlite_store = SqliteStore(db_path)
    sqlite_store.initialize()

    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            auth = TopstepAuthService(settings, http_client)
            await auth.login("alice", "api-key-f03")
            rest = TopstepRestClient(settings, auth, http_client)
            broadcaster = WsBroadcaster(client_queue_maxsize=32, send_timeout_sec=1.0)
            agg = BarAggregator(timeframes=("1m",))

            def on_signal(evt: Any, tag: str | None) -> None:
                emitted_signals.append(
                    (evt.timeframe, evt.bar.time, tag, evt.is_recovered_historical)
                )
                states_observed_during_signal.append(
                    (live_feed.feed_state, tag, evt.is_recovered_historical)
                )

            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.02,
                reconnect_max_delay_sec=0.05,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                broadcaster=broadcaster,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                on_bar_signal=on_signal,
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            assert len(transports) == 1

            # Verify SubscribeContractQuotes & SubscribeContractTrades invocations were sent.
            targets = [f.get("target") for f in transports[0].sent_frames if f.get("type") == 1]
            assert "SubscribeContractQuotes" in targets
            assert "SubscribeContractTrades" in targets

            # Seed live tick in b1, then advance clock to b4+30 and drop socket mid-candle!
            await transports[0].push_frame(_quote_frame(CID_MES, b1 + 10, 5952.0, 5.0))
            await asyncio.sleep(0.02)
            assert agg.get_open_bar(CID_MES, False, "1m") is not None

            clock_ref[0] = b4 + 30
            await transports[0].drop()

            # Wait for reconnect #2 to enter RECOVERING and start overlapping REST backfill.
            await asyncio.wait_for(backfill_started.wait(), timeout=2.0)
            assert live_feed.feed_state == "RECOVERING"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.is_contract_live_alerts_enabled(CID_MES) is False

            # Overlapping window must start at watermark (b0) - 2*60s!
            raw_start = rest_calls[1]["startTime"].replace("Z", "+00:00")
            reconnect_start = int(datetime.fromisoformat(raw_start).timestamp())
            assert reconnect_start == b0 - 120

            # Stream buffered ticks during RECOVERING:
            # 1) Stale tick in b3 (must be overridden by REST closed bar b3)
            # 2) Live ticks in active open bar b4 (must rebuild b4 open bar)
            await transports[1].push_frame(_quote_frame(CID_MES, b3 + 20, 9999.0, 999.0))
            await transports[1].push_frame(_quote_frame(CID_MES, b4 + 5, 5957.25, 3.0))
            await transports[1].push_frame(_quote_frame(CID_MES, b4 + 18, 5959.00, 7.0))
            await asyncio.sleep(0.03)

            # Still RECOVERING and live alerts still suppressed while REST is running!
            assert live_feed.feed_state == "RECOVERING"
            assert live_feed.live_alerts_enabled is False

            allow_backfill_finish.set()
            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)

            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            assert live_feed.is_contract_live_alerts_enabled(CID_MES) is True

            # Verify recovered historical bars carry RECOVERED_HISTORICAL and suppress live alerts.
            recovered_signals = [s for s in emitted_signals if s[2] == "RECOVERED_HISTORICAL"]
            assert any(s[1] == b3 and s[3] is True for s in recovered_signals)
            assert all(s[3] is True for s in recovered_signals)
            assert states_observed_during_signal
            assert any(st == "RECOVERING" for st, _tag, _rec in states_observed_during_signal)
            assert all(
                tag == "RECOVERED_HISTORICAL" and is_rec is True
                for st, tag, is_rec in states_observed_during_signal
                if st == "RECOVERING"
            )

            # Verify open bar b4 rebuilt from buffered ticks (without stale b3 tick!).
            rebuilt_open = agg.get_open_bar(CID_MES, False, "1m")
            assert rebuilt_open == OHLCVBar(
                time=b4,
                open=5957.25,
                high=5959.00,
                low=5957.25,
                close=5959.00,
                volume=10.0,
                is_closed=False,
            )

            # Verify Parquet persistence of closed b0..b3.
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            persisted = await parquet_store.read_bars(key, start_ts=b0, end_ts=b4)
            assert [bar.time for bar in persisted] == [b0, b1, b2, b3]
            assert persisted[3].high == 5958.0

            await live_feed.stop()
            await broadcaster.close_all()
    finally:
        sqlite_store.close()

    # Restart SqliteStore from disk and verify persistent watermark survived restart!
    restarted_store = SqliteStore(db_path)
    restarted_store.initialize()
    try:
        key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
        wm = await restarted_store.get_watermark(key)
        assert wm is not None
        assert wm.last_closed_bar_time == b3
        all_wms = await restarted_store.list_watermarks(contract_id=CID_MES)
        assert len(all_wms) == 1 and all_wms[0].last_closed_bar_time == b3
    finally:
        restarted_store.close()


async def test_f03_recovery_timeout_or_error_transitions_to_degraded_and_keeps_alerts_disabled(
    tmp_path: Path,
) -> None:
    """Contract: Backfill timeout or upstream failure during RECOVERING transitions to DEGRADED.

    Defect caught: Falsely reporting LIVE or enabling live alerts when REST recovery fails.
    """
    clock_now = BASE_T + 180
    mode = "timeout"

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-degraded"})
        if request.url.path == "/api/History/retrieveBars":
            if mode == "timeout":
                await asyncio.sleep(1.0)
                return httpx.Response(200, json={"success": True, "bars": []})
            if mode == "http_error":
                return httpx.Response(503, json={"errorMessage": "History service unavailable"})
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                open_p=5950,
                high_p=5951,
                low_p=5949,
                close_p=5950,
                vol=5,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_recovery_timeout_sec=0.08,
        upstream_max_retries=0,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-degraded")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(100):
                if live_feed.feed_state == "DEGRADED":
                    break
                await asyncio.sleep(0.01)

            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            snap = live_feed.get_status_snapshot()
            assert snap["unresolved_gaps"] >= 1
            assert snap["contracts"][CID_MES]["state"] == "DEGRADED"

            # Also verify HTTP 503 failure keeps feed DEGRADED and increments unresolved_gaps.
            mode = "http_error"
            prev_gaps = snap["unresolved_gaps"]
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.get_status_snapshot()["unresolved_gaps"] > prev_gaps

            # Once upstream recovers, retry transitions DEGRADED -> RECOVERING -> LIVE.
            mode = "ok"
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            healed_snap = live_feed.get_status_snapshot()
            assert healed_snap["unresolved_gaps"] == 0
            assert healed_snap["contracts"][CID_MES]["unresolved_gaps"] == 0

            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_bounded_recovery_queue_overflow_increments_counter_and_marks_degraded(
    tmp_path: Path,
) -> None:
    """Contract: Excess ticks in RECOVERING hit queue cap, increment overflow, and mark DEGRADED.

    Defect caught: Unbounded memory growth or silent tick drops during prolonged reconnect recovery.
    """
    clock_now = BASE_T + 180
    transports: list[_MockSignalRTransport] = []
    backfill_started = asyncio.Event()
    release_backfill = asyncio.Event()

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-overflow"})
        if request.url.path == "/api/History/retrieveBars":
            backfill_started.set()
            await release_backfill.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [_wire_bar(BASE_T + 120, 5950, 5951, 5949, 5950, 5)],
                },
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_recovery_queue_maxsize=3,
        signalr_recovery_timeout_sec=5.0,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-overflow")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            await asyncio.wait_for(backfill_started.wait(), timeout=2.0)
            assert live_feed.feed_state == "RECOVERING"

            # Push 10 ticks while queue maxsize is 3 -> 7 overflows, queue never exceeds 3.
            for idx in range(10):
                await transports[0].push_frame(
                    _quote_frame(CID_MES, clock_now + idx, 5951.0 + idx, 1.0)
                )
            await asyncio.sleep(0.05)

            assert live_feed._recovery_queue.qsize() <= 3
            assert live_feed.queue_overflow_count == 7

            release_backfill.set()
            await live_feed.flush_pending_tasks()

            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            snap = live_feed.get_status_snapshot()
            assert snap["queue_overflow_count"] == 7
            assert snap["unresolved_gaps"] >= 1

            await live_feed.stop()
    finally:
        sqlite_store.close()


@asynccontextmanager
async def _authed_app_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport,
    ws_factory: Any = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    upstream = httpx.AsyncClient(transport=transport)
    app = create_app(settings=settings, http_client=upstream, signalr_ws_factory=ws_factory)
    try:
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
            async with httpx.AsyncClient(
                transport=asgi, base_url="http://127.0.0.1:8000"
            ) as client:
                yield client, app
    finally:
        await upstream.aclose()


async def test_f03_status_endpoint_exposes_live_feed_telemetry_without_secret_leakage(
    tmp_path: Path,
) -> None:
    """Contract: GET /api/status exposes live-feed telemetry fields with zero secret leakage.

    Defect caught: Missing telemetry fields or leaked TopstepX JWT/API key in /api/status.
    """
    pw = "status-telemetry-pw"
    pw_hash = PasswordHasher().hash(pw)
    raw_api_key = "secret-topstep-api-key-f03"
    raw_jwt = "eyJ-secret-jwt-token-f03-never-leak"

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": raw_jwt})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(503, json={"errorMessage": f"Upstream failed token={raw_jwt}"})
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "data",
        upstream_max_retries=0,
    )
    async with _authed_app_client(
        settings,
        transport=httpx.MockTransport(handler),
        ws_factory=ws_factory,
    ) as (client, app):
        login_resp = await client.post("/api/auth/login", json={"password": pw})
        assert login_resp.status_code == 200
        csrf = login_resp.json()["csrf_token"]

        s0 = (await client.get("/api/status")).json()
        assert s0["feed_state"] == "DISCONNECTED"
        assert s0["last_event_age_sec"] is None
        assert s0["recovery_duration_ms"] is None
        assert s0["unresolved_gaps"] == 0
        assert s0["queue_overflow_count"] == 0
        assert s0["live_alerts_enabled"] is False
        assert s0["contracts"] == {}

        conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": raw_api_key},
            headers={"X-CSRF-Token": csrf},
        )
        assert conn.status_code == 200

        await app.state.live_feed_service.set_active_contracts([CID_MES], live_mode=False)
        for _ in range(100):
            if app.state.live_feed_service.feed_state == "DEGRADED":
                break
            await asyncio.sleep(0.01)

        status_resp = await client.get("/api/status")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["feed_state"] == "DEGRADED"
        assert isinstance(body["last_event_age_sec"], float)
        assert isinstance(body["recovery_duration_ms"], int)
        assert body["unresolved_gaps"] >= 1
        assert body["queue_overflow_count"] == 0
        assert body["live_alerts_enabled"] is False
        assert CID_MES in body["contracts"]

        serialized = json.dumps(body)
        assert raw_api_key not in serialized
        assert raw_jwt not in serialized
        assert pw_hash not in serialized


async def test_f03_multi_contract_and_multi_timeframe_recovery_retains_cross_contract_ticks(
    tmp_path: Path,
) -> None:
    """Contract: Multi-contract recovery preserves higher TFs (5m-1d) and cross-contract ticks.

    Defect caught: Losing contract A ticks during contract B REST backfill (P1-003/P2-003) and
    leaving 5m/15m/1h/4h/1d bars stale after 1m recovery (P1-002/P2-005).
    """
    cid_a = CID_MES
    cid_b = "CON.F.US.MNQ.Z26"
    b0 = BASE_T  # 14:00:00Z -> aligns on 1m, 5m, 15m, 1h
    b1 = BASE_T + 60
    clock_now = b1 + 30
    mnq_backfill_entered = asyncio.Event()
    release_mnq_backfill = asyncio.Event()

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-multi"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            if payload["contractId"] == cid_a:
                bars = _bars_for_history_payload(
                    payload,
                    overrides={b0: _wire_bar(b0, 100.0, 200.0, 95.0, 150.0, 10.0)},
                    open_p=100.0,
                    high_p=105.0,
                    low_p=99.0,
                    close_p=101.0,
                    vol=5.0,
                )
                return httpx.Response(200, json={"success": True, "bars": bars})
            mnq_backfill_entered.set()
            await release_mnq_backfill.wait()
            bars = _bars_for_history_payload(
                payload,
                overrides={b0: _wire_bar(b0, 20000.0, 20020.0, 19990.0, 20010.0, 8.0)},
                open_p=20000.0,
                high_p=20010.0,
                low_p=19995.0,
                close_p=20005.0,
                vol=4.0,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_timeout_sec=5.0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-multi")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator()  # default 6 timeframes: 1m, 5m, 15m, 1h, 4h, 1d
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[cid_a, cid_b],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                signalr_hub=hub,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([cid_a, cid_b], live_mode=False)

            # Seed pre-disconnect open bar on MES across all 6 timeframes.
            agg.process_tick(
                __import__("app.engine.bar_aggregator", fromlist=["NormalizedTick"]).NormalizedTick(
                    cid_a, False, b0 + 10, 150.0, 2.0
                ),
                now_utc=b0 + 10,
            )

            rec_task = asyncio.create_task(live_feed.trigger_recovery(now_utc=clock_now))
            await asyncio.wait_for(mnq_backfill_entered.wait(), timeout=2.0)

            # While MNQ is awaiting REST, stream a late tick for MES (already reconciled!)
            # plus a tick for MNQ.
            from app.topstep.signalr_hub import SignalRTickEvent

            live_feed.handle_hub_event(
                SignalRTickEvent(cid_a, False, b1 + 10, 180.0, 4.0, "GatewayTrade", 1.0)
            )
            live_feed.handle_hub_event(
                SignalRTickEvent(cid_b, False, b1 + 12, 20015.0, 3.0, "GatewayTrade", 1.1)
            )
            release_mnq_backfill.set()
            await rec_task

            assert live_feed.feed_state == "LIVE"
            assert live_feed._recovery_queue.empty()

            # Verify MES 1m open bar has the late tick, and 5m/15m/1h/4h/1d reflect BOTH
            # REST 1m bar (high=200.0, vol=10.0) and late buffered tick (close=180.0, vol=4.0).
            mes_1m = agg.get_open_bar(cid_a, False, "1m")
            assert mes_1m == OHLCVBar(b1, 180.0, 180.0, 180.0, 180.0, 4.0, is_closed=False)
            expected_htf = {
                "5m": (b0, 14.0),
                "15m": (b0, 14.0),
                "1h": (b0, 14.0),
                "4h": (b0 - 10800, 914.0),
                "1d": (b0 - 54000, 4514.0),
            }
            for htf, (exp_time, exp_vol) in expected_htf.items():
                htf_bar = agg.get_open_bar(cid_a, False, htf)
                assert htf_bar is not None
                assert htf_bar.time == exp_time
                assert htf_bar.open == 100.0
                assert htf_bar.high == 200.0
                assert htf_bar.low == 95.0
                assert htf_bar.close == 180.0
                assert htf_bar.volume == exp_vol

            mnq_5m = agg.get_open_bar(cid_b, False, "5m")
            assert mnq_5m is not None
            assert mnq_5m.close == 20015.0
            assert mnq_5m.volume == 11.0
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_tail_drain_race_absorbs_tick_arriving_during_tail_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Ticks arriving during tail-drain awaits are processed before LIVE transition.

    Defect caught: Tail ticks stranded in _recovery_queue after LIVE transition (P1-004).
    """
    b0 = BASE_T
    b1 = BASE_T + 60
    b2 = BASE_T + 120
    b3 = BASE_T + 180
    clock_ref = [b2 + 20]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-tail"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                overrides={b0: _wire_bar(b0, 100.0, 102.0, 99.0, 101.0, 5.0)},
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_timeout_sec=5.0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_upsert = parquet_store.upsert_bars
        injected_during_tail = False
        live_feed_ref: list[LiveFeedService] = []

        async def yielding_upsert(*args: Any, **kwargs: Any) -> Any:
            nonlocal injected_during_tail
            res = await orig_upsert(*args, **kwargs)
            bars_arg = args[1] if len(args) > 1 else []
            # When tail tick closes b2 (14:02), inject a tick during the await!
            if (
                not injected_during_tail
                and any(getattr(b, "time", None) == b2 for b in bars_arg)
                and live_feed_ref
            ):
                injected_during_tail = True
                from app.topstep.signalr_hub import SignalRTickEvent

                live_feed_ref[0].handle_hub_event(
                    SignalRTickEvent(CID_MES, False, b3 + 10, 120.0, 3.0, "GatewayTrade", 2.0)
                )
            return res

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-tail")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m",))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            live_feed_ref.append(live_feed)
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            live_feed._transition_all_contracts("RECOVERING")
            # Hub connected before b2 opened so b2 is continuously observed and complete.
            live_feed._last_connected_utc = b1 + 30

            from app.topstep.signalr_hub import SignalRTickEvent

            # Inject tail ticks in b2 -> b3 while recon's 1m upsert (b0..b1) runs;
            # yielding_upsert will then inject b3+10 into the queue while b2 is being written.
            async def hook_upsert(*args: Any, **kwargs: Any) -> Any:
                bars_arg = args[1] if len(args) > 1 else []
                if any(getattr(b, "time", None) == b1 for b in bars_arg):
                    clock_ref[0] = b3 + 20
                    live_feed.handle_hub_event(
                        SignalRTickEvent(CID_MES, False, b2 + 5, 110.0, 2.0, "GatewayTrade", 1.0)
                    )
                    live_feed.handle_hub_event(
                        SignalRTickEvent(CID_MES, False, b3 + 5, 115.0, 2.0, "GatewayTrade", 1.5)
                    )
                return await yielding_upsert(*args, **kwargs)

            monkeypatch.setattr(parquet_store, "upsert_bars", hook_upsert)

            await live_feed.trigger_recovery(now_utc=b2 + 20)
            assert injected_during_tail is True
            assert live_feed.feed_state == "LIVE"
            assert live_feed._recovery_queue.empty()
            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            persisted = await parquet_store.read_bars(key_1m, start_ts=b0, end_ts=b3)
            assert [bar.time for bar in persisted] == [b0, b1, b2]
            wm_1m = await sqlite_store.get_watermark(key_1m)
            assert wm_1m is not None and wm_1m.last_closed_bar_time == b2
            open_b3 = agg.get_open_bar(CID_MES, False, "1m")
            assert open_b3 == OHLCVBar(b3, 115.0, 120.0, 115.0, 120.0, 5.0, is_closed=False)
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_quarantined_rest_backfill_and_persistence_error_keep_feed_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Quarantined REST bars freeze watermark & retry unhealed gap before LIVE.

    Defect caught: Advancing watermark past quarantined b1 (P1-004/SECINV-002) or getting stuck in
    RECOVERING when storage raises OSError (P1-007).
    """
    b0 = BASE_T
    b1 = BASE_T + 60
    bars_tail = [BASE_T + (i * 60) for i in range(2, 7)]  # b2..b6 (>2 bars past b1)
    clock_now = bars_tail[-1] + 70
    corrupt_mode = True
    requested_starts: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-corrupt"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            req_start = int(
                datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00")).timestamp()
            )
            requested_starts.append(req_start)
            if corrupt_mode:
                # b1 violates OHLC bounds while b2..b6 are valid and truncated/provider_gap=False
                bars = _bars_for_history_payload(
                    payload,
                    overrides={
                        b0: _wire_bar(b0, 100.0, 102.0, 99.0, 101.0, 5.0),
                        b1: _wire_bar(b1, 105.0, 90.0, 90.0, 100.0, 5.0),
                    },
                    open_p=101.0,
                    high_p=103.0,
                    low_p=100.0,
                    close_p=102.0,
                    vol=4.0,
                )
                return httpx.Response(200, json={"success": True, "bars": bars})
            healed = _bars_for_history_payload(
                payload,
                open_p=101.0,
                high_p=103.0,
                low_p=100.0,
                close_p=102.0,
                vol=4.0,
            )
            return httpx.Response(200, json={"success": True, "bars": healed})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_timeout_sec=5.0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
        await sqlite_store.upsert_watermark(key, b0)
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-corrupt")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)

            # 1) Quarantined b1 + valid b2..b6 -> DEGRADED and watermark MUST stay at b0!
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            wm_after_corrupt = await sqlite_store.get_watermark(key)
            assert wm_after_corrupt is not None and wm_after_corrupt.last_closed_bar_time == b0
            assert len(await sqlite_store.list_quality_issues(key)) == 1

            # 2) Persistence OSError -> DEGRADED (NOT stuck in RECOVERING), then heal b1..b6
            corrupt_mode = False
            orig_upsert = parquet_store.upsert_bars

            async def failing_upsert(*args: Any, **kwargs: Any) -> Any:
                raise OSError("simulated disk write failure")

            monkeypatch.setattr(parquet_store, "upsert_bars", failing_upsert)
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 2

            monkeypatch.setattr(parquet_store, "upsert_bars", orig_upsert)
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            # Retry MUST have requested start <= b1 to heal the unhealed gap!
            assert requested_starts[-1] <= b1
            wm_healed = await sqlite_store.get_watermark(key)
            assert wm_healed is not None and wm_healed.last_closed_bar_time == bars_tail[-1]
            persisted = await parquet_store.read_bars(key, start_ts=b0, end_ts=bars_tail[-1])
            assert [bar.time for bar in persisted] == [BASE_T + (i * 60) for i in range(0, 7)]
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_heartbeat_keepalive_and_key_rotation_watchdog_enforcement(
    tmp_path: Path,
) -> None:
    """Contract: Type=6 pings prevent false disconnects (P1-006); key rotation closes hub (P2-001).

    Defect caught: False timeout disconnects on quiet markets with active pings, or keeping hub LIVE
    after KEY_ROTATION_ENFORCE invalidates TopstepX auth.
    """
    clock_now = BASE_T + 180
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-watchdog"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                open_p=100,
                high_p=101,
                low_p=99,
                close_p=100,
                vol=5,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_heartbeat_timeout_sec=0.12,
        signalr_ping_interval_sec=0.04,
        key_rotation_days=30,
        key_rotation_enforce=False,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-watchdog")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                ping_interval_sec=0.04,
                heartbeat_timeout_sec=0.12,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert live_feed.feed_state == "LIVE"
            assert len(transports) == 1

            # Send only SignalR type=6 pings (zero ticks) past heartbeat_timeout_sec (0.12s).
            for _ in range(5):
                await transports[0].push_frame({"type": 6})
                await asyncio.sleep(0.04)
                await live_feed.run_watchdog_tick()

            assert live_feed.feed_state == "LIVE"
            assert hub.is_connected is True
            assert len(transports) == 1

            # Now enforce key rotation when key is expired -> watchdog must disconnect open hub!
            settings.key_rotation_enforce = True
            settings.topstepx_key_updated_at = "2020-01-01"
            await live_feed.run_watchdog_tick()

            assert hub.is_connected is False
            assert auth.is_connected is False
            assert live_feed.feed_state == "DISCONNECTED"
            assert live_feed.live_alerts_enabled is False
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_gateway_quote_cumulative_volume_and_symbol_id_preserve_contract_ohlcv(
    tmp_path: Path,
) -> None:
    """Contract: GatewayQuote ignores cumulative vol; symbolId never overrides callback contractId.

    Defect caught: SymbolId routing ticks away from contractId (P1-001) and GatewayQuote cumulative
    volume doubling on every quote snapshot (P1-002).
    """
    clock_now = BASE_T + 15
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-quote-vol"})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(200, json={"success": True, "bars": []})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-quote")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m",))
            broadcaster = WsBroadcaster(client_queue_maxsize=16, send_timeout_sec=1.0)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                broadcaster=broadcaster,
                signalr_hub=hub,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)

            # Send two GatewayQuote frames with cumulative volume=12000 and two GatewayTrade frames.
            await transports[0].push_frame(_market_quote_frame(CID_MES, BASE_T + 2, 5950.0, 12000))
            await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 4, 5951.0, 3.0))
            await transports[0].push_frame(_market_quote_frame(CID_MES, BASE_T + 6, 5952.0, 12003))
            await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 8, 5951.5, 2.0))
            await asyncio.sleep(0.03)

            open_bar = agg.get_open_bar(CID_MES, False, "1m")
            assert open_bar == OHLCVBar(
                time=BASE_T,
                open=5950.0,
                high=5952.0,
                low=5950.0,
                close=5951.5,
                volume=5.0,
                is_closed=False,
            )
            assert agg.get_open_bar("F.US.MES", False, "1m") is None
            await live_feed.stop()
            await broadcaster.close_all()
    finally:
        sqlite_store.close()


async def test_f03_watchdog_does_not_flush_recovering_open_bars_across_minute_boundary(
    tmp_path: Path,
) -> None:
    """Contract: Watchdog never flushes open bars for contracts in RECOVERING state.

    Defect caught: Watchdog finalizing pre-disconnect bar and discarding buffered recovery ticks
    for that bucket (P1-003).
    """
    clock_ref = [BASE_T + 10]
    backfill_entered = asyncio.Event()
    release_backfill = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-wd-recover"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            backfill_entered.set()
            await release_backfill.wait()
            bars = _bars_for_history_payload(
                payload,
                open_p=99.0,
                high_p=101.0,
                low_p=98.0,
                close_p=100.0,
                vol=2.0,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_timeout_sec=5.0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-wd-rec")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m",))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)

            # Seed pre-disconnect open bar in BASE_T
            from app.engine.bar_aggregator import NormalizedTick
            from app.topstep.signalr_hub import SignalRTickEvent

            agg.process_tick(
                NormalizedTick(CID_MES, False, BASE_T + 10, 100.0, 2.0),
                now_utc=BASE_T + 10,
            )
            rec_task = asyncio.create_task(live_feed.trigger_recovery())
            await asyncio.wait_for(backfill_entered.wait(), timeout=2.0)

            # Buffer tick in BASE_T while RECOVERING, advance clock past BASE_T+60, run watchdog.
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES,
                    False,
                    BASE_T + 45,
                    120.0,
                    3.0,
                    "GatewayTrade",
                    time.monotonic(),
                )
            )
            clock_ref[0] = BASE_T + 65
            await live_feed.run_watchdog_tick()

            # Watchdog must NOT have closed BASE_T prematurely without buffered tick!
            assert agg.get_last_closed_time(CID_MES, False, "1m") is None
            release_backfill.set()
            await rec_task

            # Merged provisional open bar retains pre-disconnect + buffered OHLCV while open,
            # and once clock passes BASE_T+60, watchdog refuses to finalize interrupted BASE_T
            # without REST confirmation (PASS2-001).
            assert agg.get_open_bar(CID_MES, False, "1m") == OHLCVBar(
                BASE_T, 100.0, 120.0, 100.0, 120.0, 5.0, is_closed=False
            )
            await live_feed.run_watchdog_tick()
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            assert await parquet_store.read_bars(key, start_ts=BASE_T) == []
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False

            # Now confirm closed BASE_T via REST recovery -> persists authoritative bar and LIVE!
            await live_feed.trigger_recovery(now_utc=BASE_T + 65)
            assert live_feed.feed_state == "LIVE"
            persisted = await parquet_store.read_bars(key, start_ts=BASE_T)
            assert len(persisted) == 1
            assert persisted[0] == OHLCVBar(BASE_T, 99.0, 101.0, 98.0, 100.0, 2.0, is_closed=True)
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_live_and_watchdog_persistence_failures_degrade_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Storage failures during LIVE bar close or background watchdog degrade cleanly.

    Defect caught: Unhandled live task/watchdog exceptions leaving feed LIVE and watchdog dead
    (P1-007, SECINV-003).
    """
    clock_ref = [BASE_T + 10]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-live-err"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                open_p=100,
                high_p=101,
                low_p=99,
                close_p=100,
                vol=2,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-live-err")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()
            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert live_feed.feed_state == "LIVE"

            from app.topstep.signalr_hub import SignalRTickEvent

            # 1) Fault on real LIVE tick bar-close persistence -> DEGRADED, gap recorded, alerts off
            orig_upsert = parquet_store.upsert_bars
            upsert_calls: list[tuple[StorageDatasetKey, list[OHLCVBar]]] = []

            async def failing_upsert(
                key_arg: StorageDatasetKey,
                bars_arg: list[OHLCVBar],
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                upsert_calls.append((key_arg, list(bars_arg)))
                raise OSError("disk full on live persist")

            monkeypatch.setattr(parquet_store, "upsert_bars", failing_upsert)
            clock_ref[0] = BASE_T + 65
            live_feed.handle_hub_event(
                SignalRTickEvent(CID_MES, False, BASE_T + 65, 105.0, 2.0, "GatewayTrade", 1.0)
            )
            clock_ref[0] = BASE_T + 125
            live_feed.handle_hub_event(
                SignalRTickEvent(CID_MES, False, BASE_T + 125, 106.0, 3.0, "GatewayTrade", 2.0)
            )
            await live_feed.flush_pending_tasks()
            assert any(
                bar.time == BASE_T + 60 for _k, bars_list in upsert_calls for bar in bars_list
            )
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 1
            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            wm_after_fault = await sqlite_store.get_watermark(key_1m)
            assert wm_after_fault is not None and wm_after_fault.last_closed_bar_time < BASE_T + 60

            # 2) Restore storage, recover to LIVE, then fault inside background _watchdog_loop.
            upsert_calls.clear()
            monkeypatch.setattr(parquet_store, "upsert_bars", orig_upsert)
            await live_feed.trigger_recovery(now_utc=clock_ref[0])
            assert live_feed.feed_state == "LIVE"

            monkeypatch.setattr(parquet_store, "upsert_bars", failing_upsert)
            clock_ref[0] = BASE_T + 190  # closes BASE_T + 120 bar for watchdog flush
            await asyncio.sleep(0.65)

            assert any(
                bar.time == BASE_T + 120 for _k, bars_list in upsert_calls for bar in bars_list
            )
            assert live_feed._watchdog_task is not None and not live_feed._watchdog_task.done()
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_on_auth_connected_live_mode_switch_reconnects_and_recovers(
    tmp_path: Path,
) -> None:
    """Contract: Re-authing with a different live_mode while connected switches hub & resets state.

    Defect caught: Sim/Live mismatch when /api/connect switches mode on an already-connected hub
    (P1-008, SECINV-004).
    """
    clock_now = BASE_T + 65
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-mode-switch"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                open_p=100.0,
                high_p=102.0,
                low_p=99.0,
                close_p=101.0,
                vol=4.0,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_reconnect_initial_delay_sec=0.02,
        signalr_reconnect_max_delay_sec=0.05,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-sim", live=False)
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.02,
                reconnect_max_delay_sec=0.05,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert hub.live_mode is False
            assert live_feed.get_status_snapshot()["contracts"][CID_MES]["live_mode"] is False

            # Switch to live=True without disconnecting first!
            await auth.login("alice", "key-live", live=True)
            await live_feed.on_auth_connected()

            for _ in range(100):
                if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)

            assert len(transports) >= 2
            assert hub.live_mode is True
            assert live_feed.get_status_snapshot()["contracts"][CID_MES]["live_mode"] is True
            live_wm = await sqlite_store.get_watermark(
                StorageDatasetKey("topstepx", True, CID_MES, "1m")
            )
            assert live_wm is not None and live_wm.last_closed_bar_time == BASE_T
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_cold_start_htf_alignment_and_watermark_correction(tmp_path: Path) -> None:
    """Contract: Cold start aligns 1m backfill to HTF bucket boundary and allows HTF correction.

    Defect caught: Cold start fetching 10m and finalizing partial 1h bar (P1-005, SECINV-001)
    or failing to overwrite corrected HTF bar <= watermark (P1-006).
    """
    h0 = BASE_T  # 14:00:00Z
    clock_now = h0 + 3600 + 30  # 15:00:30Z (14:00 1h bucket has closed)
    rest_starts: list[int] = []
    mode = "partial"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-htf-cold"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            req_start = int(
                datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00")).timestamp()
            )
            rest_starts.append(req_start)
            if mode == "partial":
                # Simulate upstream returning only 14:50..14:51 despite aligned request start
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bars": [
                            _wire_bar(h0 + 50 * 60, 100.0, 105.0, 99.0, 102.0, 11.0),
                            _wire_bar(h0 + 51 * 60, 102.0, 106.0, 101.0, 103.0, 11.0),
                        ],
                    },
                )
            high_peak = 6099.0 if mode == "corrected" else 110.0
            full_bars = _bars_for_history_payload(
                payload,
                overrides={
                    h0 + 15 * 60: _wire_bar(
                        h0 + 15 * 60,
                        100.0,
                        high_peak,
                        95.0,
                        102.0,
                        2.0,
                    )
                },
                open_p=100.0,
                high_p=105.0,
                low_p=95.0,
                close_p=102.0,
                vol=2.0,
            )
            return httpx.Response(200, json={"success": True, "bars": full_bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-htf")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m", "15m", "1h"))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)

            # 1) Cold start aligns REST request start back to <= h0 (14:00), and partial response
            #    must keep feed DEGRADED, keep alerts disabled, and NOT finalize 1h bar!
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert rest_starts[0] <= h0
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 1
            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            assert await sqlite_store.is_interval_complete(key_1m, h0, h0 + 3600) is False
            key_1h = StorageDatasetKey("topstepx", False, CID_MES, "1h")
            assert await parquet_store.read_bars(key_1h) == []
            assert await sqlite_store.get_watermark(key_1h) is None

            # 2) Full 60 bars without touching watermark -> automatic self-heal to LIVE & 1h closes!
            mode = "full"
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            assert await sqlite_store.is_interval_complete(key_1m, h0, h0 + 3600) is True
            bars_1h = await parquet_store.read_bars(key_1h)
            assert len(bars_1h) == 1 and bars_1h[0].high == 110.0

            # 3) Historical correction on 1m at 14:15 (outside 1m overlap, inside 1h overlap) ->
            #    overlapping REST request pulls [wm_1h - 2h, now] and overwrites 1h Parquet!
            mode = "corrected"
            await live_feed.trigger_recovery(now_utc=clock_now)
            assert rest_starts[-1] <= h0 - 7200
            bars_1h_corrected = await parquet_store.read_bars(key_1h, start_ts=h0, end_ts=h0)
            assert len(bars_1h_corrected) == 1
            assert bars_1h_corrected[0].high == 6099.0
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_token_provider_redaction_without_extra_secrets_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract: SignalR reconnect logger redacts token_provider JWT without extra_secrets_provider.

    Defect caught: Standalone token_provider JWT leaking raw in reconnect warning logs when
    extra_secrets_provider and auth_service are omitted (P2-004).
    """
    secret_jwt = "eyJzdWIiOiJzdGFuZGFsb25lIn0.jwt-token-provider-secret.sig999"
    attempts = 0
    events: list[Any] = []

    async def token_provider() -> str:
        return secret_jwt

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        nonlocal attempts
        attempts += 1
        raise ConnectionError(f"handshake failed for bearer={secret_jwt}")

    settings = Settings(
        signalr_reconnect_initial_delay_sec=0.01,
        signalr_reconnect_max_delay_sec=0.02,
    )
    hub = SignalRMarketHub(
        settings,
        token_provider=token_provider,
        auth_service=None,
        extra_secrets_provider=None,
        ws_factory=ws_factory,
        on_event=events.append,
        reconnect_initial_delay_sec=0.01,
        reconnect_max_delay_sec=0.02,
    )

    with caplog.at_level(logging.WARNING, logger="topstep_chart.signalr"):
        task = hub.start()
        for _ in range(100):
            if attempts >= 2:
                break
            await asyncio.sleep(0.01)
        await hub.stop()
        assert task.done()

    assert attempts >= 2
    assert any(hasattr(e, "message") for e in events)
    assert all(secret_jwt not in e.message for e in events if hasattr(e, "message"))
    assert caplog.records
    assert all(secret_jwt not in rec.getMessage() for rec in caplog.records)
    assert any("***REDACTED***" in rec.getMessage() for rec in caplog.records)


async def test_f03_htf_persistence_failure_self_heals_on_recovery_to_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Higher-timeframe storage failures record HTF gap and self-heal on recovery.

    Defect caught: unhealed_gap_starts for 5m never popped by _recover_higher_timeframes_from_1m,
    leaving feed permanently stuck in DEGRADED after storage recovers (PASS1-003).
    """
    b0 = BASE_T  # 14:00:00Z (start of 5m bucket [14:00, 14:05))
    clock_ref = [b0 + 4 * 60 + 30]  # 14:04:30Z

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-htf-heal"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload, open_p=100.0, high_p=105.0, low_p=99.0, close_p=102.0, vol=3.0
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_upsert = parquet_store.upsert_bars
        fail_5m = True

        async def selective_upsert(key: StorageDatasetKey, *args: Any, **kwargs: Any) -> Any:
            if fail_5m and key.timeframe == "5m":
                raise OSError("transient 5m disk fault")
            return await orig_upsert(key, *args, **kwargs)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-htf-heal")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m", "5m"))
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                signalr_hub=hub,
                recovery_timeframes=("1m", "5m"),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()
            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert live_feed.feed_state == "LIVE"

            # Fault 5m write on LIVE close of continuously observed [14:05, 14:10) bar!
            from app.topstep.signalr_hub import SignalRTickEvent

            monkeypatch.setattr(parquet_store, "upsert_bars", selective_upsert)
            clock_ref[0] = b0 + 5 * 60 + 10
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 5 * 60 + 10, 108.0, 2.0, "GatewayTrade", 1.0
                )
            )
            clock_ref[0] = b0 + 10 * 60 + 5
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 10 * 60 + 5, 111.0, 2.0, "GatewayTrade", 2.0
                )
            )
            await live_feed.flush_pending_tasks()
            assert live_feed.feed_state == "DEGRADED"
            assert "5m" in live_feed._contracts[CID_MES].unhealed_gap_starts

            # Restore storage and recover WITHOUT forcing watermarks -> 5m gap clears to LIVE!
            fail_5m = False
            await live_feed.trigger_recovery(now_utc=clock_ref[0])
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            assert live_feed._contracts[CID_MES].unhealed_gap_starts == {}
            key_5m = StorageDatasetKey("topstepx", False, CID_MES, "5m")
            bars_5m = await parquet_store.read_bars(key_5m, start_ts=b0, end_ts=b0 + 600)
            assert any(bar.time == b0 + 300 for bar in bars_5m)
            wm_5m = await sqlite_store.get_watermark(key_5m)
            assert wm_5m is not None and wm_5m.last_closed_bar_time == b0 + 300
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_inflight_live_persistence_task_cannot_overwrite_rest_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Stalled in-flight LIVE persistence tasks cannot overwrite authoritative REST data.

    Defect caught: Old _live_tick_tasks finishing after recovery and overwriting REST high=200.0
    with stale pre-disconnect high=100.0 + emitting stale LIVE tag=None signal (PASS1-001).
    """
    b0 = BASE_T  # 14:00:00Z
    clock_ref = [b0 + 3 * 60 + 30]  # 14:03:30Z so 14:04 opens continuously after connect
    signals: list[tuple[str, int, str | None, float]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-gen"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                overrides={
                    b0 + 2 * 60: _wire_bar(b0 + 2 * 60, 100.0, 200.0, 95.0, 150.0, 10.0)
                },
                open_p=100.0,
                high_p=105.0,
                low_p=95.0,
                close_p=102.0,
                vol=4.0,
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_upsert_wm = sqlite_store.upsert_watermark
        wm_pause_entered = asyncio.Event()
        release_old_wm = asyncio.Event()
        paused_once = False

        async def gated_upsert_watermark(
            key: StorageDatasetKey, bar_time: int, *, force: bool = False
        ) -> Any:
            nonlocal paused_once
            if not paused_once and key.timeframe == "1m" and bar_time == b0 + 4 * 60:
                paused_once = True
                wm_pause_entered.set()
                await release_old_wm.wait()
            return await orig_upsert_wm(key, bar_time, force=force)

        monkeypatch.setattr(sqlite_store, "upsert_watermark", gated_upsert_watermark)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-gen")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m", "5m"))

            def on_signal(evt: Any, tag: str | None) -> None:
                signals.append((evt.timeframe, evt.bar.time, tag, evt.bar.high))

            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                recovery_timeframes=("1m", "5m"),
                on_bar_signal=on_signal,
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.trigger_recovery(now_utc=clock_ref[0])
            assert live_feed.feed_state == "LIVE"

            # Emit LIVE ticks in 14:04 (complete) -> 14:05 -> pauses at 1m wm.
            from app.topstep.signalr_hub import SignalRTickEvent

            clock_ref[0] = b0 + 4 * 60 + 55
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 4 * 60 + 55, 100.0, 2.0, "GatewayTrade", 1.0
                )
            )
            clock_ref[0] = b0 + 5 * 60 + 5
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 5 * 60 + 5, 101.0, 2.0, "GatewayTrade", 2.0
                )
            )
            await asyncio.wait_for(wm_pause_entered.wait(), timeout=2.0)

            # Simulate disconnect + recovery while old live task is still in-flight!
            from app.topstep.signalr_hub import SignalRDisconnectedEvent

            live_feed.handle_hub_event(SignalRDisconnectedEvent("drop", True, 1.0))
            rec_task = asyncio.create_task(live_feed.trigger_recovery(now_utc=clock_ref[0]))
            await asyncio.sleep(0.02)
            release_old_wm.set()
            await rec_task

            assert live_feed.feed_state == "LIVE"
            key_5m = StorageDatasetKey("topstepx", False, CID_MES, "5m")
            bars_5m = await parquet_store.read_bars(key_5m, start_ts=b0, end_ts=b0 + 300)
            b0_5m = [b for b in bars_5m if b.time == b0]
            assert len(b0_5m) == 1 and b0_5m[0].high == 200.0
            assert not any(
                tf == "5m" and t == b0 and tag is None and high < 200.0
                for tf, t, tag, high in signals
            )
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_incomplete_rest_with_buffered_tick_stays_degraded_without_closing_bar(
    tmp_path: Path,
) -> None:
    """Contract: Missing REST minute stays DEGRADED even when buffered post-reconnect tick exists.

    Defect caught: Buffered tick closing missing 14:01 minute as complete history (COR-001) and
    finalizing interrupted mid-candle bar without REST confirmation (PASS2-001).
    """
    b1 = BASE_T + 60  # 14:01 (missing from REST)
    b2 = BASE_T + 120  # 14:02
    clock_now = b2 + 5  # 14:02:05
    signals: list[tuple[str, int, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-cor001"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(payload)
            # Omit b1 (14:01) from REST while keeping earlier bars up through b0.
            bars = [b for b in bars if b["t"] < b1]
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-cor001")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                on_bar_signal=lambda evt, tag: signals.append((evt.timeframe, evt.bar.time, tag)),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            live_feed._transition_all_contracts("RECOVERING")

            from app.topstep.signalr_hub import SignalRTickEvent

            live_feed.handle_hub_event(
                SignalRTickEvent(CID_MES, False, b1 + 50, 105.0, 2.0, "GatewayTrade", 1.0)
            )
            await live_feed.trigger_recovery(now_utc=clock_now)

            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 1
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            persisted_b1 = await parquet_store.read_bars(key, start_ts=b1, end_ts=b1)
            assert persisted_b1 == []
            wm = await sqlite_store.get_watermark(key)
            assert wm is None
            assert not any(t == b1 and tag is None for _, t, tag in signals)
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_restart_after_persistence_gap_preserves_pre_gap_watermark_and_heals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Persistence gap freezes SQLite watermark so restart re-fetches and heals gap.

    Defect caught: Watermark advancing >2 bars past an unhealed in-memory gap during DEGRADED,
    skipping the missing bar upon coordinator restart (PASS2-002).
    """
    b0 = BASE_T
    gap_t = b0 + 60  # 14:01 will fail storage write
    later_bars = [b0 + (i * 60) for i in range(2, 7)]  # 14:02..14:06 (>2 bars past gap_t)
    clock_ref = [b0 + 65]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-pass2-002"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                overrides={gap_t: _wire_bar(gap_t, 500.0, 505.0, 499.0, 502.0, 99.0)},
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_upsert = parquet_store.upsert_bars
        fail_gap_t = True

        async def fail_once_upsert(key: StorageDatasetKey, bars: Any, **kwargs: Any) -> Any:
            if fail_gap_t and any(getattr(b, "time", None) == gap_t for b in bars):
                raise OSError("transient failure on gap_t")
            return await orig_upsert(key, bars, **kwargs)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-restart")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.trigger_recovery(now_utc=clock_ref[0])
            assert live_feed.feed_state == "LIVE"
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            wm0 = await sqlite_store.get_watermark(key)
            assert wm0 is not None and wm0.last_closed_bar_time == b0

            from app.topstep.signalr_hub import SignalRTickEvent

            monkeypatch.setattr(parquet_store, "upsert_bars", fail_once_upsert)
            # Fail on gap_t close, then continue closing later bars 14:02..14:06 while DEGRADED.
            live_feed.handle_hub_event(
                SignalRTickEvent(CID_MES, False, gap_t + 10, 100.0, 1.0, "GatewayTrade", 1.0)
            )
            for bt in later_bars:
                clock_ref[0] = bt + 10
                live_feed.handle_hub_event(
                    SignalRTickEvent(CID_MES, False, bt + 10, 101.0, 1.0, "GatewayTrade", 2.0)
                )
            clock_ref[0] = later_bars[-1] + 65
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, later_bars[-1] + 65, 102.0, 1.0, "GatewayTrade", 3.0
                )
            )
            await live_feed.flush_pending_tasks()
            assert live_feed.feed_state == "DEGRADED"
            # Watermark MUST still be frozen at b0 (< gap_t), NOT advanced to 14:06!
            wm_frozen = await sqlite_store.get_watermark(key)
            assert wm_frozen is not None and wm_frozen.last_closed_bar_time == b0
            await live_feed.stop()

            # Simulate process restart with a brand-new LiveFeedService using persisted storage!
            monkeypatch.setattr(parquet_store, "upsert_bars", orig_upsert)
            fail_gap_t = False
            restarted_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await restarted_feed.set_active_contracts([CID_MES], live_mode=False)
            await restarted_feed.trigger_recovery(now_utc=clock_ref[0])
            assert restarted_feed.feed_state == "LIVE"
            healed = await parquet_store.read_bars(key, start_ts=gap_t, end_ts=gap_t)
            assert len(healed) == 1 and healed[0].high == 505.0
            await restarted_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_subscribe_completion_error_prevents_live_and_triggers_reconnect(
    tmp_path: Path,
) -> None:
    """Contract: SignalR type=3 subscribe completion error prevents LIVE and triggers reconnect.

    Defect caught: Emitting ConnectedEvent before subscribe completion so feed enters LIVE
    before or despite upstream subscription rejection (COR-003 / PASS2-002 / P1-NEW-004).
    """
    clock_now = BASE_T + 120
    transports: list[_MockSignalRTransport] = []
    call_idx = 0
    observed_while_first_conn: list[tuple[str, bool]] = []
    first_conn_rejected = asyncio.Event()

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        nonlocal call_idx
        call_idx += 1
        err = "Subscription unauthorized: jwt-sub-secret" if call_idx == 1 else None
        if call_idx >= 2 and not first_conn_rejected.is_set():
            first_conn_rejected.set()
        t = _MockSignalRTransport(subscribe_error=err)
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-sub-secret"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_reconnect_initial_delay_sec=0.05,
        signalr_reconnect_max_delay_sec=0.08,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-sub-err")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.05,
                reconnect_max_delay_sec=0.08,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(150):
                if len(transports) == 1:
                    observed_while_first_conn.append(
                        (live_feed.feed_state, live_feed.live_alerts_enabled)
                    )
                if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.005)

            assert len(transports) >= 2
            assert transports[0].closed is True
            assert observed_while_first_conn
            assert all(
                state != "LIVE" and alerts is False
                for state, alerts in observed_while_first_conn
            )
            assert live_feed.feed_state == "LIVE"
            assert live_feed.live_alerts_enabled is True
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_stalled_subscribe_send_hits_deadline_and_reconnects() -> None:
    """Contract: Stalled ws.send during subscribe hits bounded deadline and triggers reconnect.

    Defect caught: _send_raw waiting indefinitely on _send_lock/ws.send before heartbeat starts
    (PASS2-004).
    """
    attempts = 0
    transports: list[_MockSignalRTransport] = []
    events: list[Any] = []

    async def token_provider() -> str:
        return "jwt-stall-send"

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        nonlocal attempts
        attempts += 1
        t = _MockSignalRTransport(stall_subscribe_send=(attempts == 1))
        transports.append(t)
        return t

    settings = Settings(
        upstream_connect_timeout_sec=0.03,
        signalr_heartbeat_timeout_sec=0.05,
        signalr_reconnect_initial_delay_sec=0.01,
        signalr_reconnect_max_delay_sec=0.02,
    )
    hub = SignalRMarketHub(
        settings,
        token_provider=token_provider,
        ws_factory=ws_factory,
        contracts=[CID_MES],
        on_event=events.append,
        connect_timeout_sec=0.03,
        heartbeat_timeout_sec=0.05,
        reconnect_initial_delay_sec=0.01,
        reconnect_max_delay_sec=0.02,
    )
    task = hub.start()
    for _ in range(100):
        if hub.is_connected and attempts >= 2:
            break
        await asyncio.sleep(0.01)

    assert attempts >= 2
    assert hub.is_connected is True
    assert any(hasattr(e, "message") and "deadline" in e.message for e in events)
    await hub.stop()
    assert task.done()


async def test_f03_mid_candle_recovery_empty_queue_marks_next_tick_bucket_incomplete(
    tmp_path: Path,
) -> None:
    """Contract: Mid-candle recovery with empty tick queue keeps that bucket incomplete until REST.

    Defect caught: Clearing incomplete flag when rebuilt_open is None so the first live tick after
    recovery starts a bar that closes without pre-connect trades (PASS2-001 / P1-NEW-001).
    """
    b_prev = BASE_T - 60  # 13:59
    b0 = BASE_T  # 14:00 (connected mid-candle at 14:00:30 with no buffered ticks)
    b1 = BASE_T + 60  # 14:01
    clock_ref = [b0 + 30]
    signals: list[tuple[str, int, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-empty-buf"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                overrides={b0: _wire_bar(b0, 99.0, 106.0, 98.0, 104.0, 40.0)},
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-empty-buf")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m",))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                recovery_timeframes=("1m",),
                on_bar_signal=lambda evt, tag: signals.append((evt.timeframe, evt.bar.time, tag)),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.trigger_recovery(now_utc=b0 + 30)
            assert agg.get_open_bar(CID_MES, False, "1m") is None
            assert live_feed.feed_state == "LIVE"

            from app.topstep.signalr_hub import SignalRTickEvent

            # First tick arrives at 14:00:40, then next minute arrives at 14:01:01.
            clock_ref[0] = b0 + 40
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 40, 100.0, 2.0, "GatewayTrade", time.monotonic()
                )
            )
            clock_ref[0] = b1 + 1
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b1 + 1, 102.0, 3.0, "GatewayTrade", time.monotonic()
                )
            )
            await live_feed.flush_pending_tasks()

            # 14:00 bar must NOT be persisted as closed, watermark stays at 13:59, no LIVE signal!
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            assert await parquet_store.read_bars(key, start_ts=b0, end_ts=b0) == []
            wm = await sqlite_store.get_watermark(key)
            assert wm is not None and wm.last_closed_bar_time == b_prev
            assert not any(t == b0 and tag is None for _, t, tag in signals)
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False

            # REST confirmation heals authoritative 14:00 bar (vol=40.0) cleanly.
            await live_feed.trigger_recovery(now_utc=clock_ref[0])
            assert live_feed.feed_state == "LIVE"
            healed = await parquet_store.read_bars(key, start_ts=b0, end_ts=b0)
            assert len(healed) == 1 and healed[0].volume == 40.0
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_post_window_discarded_tick_bar_retains_gap_and_degraded(
    tmp_path: Path,
) -> None:
    """Contract: Minute closing during recovery past initial eff_now is never silently dropped.

    Defect caught: Clearing discarded incomplete marker when closed_end was computed at eff_now
    before clock crossed minute boundary (P1-NEW-002).
    """
    b0 = BASE_T  # 14:00
    b1 = BASE_T + 60  # 14:01
    clock_ref = [b0 + 50]
    backfill_entered = asyncio.Event()
    release_backfill = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-p1-002"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            if not backfill_entered.is_set():
                backfill_entered.set()
                await release_backfill.wait()
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-p1-002")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            rec_task = asyncio.create_task(live_feed.trigger_recovery())
            await asyncio.wait_for(backfill_entered.wait(), timeout=2.0)

            from app.topstep.signalr_hub import SignalRTickEvent

            clock_ref[0] = b1 + 10
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b0 + 55, 100.0, 2.0, "GatewayTrade", time.monotonic()
                )
            )
            live_feed.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, b1 + 5, 101.0, 3.0, "GatewayTrade", time.monotonic()
                )
            )
            release_backfill.set()
            await rec_task

            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            assert live_feed._contracts[CID_MES].unhealed_gap_starts["1m"] == b0

            # Follow-up recovery confirms b0 from REST and reaches LIVE!
            await live_feed.trigger_recovery(now_utc=b1 + 10)
            assert live_feed.feed_state == "LIVE"
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            assert len(await parquet_store.read_bars(key, start_ts=b0, end_ts=b0)) == 1
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_update_contracts_during_initial_subscribe_syncs_all_contracts() -> None:
    """Contract: Adding a contract while initial subscribe is in-flight still subscribes it first.

    Defect caught: update_contracts() updating self._contracts during stalled initial subscribe of
    contract A and ConnectedEvent reporting B before B was subscribed (P1-NEW-003).
    """
    cid_a = CID_MES
    cid_b = "CON.F.US.MNQ.Z26"
    first_sub_blocked = asyncio.Event()
    release_first_sub = asyncio.Event()
    events: list[Any] = []

    class _GateSubscribeTransport(_MockSignalRTransport):
        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                if frame.get("target") == "SubscribeContractQuotes" and frame.get("arguments") == [
                    cid_a
                ]:
                    first_sub_blocked.set()
                    await release_first_sub.wait()
                await super().send(chunk + SIGNALR_RECORD_SEPARATOR)

    transport = _GateSubscribeTransport()
    hub = SignalRMarketHub(
        Settings(signalr_reconnect_initial_delay_sec=0.02),
        token_provider=lambda: asyncio.sleep(0, result="jwt-sync"),
        ws_factory=lambda _u, _h: asyncio.sleep(0, result=transport),
        contracts=[cid_a],
        on_event=events.append,
    )
    task = hub.start()
    await asyncio.wait_for(first_sub_blocked.wait(), timeout=2.0)

    # Add cid_b concurrently while initial subscribe for cid_a is still blocked in ws.send!
    await hub.update_contracts([cid_a, cid_b])
    assert hub.is_connected is False
    release_first_sub.set()

    for _ in range(100):
        if hub.is_connected:
            break
        await asyncio.sleep(0.01)

    assert hub.is_connected is True
    sub_targets = {
        (f.get("target"), tuple(f.get("arguments", [])))
        for f in transport.sent_frames
        if f.get("type") == 1
    }
    assert ("SubscribeContractQuotes", (cid_a,)) in sub_targets
    assert ("SubscribeContractTrades", (cid_a,)) in sub_targets
    assert ("SubscribeContractQuotes", (cid_b,)) in sub_targets
    assert ("SubscribeContractTrades", (cid_b,)) in sub_targets
    connected_evts = [e for e in events if hasattr(e, "contracts")]
    assert connected_evts and set(connected_evts[-1].contracts) == {cid_a, cid_b}
    await hub.stop()
    assert task.done()


async def test_f03_missing_subscribe_completion_with_pings_times_out_without_live(
    tmp_path: Path,
) -> None:
    """Contract: Missing subscribe type=3 ack times out even when pings arrive, preventing LIVE.

    Defect caught: Emitting ConnectedEvent before type=3 completion while server sends type=6
    keepalive pings, causing unbounded false LIVE (PASS2-002 / P1-NEW-004).
    """
    clock_now = BASE_T + 120
    transports: list[_MockSignalRTransport] = []
    call_idx = 0
    observed_attempt1: list[tuple[str, bool]] = []

    class _NoAckWithPingsTransport(_MockSignalRTransport):
        def __init__(self, *, omit_acks: bool) -> None:
            super().__init__()
            self._omit_acks = omit_acks
            self._ping_task: asyncio.Task[None] | None = None

        async def send(self, data: str) -> None:
            if not self._omit_acks:
                await super().send(data)
                return
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if chunk.strip():
                    frame = json.loads(chunk)
                    self.sent_frames.append(frame)
                    if frame.get("protocol") == "json":
                        await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                        self.handshake_done.set()
                        self._ping_task = asyncio.create_task(self._emit_pings())

        async def _emit_pings(self) -> None:
            while not self.closed:
                await self.incoming.put('{"type":6}' + SIGNALR_RECORD_SEPARATOR)
                await asyncio.sleep(0.01)

        async def close(self) -> None:
            await super().close()
            if self._ping_task is not None:
                self._ping_task.cancel()

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        nonlocal call_idx
        call_idx += 1
        t = _NoAckWithPingsTransport(omit_acks=(call_idx == 1))
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-no-ack"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        upstream_connect_timeout_sec=0.04,
        signalr_heartbeat_timeout_sec=0.2,
        signalr_reconnect_initial_delay_sec=0.02,
        signalr_reconnect_max_delay_sec=0.04,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-no-ack")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                connect_timeout_sec=0.04,
                heartbeat_timeout_sec=0.2,
                reconnect_initial_delay_sec=0.02,
                reconnect_max_delay_sec=0.04,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()

            for _ in range(150):
                if len(transports) == 1:
                    observed_attempt1.append((live_feed.feed_state, live_feed.live_alerts_enabled))
                if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.005)

            assert len(transports) >= 2
            assert transports[0].closed is True
            assert observed_attempt1
            assert all(st != "LIVE" and al is False for st, al in observed_attempt1)
            assert live_feed.feed_state == "LIVE"
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_recovery_buffer_enforces_maxsize_across_drained_pending_buckets(
    tmp_path: Path,
) -> None:
    """Contract: Bounded recovery cap covers both _recovery_queue and drained pending_by_contract.

    Defect caught: Accumulating >maxsize ticks for contract C across drains of A and B without
    counting overflow (P1-NEW-005).
    """
    cid_a = CID_MES
    cid_b = "CON.F.US.MNQ.Z26"
    cid_c = "CON.F.US.ES.Z26"
    clock_now = BASE_T + 120
    gate_a = asyncio.Event()
    release_a = asyncio.Event()
    gate_b = asyncio.Event()
    release_b = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-cap"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            if payload["contractId"] == cid_a:
                gate_a.set()
                await release_a.wait()
            elif payload["contractId"] == cid_b:
                gate_b.set()
                await release_b.wait()
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_queue_maxsize=4)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-cap")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts([cid_a, cid_b, cid_c], live_mode=False)
            rec_task = asyncio.create_task(live_feed.trigger_recovery(now_utc=clock_now))

            from app.topstep.signalr_hub import SignalRTickEvent

            # Batch 1 (< maxsize=4): 3 ticks for cid_c during cid_a REST -> drained into pending!
            await asyncio.wait_for(gate_a.wait(), timeout=2.0)
            for idx in range(3):
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        cid_c, False, clock_now + idx, 5900.0 + idx, 1.0, "GatewayTrade", float(idx)
                    )
                )
            release_a.set()

            # Batch 2 (< maxsize=4): 3 more ticks for cid_c during cid_b REST -> total 6 > 4!
            await asyncio.wait_for(gate_b.wait(), timeout=2.0)
            for idx in range(3, 6):
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        cid_c, False, clock_now + idx, 5900.0 + idx, 1.0, "GatewayTrade", float(idx)
                    )
                )
            release_b.set()
            await rec_task

            assert live_feed.queue_overflow_count >= 2
            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.live_alerts_enabled is False
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_malformed_signalr_tick_quarantined_and_blocks_incomplete_bar(
    tmp_path: Path,
) -> None:
    """Contract: Malformed live SignalR trade is quarantined in SQLite and blocks bar completion.

    Defect caught: Silently dropping invalid GatewayTrade (e.g. negative volume) without recording
    quality issue or marking the affected bar incomplete (PASS2-003 / R04).
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-malformed"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-malformed")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await live_feed.set_active_contracts([CID_MES], live_mode=False)
            await live_feed.start()
            for _ in range(100):
                if live_feed.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert live_feed.feed_state == "LIVE"

            # Valid trade in BASE_T, then malformed GatewayTrade (negative volume) in BASE_T!
            await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 10, 5950.0, 2.0))
            await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 25, 5952.0, -5.0))
            await asyncio.sleep(0.03)
            await live_feed.flush_pending_tasks()

            assert live_feed.feed_state == "DEGRADED"
            assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 1
            key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            issues = await sqlite_store.list_quality_issues(key)
            assert len(issues) == 1
            assert issues[0]["issue_code"] == "negative_volume"

            # Even if another tick arrives in BASE_T+60, affected BASE_T bar cannot finalize closed!
            clock_ref[0] = BASE_T + 65
            await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 65, 5955.0, 3.0))
            await asyncio.sleep(0.03)
            await live_feed.flush_pending_tasks()
            assert await parquet_store.read_bars(key, start_ts=BASE_T, end_ts=BASE_T) == []
            wm = await sqlite_store.get_watermark(key)
            assert wm is not None and wm.last_closed_bar_time < BASE_T
            await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_retry_within_same_minute_keeps_malformed_bar_degraded_until_close(
    tmp_path: Path,
) -> None:
    """Contract: Recovery retry within the same open minute keeps unhealed malformed gap DEGRADED.

    Defect caught: Lowering unhealed_gap_starts['1m'] to an earlier backfill_start during a
    same-minute retry, falsely clearing the active minute's gap and finalizing incomplete bars.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-retry"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            overrides = {BASE_T: _wire_bar(BASE_T, 5950.0, 5954.0, 5949.0, 5953.0, 12.0)}
            bars = _bars_for_history_payload(payload, overrides=overrides)
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-retry")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Valid trade then malformed trade inside still-open BASE_T minute.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 10, 5950.0, 2.0))
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 25, 5952.0, -5.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"

                # Trigger recovery retry WHILE still inside BASE_T minute (clock = BASE_T + 40).
                clock_ref[0] = BASE_T + 40
                await live_feed.trigger_recovery()
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False

                # Even if a tick in BASE_T+60 arrives before heal, BASE_T cannot finalize.
                clock_ref[0] = BASE_T + 62
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 62, 5955.0, 1.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert await parquet_store.read_bars(key, start_ts=BASE_T, end_ts=BASE_T) == []

                # Once BASE_T is closed and REST confirms BASE_T bar, recovery heals to LIVE.
                await live_feed.trigger_recovery()
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "LIVE"
                healed = await parquet_store.read_bars(key, start_ts=BASE_T, end_ts=BASE_T)
                assert len(healed) == 1
                assert healed[0].close == 5953.0 and healed[0].volume == 12.0
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_dynamic_subscribe_missing_or_error_ack_never_live_and_reconnects(
    tmp_path: Path,
) -> None:
    """Contract: Dynamic subscribe on connected socket waits for type=3 ACK and reconnects on fail.

    Defect caught: Marking dynamically added /ws/market contracts LIVE immediately when upstream
    type=3 subscribe ACK is missing (even with type=6 pings) or returns an error.
    """
    cid_nq = "CON.F.US.NQ.Z26"
    clock_ref = [BASE_T + 15]

    class _DynamicAckMockTransport(_MockSignalRTransport):
        def __init__(self, *, drop_nq_ack: bool, nq_error: str | None = None) -> None:
            super().__init__()
            self._drop_nq_ack = drop_nq_ack
            self._nq_error = nq_error

        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif (
                    frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                    and frame.get("invocationId") is not None
                ):
                    args = frame.get("arguments") or []
                    if args and args[0] == cid_nq and self._drop_nq_ack:
                        # Keep connection "alive" with type=6 pings so only ACK deadline catches it.
                        await self.incoming.put(json.dumps({"type": 6}) + SIGNALR_RECORD_SEPARATOR)
                        continue
                    comp: dict[str, Any] = {"type": 3, "invocationId": frame["invocationId"]}
                    if args and args[0] == cid_nq and self._nq_error:
                        comp["error"] = self._nq_error
                    await self.incoming.put(json.dumps(comp) + SIGNALR_RECORD_SEPARATOR)

    transports: list[_DynamicAckMockTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _DynamicAckMockTransport:
        idx = len(transports)
        if idx == 0:
            t = _DynamicAckMockTransport(drop_nq_ack=True)
        elif idx == 1:
            t = _DynamicAckMockTransport(drop_nq_ack=False, nq_error="not entitled")
        else:
            t = _DynamicAckMockTransport(drop_nq_ack=False)
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-dyn"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-dyn")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                connect_timeout_sec=0.15,
                heartbeat_timeout_sec=0.15,
                reconnect_initial_delay_sec=0.02,
                reconnect_max_delay_sec=0.05,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Dynamic subscribe on connected socket: transport 0 omits ACK for cid_nq.
                await live_feed.subscribe_ws_contract(cid_nq)
                assert live_feed.is_contract_live_alerts_enabled(cid_nq) is False
                assert live_feed.feed_state != "LIVE"

                # Session 1 returns type=3 error for cid_nq -> reconnects until session 2 succeeds.
                for _ in range(200):
                    if len(transports) >= 3 and live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.02)
                assert len(transports) >= 3
                assert live_feed.feed_state == "LIVE"
                assert live_feed.is_contract_live_alerts_enabled(cid_nq) is True
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_interleaved_ticks_between_initial_subscribe_acks_preserved(
    tmp_path: Path,
) -> None:
    """Contract: Market data arriving between initial subscribe type=3 ACKs is never dropped.

    Defect caught: Discarding GatewayTrade frames received after SubscribeContractQuotes ACK
    but before SubscribeContractTrades ACK completes during initial session connect.
    """
    class _InterleavedMockTransport(_MockSignalRTransport):
        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif frame.get("target") == "SubscribeContractQuotes":
                    await self.incoming.put(
                        json.dumps({"type": 3, "invocationId": frame["invocationId"]})
                        + SIGNALR_RECORD_SEPARATOR
                    )
                    # Server sends a live trade BEFORE sending SubscribeContractTrades completion!
                    await self.incoming.put(
                        json.dumps(_quote_frame(CID_MES, BASE_T + 12, 5960.5, 4.0))
                        + SIGNALR_RECORD_SEPARATOR
                    )
                elif frame.get("target") == "SubscribeContractTrades":
                    await self.incoming.put(
                        json.dumps({"type": 3, "invocationId": frame["invocationId"]})
                        + SIGNALR_RECORD_SEPARATOR
                    )

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _InterleavedMockTransport:
        return _InterleavedMockTransport()

    settings = Settings(data_dir=tmp_path / "data")
    events: asyncio.Queue[Any] = asyncio.Queue()
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="jwt-interleave"),
        ws_factory=ws_factory,
        contracts=[CID_MES],
        event_queue=events,
    )
    hub.start()
    try:
        emitted: list[Any] = []
        deadline = time.monotonic() + 2.0
        while len(emitted) < 2 and time.monotonic() < deadline:
            emitted.append(await asyncio.wait_for(events.get(), timeout=2.0))
        assert isinstance(emitted[0], SignalRConnectedEvent)
        assert emitted[1].contract_id == CID_MES
        assert emitted[1].price == 5960.5
        assert emitted[1].volume == 4.0
        assert emitted[1].timestamp_utc_sec == BASE_T + 12
    finally:
        await hub.stop()


async def test_f03_gateway_trade_missing_volume_and_timestamp_quarantined(
    tmp_path: Path,
) -> None:
    """Contract: GatewayTrade missing volume or timestamp is quarantined; volume=0 remains valid.

    Defect caught: Defaulting missing GatewayTrade volume to 0.0 or missing timestamp to
    wall-clock time instead of quarantining malformed events.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-missing"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-missing")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Valid zero-volume trade should be accepted cleanly while still LIVE.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 10, 5950.0, 0.0))
                await asyncio.sleep(0.02)
                assert live_feed.feed_state == "LIVE"

                # Malformed GatewayTrade missing volume key + GatewayTrade missing timestamp key.
                await transports[0].push_frame(
                    {
                        "type": 1,
                        "target": "GatewayTrade",
                        "arguments": [
                            CID_MES,
                            {
                                "symbolId": "F.US.MES",
                                "price": 5951.0,
                                "timestamp": datetime.fromtimestamp(BASE_T + 20, tz=UTC)
                                .isoformat()
                                .replace("+00:00", "Z"),
                            },
                        ],
                    }
                )
                await transports[0].push_frame(
                    {
                        "type": 1,
                        "target": "GatewayTrade",
                        "arguments": [
                            CID_MES,
                            {"symbolId": "F.US.MES", "price": 5952.0, "volume": 1.0},
                        ],
                    }
                )
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()

                assert live_feed.feed_state == "DEGRADED"
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                issue_codes = {item["issue_code"] for item in issues}
                assert {"missing_volume", "invalid_timestamp"} <= issue_codes
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_quarantine_backlog_bounded_under_slow_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Malformed tick quarantine writes use a single bounded worker under slow SQLite.

    Defect caught: Spawning unbounded asyncio.create_task per malformed SignalR frame under
    flood/slow-SQLite conditions instead of enforcing a bounded queue and overflow telemetry.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-qbound"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_queue_maxsize=3)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    orig_record_issues = sqlite_store.record_quality_issues
    gate_entered = asyncio.Event()
    release_gate = asyncio.Event()

    async def gated_record_issues(key: StorageDatasetKey, issues: list[Any]) -> int:
        gate_entered.set()
        await release_gate.wait()
        return await orig_record_issues(key, issues)

    monkeypatch.setattr(sqlite_store, "record_quality_issues", gated_record_issues)
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-qbound")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # First malformed tick enters worker and stalls at gate; next 9 test cap=3.
                live_feed.handle_hub_event(
                    SignalRMalformedTickEvent(
                        contract_id=CID_MES,
                        live_mode=False,
                        issue_code="negative_volume",
                        reason="negative volume",
                        bar_time=BASE_T + 16,
                        raw_summary="{'volume': -1}",
                        received_monotonic=time.monotonic(),
                    )
                )
                await asyncio.wait_for(gate_entered.wait(), timeout=2.0)
                for idx in range(9):
                    live_feed.handle_hub_event(
                        SignalRMalformedTickEvent(
                            contract_id=CID_MES,
                            live_mode=False,
                            issue_code="negative_volume",
                            reason="negative volume",
                            bar_time=BASE_T + 17 + idx,
                            raw_summary="{'volume': -1}",
                            received_monotonic=time.monotonic(),
                        )
                    )

                assert len(live_feed._live_tick_tasks) == 0
                assert live_feed._quarantine_worker_task is not None
                assert live_feed._quarantine_queue.qsize() == 3
                assert live_feed.queue_overflow_count >= 6
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False

                release_gate.set()
                await live_feed.flush_pending_tasks()
            finally:
                release_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_f03_malformed_tick_in_new_bucket_while_prior_bar_open_blocks_close() -> None:
    """Contract: Malformed tick in next bucket while prior open bar exists blocks new bar close.

    Defect caught: Applying _incomplete_open_buckets only on the existing-is-None branch, allowing
    the bucket-rollover branch (bucket_ts > existing.time) to finalize a gap-tainted bar.
    """
    from app.engine.bar_aggregator import NormalizedTick

    agg = BarAggregator(timeframes=("1m",))
    # 1) Open bar in BASE_T still open at BASE_T + 55.
    r1 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 55, 5950.0, 2.0),
        now_utc=BASE_T + 55,
    )
    assert r1.accepted is True
    # 2) Malformed tick arrives for BASE_T + 60 BEFORE any valid tick in BASE_T + 60.
    agg.mark_open_bucket_incomplete(CID_MES, False, BASE_T + 60)
    # 3) Valid tick arrives at BASE_T + 61 -> rolls over BASE_T and creates BASE_T + 60 open bar.
    r2 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 61, 5951.0, 1.0),
        now_utc=BASE_T + 61,
    )
    assert r2.accepted is True
    closed_before = [e for e in r2.events if e.bar.is_closed]
    assert [e.bar.time for e in closed_before] == [BASE_T]
    # 4) Advancing past BASE_T + 120 must discard BASE_T + 60 instead of finalizing it closed!
    flushed = agg.flush_closed_bars(now_utc=BASE_T + 125)
    assert flushed == []
    discarded = agg.pop_discarded_incomplete_bars(contract_id=CID_MES, live_mode=False)
    assert (CID_MES, False, "1m", BASE_T + 60) in discarded


async def test_f03_dynamic_subscribe_non_live_and_buffered_while_ack_pending(
    tmp_path: Path,
) -> None:
    """Contract: Dynamic subscribe keeps feed non-LIVE and buffers new ticks while ACK is pending.

    Defect caught: Recomputing aggregate feed state only after awaiting hub.update_contracts(),
    causing feed_state/live_alerts_enabled to remain LIVE and ticks to ingest before recovery.
    """
    from app.topstep.signalr_hub import SignalRTickEvent

    cid_nq = "CON.F.US.NQ.Z26"
    clock_ref = [BASE_T + 15]
    nq_ack_gate = asyncio.Event()
    nq_ack_sent = asyncio.Event()
    pending_nq_inv_ids: list[str] = []

    class _GatedDynamicAckTransport(_MockSignalRTransport):
        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif (
                    frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                    and frame.get("invocationId") is not None
                ):
                    args = frame.get("arguments") or []
                    if args and args[0] == cid_nq:
                        pending_nq_inv_ids.append(str(frame["invocationId"]))
                        if len(pending_nq_inv_ids) >= 2:
                            nq_ack_sent.set()
                        continue
                    comp = {"type": 3, "invocationId": frame["invocationId"]}
                    await self.incoming.put(json.dumps(comp) + SIGNALR_RECORD_SEPARATOR)

        async def release_nq_acks(self) -> None:
            await nq_ack_gate.wait()
            for inv_id in pending_nq_inv_ids:
                await self.incoming.put(
                    json.dumps({"type": 3, "invocationId": inv_id}) + SIGNALR_RECORD_SEPARATOR
                )

    transports: list[_GatedDynamicAckTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _GatedDynamicAckTransport:
        t = _GatedDynamicAckTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-gated"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-gated")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                connect_timeout_sec=2.0,
                heartbeat_timeout_sec=2.0,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                sub_task = asyncio.create_task(live_feed.subscribe_ws_contract(cid_nq))
                await asyncio.wait_for(nq_ack_sent.wait(), timeout=2.0)

                # While NQ subscribe ACK is held, feed MUST NOT report LIVE or direct-ingest NQ.
                assert live_feed.feed_state != "LIVE"
                assert live_feed.live_alerts_enabled is False
                assert live_feed.is_contract_live_alerts_enabled(cid_nq) is False
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        cid_nq, False, BASE_T + 16, 20100.0, 2.0, "GatewayTrade", time.monotonic()
                    )
                )
                assert live_feed._recovery_queue.qsize() >= 1

                nq_ack_gate.set()
                await transports[0].release_nq_acks()
                await asyncio.wait_for(sub_task, timeout=2.0)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "LIVE"
                assert live_feed.is_contract_live_alerts_enabled(cid_nq) is True
            finally:
                nq_ack_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_pre_connect_buffer_overflow_reports_overflow_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: Overflowing pre-connect event buffer reports overflow and prevents silent LIVE.

    Defect caught: Using bounded deque(maxlen=N) during initial subscribe ACK wait without
    reporting overflow to LiveFeedService, losing ticks silently with queue_overflow_count == 0.
    """
    clock_ref = [BASE_T + 15]

    class _OverflowPreConnectTransport(_MockSignalRTransport):
        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif frame.get("target") == "SubscribeContractQuotes":
                    await self.incoming.put(
                        json.dumps({"type": 3, "invocationId": frame["invocationId"]})
                        + SIGNALR_RECORD_SEPARATOR
                    )
                    # Push 5 trades while capacity is 3 BEFORE SubscribeContractTrades ACK.
                    for idx in range(5):
                        await self.incoming.put(
                            json.dumps(_quote_frame(CID_MES, BASE_T + 10 + idx, 5950.0 + idx, 1.0))
                            + SIGNALR_RECORD_SEPARATOR
                        )
                elif frame.get("target") == "SubscribeContractTrades":
                    await self.incoming.put(
                        json.dumps({"type": 3, "invocationId": frame["invocationId"]})
                        + SIGNALR_RECORD_SEPARATOR
                    )

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _OverflowPreConnectTransport:
        return _OverflowPreConnectTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-preovf"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_queue_maxsize=3)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-preovf")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.queue_overflow_count >= 2:
                        break
                    await asyncio.sleep(0.01)
                await live_feed.flush_pending_tasks()
                assert live_feed.queue_overflow_count >= 2
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_f03_ws_subscribe_rejects_timeframe_not_provided_by_aggregator(tmp_path: Path) -> None:
    """Contract: /ws/market subscribe rejects valid timeframes absent from active BarAggregator.

    Defect caught: Accepting '3m' or '30m' in WS subscribe even though BarAggregator defaults to
    ('1m', '5m', '15m', '1h', '4h', '1d') and never produces '3m' bar_update events.
    """
    from fastapi.testclient import TestClient

    pw = "ws-tf-check-pass"
    settings = Settings(
        app_password_hash=PasswordHasher().hash(pw),
        data_dir=tmp_path / "data",
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": pw})
        assert login_resp.status_code == 200
        cookie = login_resp.cookies["topstep_session"]
        with client.websocket_connect(
            "/ws/market",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "Cookie": f"topstep_session={cookie}",
            },
        ) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "feed_status"
            ws.send_json({"type": "subscribe", "contract_id": CID_MES, "timeframe": "3m"})
            err_msg = ws.receive_json()
            assert err_msg["type"] == "error"
            assert "3m" in err_msg["detail"]


async def test_f03_aggregator_rejected_out_of_range_timestamp_quarantined_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: Ticks rejected by BarAggregator._validate_tick enter quarantine and DEGRADED.

    Defect caught: Silently dropping syntactically numeric ticks whose timestamp is outside
    [DEFAULT_MIN_BAR_TIMESTAMP, now + DEFAULT_MAX_FUTURE_SKEW_SEC] without quarantine or DEGRADED.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-range"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-range")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Push GatewayTrade with valid price/volume/ISO timestamp 1 hour in the future.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 3600, 5960.0, 2.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()

                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                assert len(issues) == 1
                assert issues[0]["issue_code"] == "invalid_timestamp"
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_live_contract_ticks_preserved_while_new_contract_awaits_ack(
    tmp_path: Path,
) -> None:
    """Contract: Existing LIVE contract ticks continue aggregating while new contract awaits ACK.

    Defect caught: Dropping live ticks on an already-LIVE contract because aggregate feed_state
    became DISCONNECTED while a newly subscribed contract awaited upstream type=3 ACK.
    """
    from app.topstep.signalr_hub import SignalRTickEvent

    cid_nq = "CON.F.US.NQ.Z26"
    clock_ref = [BASE_T + 15]
    nq_ack_gate = asyncio.Event()
    nq_ack_sent = asyncio.Event()
    pending_nq_inv_ids: list[str] = []

    class _DualContractAckTransport(_MockSignalRTransport):
        async def send(self, data: str) -> None:
            for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
                if not chunk.strip():
                    continue
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()
                elif (
                    frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                    and frame.get("invocationId") is not None
                ):
                    args = frame.get("arguments") or []
                    if args and args[0] == cid_nq:
                        pending_nq_inv_ids.append(str(frame["invocationId"]))
                        if len(pending_nq_inv_ids) >= 2:
                            nq_ack_sent.set()
                        continue
                    comp = {"type": 3, "invocationId": frame["invocationId"]}
                    await self.incoming.put(json.dumps(comp) + SIGNALR_RECORD_SEPARATOR)

        async def release_nq_acks(self) -> None:
            await nq_ack_gate.wait()
            for inv_id in pending_nq_inv_ids:
                await self.incoming.put(
                    json.dumps({"type": 3, "invocationId": inv_id}) + SIGNALR_RECORD_SEPARATOR
                )

    transports: list[_DualContractAckTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _DualContractAckTransport:
        t = _DualContractAckTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-dual"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-dual")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                connect_timeout_sec=2.0,
                heartbeat_timeout_sec=2.0,
            )
            agg = BarAggregator(timeframes=("1m",))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                sub_task = asyncio.create_task(live_feed.subscribe_ws_contract(cid_nq))
                await asyncio.wait_for(nq_ack_sent.wait(), timeout=2.0)

                # While NQ awaits ACK, MES stays LIVE and MUST NOT drop incoming ticks.
                assert live_feed.feed_state != "LIVE"
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, BASE_T + 20, 5950.0, 2.0, "GatewayTrade", time.monotonic()
                    )
                )
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, BASE_T + 25, 5955.0, 3.0, "GatewayTrade", time.monotonic()
                    )
                )
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        cid_nq, False, BASE_T + 22, 20100.0, 1.0, "GatewayTrade", time.monotonic()
                    )
                )
                mes_open = agg.get_open_bar(CID_MES, False, "1m")
                assert mes_open is not None
                assert mes_open.open == 5950.0
                assert mes_open.high == 5955.0
                assert mes_open.volume == 5.0

                nq_ack_gate.set()
                await transports[0].release_nq_acks()
                await asyncio.wait_for(sub_task, timeout=2.0)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "LIVE"
                mes_after = agg.get_open_bar(CID_MES, False, "1m")
                assert mes_after is not None and mes_after.volume == 5.0
            finally:
                nq_ack_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_overflow_bucket_stays_incomplete_and_degraded_across_same_bucket_retry(
    tmp_path: Path,
) -> None:
    """Contract: Overflowed open bucket stays incomplete and DEGRADED across same-bucket retry.

    Defect caught: Clearing _recovery_overflowed on a watchdog retry in the same bucket without
    recording unhealed_gap_starts or marking the open bar incomplete.
    """
    from app.topstep.signalr_hub import SignalRTickEvent

    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    rest_gate = asyncio.Event()
    rest_entered = asyncio.Event()
    gate_enabled = [False]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ovf-retry"})
        if request.url.path == "/api/History/retrieveBars":
            if gate_enabled[0]:
                rest_entered.set()
                await rest_gate.wait()
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_queue_maxsize=1)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-ovf-retry")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Advance into next bucket BASE_T + 60 so _last_connected_utc < BASE_T + 60.
                clock_ref[0] = BASE_T + 70
                gate_enabled[0] = True
                rec_task = asyncio.create_task(live_feed.trigger_recovery())
                await asyncio.wait_for(rest_entered.wait(), timeout=2.0)

                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, BASE_T + 65, 5950.0, 2.0, "GatewayTrade", time.monotonic()
                    )
                )
                live_feed.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, BASE_T + 70, 5990.0, 9.0, "GatewayTrade", time.monotonic()
                    )
                )
                assert live_feed.queue_overflow_count >= 1

                gate_enabled[0] = False
                rest_gate.set()
                await asyncio.wait_for(rec_task, timeout=2.0)
                assert live_feed.feed_state == "DEGRADED"

                # Same-bucket retry (BASE_T + 80) must STILL remain DEGRADED (not clear gap!).
                clock_ref[0] = BASE_T + 80
                await live_feed.trigger_recovery()
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False

                # Even if BASE_T + 125 tick rolls over before REST confirms BASE_T+60, never close.
                clock_ref[0] = BASE_T + 125
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 125, 5960.0, 1.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert await parquet_store.read_bars(
                    key, start_ts=BASE_T + 60, end_ts=BASE_T + 60
                ) == []
            finally:
                rest_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_corrupted_cached_parquet_bar_not_counted_as_gap_healed(
    tmp_path: Path,
) -> None:
    """Contract: Cached Parquet bar inside CORRUPTED coverage never heals a missing REST bar.

    Defect caught: Merging Parquet bars in CORRUPTED coverage intervals into present_closed_times,
    falsely clearing unhealed_gap_starts and recording COMPLETE when REST omits that bucket.
    """
    clock_ref = [BASE_T + 65]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-corrupt-cache"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = [
                b
                for b in _bars_for_history_payload(payload)
                if int(b["t"]) != (BASE_T - 60)
            ]
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
        await parquet_store.upsert_bars(
            key,
            [
                OHLCVBar(BASE_T - 120, 100.0, 101.0, 99.0, 100.5, 5.0, True),
                OHLCVBar(BASE_T - 60, 100.5, 102.0, 100.0, 101.0, 6.0, True),
                OHLCVBar(BASE_T, 101.0, 102.0, 100.5, 101.5, 7.0, True),
            ],
            now_utc=BASE_T + 65,
        )
        await sqlite_store.record_coverage(key, BASE_T - 120, BASE_T - 60, status="COMPLETE")
        await sqlite_store.record_coverage(
            key, BASE_T - 60, BASE_T, status="CORRUPTED", quarantined_count=1
        )
        await sqlite_store.record_coverage(key, BASE_T, BASE_T + 60, status="COMPLETE")
        await sqlite_store.upsert_watermark(key, BASE_T)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-corrupt-cache")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.get_status_snapshot()["unresolved_gaps"] >= 1:
                        break
                    await asyncio.sleep(0.01)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
                assert live_feed._contracts[CID_MES].unhealed_gap_starts["1m"] == BASE_T - 60
                assert (
                    await sqlite_store.is_interval_complete(key, BASE_T - 120, BASE_T + 60)
                    is False
                )
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_recovery_buffered_out_of_range_tick_quarantined_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: Out-of-range buffered ticks seen during RECOVERING are quarantined and DEGRADED.

    Defect caught: Silently skipping invalid buffered ticks in BarAggregator.reconcile_recovery()
    so recovery could exit to LIVE without recording SQLite quality issues.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    rest_gate = asyncio.Event()
    rest_entered = asyncio.Event()

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-rec-range"})
        if request.url.path == "/api/History/retrieveBars":
            rest_entered.set()
            await rest_gate.wait()
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-rec-range")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                await asyncio.wait_for(rest_entered.wait(), timeout=2.0)
                assert live_feed.feed_state == "RECOVERING"

                # Push out-of-range future timestamp tick while recovery backfill is in-flight.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 3600, 5960.0, 2.0))
                await asyncio.sleep(0.02)
                rest_gate.set()

                for _ in range(100):
                    if live_feed.feed_state == "DEGRADED":
                        break
                    await asyncio.sleep(0.01)
                await live_feed.flush_pending_tasks()

                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                assert len(issues) == 1
                assert issues[0]["issue_code"] == "invalid_timestamp"
            finally:
                rest_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_rest_triggered_jwt_refresh_reconnects_hub_via_watchdog(
    tmp_path: Path,
) -> None:
    """Contract: Watchdog detects REST-triggered JWT refresh and reconnects Hub with new token.

    Defect caught: Checking only auth.needs_refresh() in run_watchdog_tick(), leaving Hub on an
    older JWT after a REST call refreshed the token and reset needs_refresh() to False.
    """
    from datetime import timedelta

    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    ws_urls: list[str] = []
    tokens_issued = ["jwt-refresh-v1", "jwt-refresh-v2"]
    login_idx = [0]

    async def ws_factory(url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        ws_urls.append(url)
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            tok = tokens_issued[min(login_idx[0], len(tokens_issued) - 1)]
            login_idx[0] += 1
            return httpx.Response(200, json={"success": True, "token": tok})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", jwt_refresh_hours=1)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-jwt-sync")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.01,
                reconnect_max_delay_sec=0.02,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"
                assert "jwt-refresh-v1" in ws_urls[0]

                # Simulate token aging followed by REST call triggering JWT refresh before watchdog.
                assert auth._token_acquired_at is not None
                auth._token_acquired_at = auth._token_acquired_at - timedelta(hours=2)
                await rest.retrieve_bars_paginated(
                    contract_id=CID_MES,
                    live=False,
                    timeframe="1m",
                    start_ts=BASE_T - 120,
                    end_ts=BASE_T,
                    include_partial_bar=False,
                )
                assert auth.needs_refresh() is False
                assert auth.token_generation >= 2

                # Watchdog must detect hub's stale token digest and force LIVE -> RECOVERING.
                await live_feed.run_watchdog_tick()
                for _ in range(100):
                    if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.02)
                assert len(transports) >= 2
                assert "jwt-refresh-v2" in ws_urls[-1]
                assert live_feed.feed_state == "LIVE"
                assert hub.connected_token_digest == auth.current_token_digest
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_f03_late_tick_for_rest_confirmed_1m_minute_does_not_mutate_open_htf_bar() -> None:
    """Contract: Late tick in a REST-confirmed 1m minute never mutates open 5m/15m/HTF bars.

    Defect caught: Checking only per-timeframe watermarks in BarAggregator.process_tick(), allowing
    a late tick for an already-confirmed 1m bar to double-count volume/close into an open 5m bar.
    """
    from app.engine.bar_aggregator import NormalizedTick

    agg = BarAggregator(timeframes=("1m", "5m"))
    bar_1m = OHLCVBar(BASE_T, 100.0, 106.0, 99.0, 105.0, 10.0, True)
    agg.reconcile_recovery(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="1m",
        historical_bars=[bar_1m],
        buffered_ticks=(),
        now_utc=BASE_T + 90,
        mark_open_incomplete=False,
    )
    recon_5m = agg.reconcile_from_1m_bars(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="5m",
        closed_1m_bars=[bar_1m],
        open_1m_bar=None,
        now_utc=BASE_T + 90,
    )
    assert recon_5m.open_bar is not None
    assert recon_5m.open_bar.bar.close == 105.0
    assert recon_5m.open_bar.bar.volume == 10.0

    # Late tick belongs to BASE_T (14:00:20) which was already REST-confirmed in 1m.
    res = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 20, 110.0, 3.0),
        now_utc=BASE_T + 90,
    )
    assert res.accepted is True
    assert res.events == ()
    open_5m = agg.get_open_bar(CID_MES, False, "5m")
    assert open_5m is not None
    assert open_5m.close == 105.0
    assert open_5m.volume == 10.0


def test_f03_two_pending_incomplete_buckets_both_preserved_before_opening() -> None:
    """Contract: Marking multiple future buckets incomplete preserves every pending bucket marker.

    Defect caught: Storing a single integer in _incomplete_open_buckets[(cid, live, tf)], causing
    a second bucket marker (b2) to overwrite an earlier bucket marker (b1).
    """
    from app.engine.bar_aggregator import NormalizedTick

    agg = BarAggregator(timeframes=("1m",))
    # Open b0 at BASE_T + 55.
    r0 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 55, 5950.0, 2.0),
        now_utc=BASE_T + 55,
    )
    assert r0.accepted is True

    # Mark BOTH b1 (BASE_T+60) and b2 (BASE_T+120) incomplete before valid ticks open them.
    agg.mark_open_bucket_incomplete(CID_MES, False, BASE_T + 65)
    agg.mark_open_bucket_incomplete(CID_MES, False, BASE_T + 125)

    # Open b1 then roll into b2, then flush past b2.
    r1 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 70, 5951.0, 1.0),
        now_utc=BASE_T + 70,
    )
    assert [e.bar.time for e in r1.events if e.bar.is_closed] == [BASE_T]

    r2 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 130, 5952.0, 1.0),
        now_utc=BASE_T + 130,
    )
    assert [e.bar.time for e in r2.events if e.bar.is_closed] == []

    flushed = agg.flush_closed_bars(now_utc=BASE_T + 185)
    assert flushed == []
    discarded = agg.pop_discarded_incomplete_bars(contract_id=CID_MES, live_mode=False)
    assert (CID_MES, False, "1m", BASE_T + 60) in discarded
    assert (CID_MES, False, "1m", BASE_T + 120) in discarded


async def test_f03_slow_quarantine_worker_cannot_overwrite_healed_complete_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Slow quarantine worker writes quality audit row without reverting COMPLETE heal.

    Defect caught: Delayed _quarantine_malformed_tick writing CORRUPTED coverage after a newer
    recovery generation already healed the interval to COMPLETE and LIVE.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-qheal"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    orig_record_issues = sqlite_store.record_quality_issues
    gate_entered = asyncio.Event()
    release_gate = asyncio.Event()

    async def gated_record_issues(key: StorageDatasetKey, issues: list[Any]) -> int:
        gate_entered.set()
        await release_gate.wait()
        return await orig_record_issues(key, issues)

    monkeypatch.setattr(sqlite_store, "record_quality_issues", gated_record_issues)
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-qheal")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Malformed tick in BASE_T stalls worker inside record_quality_issues.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 20, 5951.0, -3.0))
                await asyncio.wait_for(gate_entered.wait(), timeout=2.0)
                assert live_feed.feed_state == "DEGRADED"

                # Advance past BASE_T close and heal via REST recovery while worker is paused.
                clock_ref[0] = BASE_T + 65
                await live_feed.trigger_recovery()
                assert live_feed.feed_state == "LIVE"
                assert live_feed.get_status_snapshot()["unresolved_gaps"] == 0

                # Now release slow worker: audit issue writes, but BASE_T coverage stays COMPLETE.
                release_gate.set()
                await live_feed.flush_pending_tasks()
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                assert len(issues) == 1
                assert await sqlite_store.is_interval_complete(key, BASE_T, BASE_T + 60) is True
                assert live_feed.feed_state == "LIVE"
            finally:
                release_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_self_healed_recovery_resets_unresolved_gaps_to_zero(
    tmp_path: Path,
) -> None:
    """Contract: Self-healing recovery resets active unresolved_gaps telemetry back to zero.

    Defect caught: Leaving c_state.unresolved_gaps / _global_unresolved_gaps positive after
    subsequent recovery cleanly healed all gaps and transitioned feed_state back to LIVE.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-heal-zero"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-heal-zero")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 20, 5951.0, -2.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.get_status_snapshot()["unresolved_gaps"] >= 1

                clock_ref[0] = BASE_T + 65
                await live_feed.trigger_recovery()
                await live_feed.flush_pending_tasks()
                snap = live_feed.get_status_snapshot()
                assert snap["feed_state"] == "LIVE"
                assert snap["unresolved_gaps"] == 0
                assert snap["contracts"][CID_MES]["unresolved_gaps"] == 0
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_transient_jwt_refresh_failure_retries_and_recovers_to_live(
    tmp_path: Path,
) -> None:
    """Contract: Transient loginKey failure during JWT refresh retries via watchdog to LIVE.

    Defect caught: Stopping SignalR permanently on a single 503/timeout during proactive JWT
    refresh without preserving retry capability once upstream loginKey recovers.
    """
    from datetime import timedelta

    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    login_mode = ["v1"]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            if login_mode[0] == "v1":
                return httpx.Response(200, json={"success": True, "token": "jwt-retry-v1"})
            if login_mode[0] == "outage":
                return httpx.Response(503, json={"success": False, "errorMessage": "unavailable"})
            return httpx.Response(200, json={"success": True, "token": "jwt-retry-v2"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        jwt_refresh_hours=1,
        upstream_max_retries=0,
        signalr_reconnect_initial_delay_sec=0.01,
        signalr_reconnect_max_delay_sec=0.02,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-transient-jwt")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.01,
                reconnect_max_delay_sec=0.02,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Force JWT refresh while loginKey returns 503 -> DISCONNECTED, retryable.
                login_mode[0] = "outage"
                assert auth._token_acquired_at is not None
                auth._token_acquired_at = auth._token_acquired_at - timedelta(hours=2)
                await live_feed.run_watchdog_tick()
                assert live_feed.feed_state == "DISCONNECTED"
                assert auth.can_retry_login is True

                # Upstream loginKey recovers -> subsequent watchdog tick re-auths & returns LIVE.
                login_mode[0] = "v2"
                await asyncio.sleep(0.02)
                await live_feed.run_watchdog_tick()
                for _ in range(100):
                    if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.02)
                assert len(transports) >= 2
                assert live_feed.feed_state == "LIVE"
                assert live_feed.live_alerts_enabled is True
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_malformed_tick_with_huge_timestamp_does_not_crash_hub(
    tmp_path: Path,
) -> None:
    """Contract: Malformed GatewayTrade with huge timestamp 1e30 quarantines without OverflowError.

    Defect caught: Calling bucket_open_utc() directly on out-of-range malformed bar_time values
    (e.g. 1e30), raising OverflowError out of the SignalR callback and dropping all contracts.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-1e30"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-1e30")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Invalid price + huge finite timestamp 1e30 must quarantine cleanly (no drop).
                await transports[0].push_frame(
                    {
                        "type": 1,
                        "target": "GatewayTrade",
                        "arguments": [
                            CID_MES,
                            {
                                "symbolId": "F.US.MES",
                                "price": "bad",
                                "volume": 1.0,
                                "timestamp": 1e30,
                            },
                        ],
                    }
                )
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()

                assert len(transports) == 1
                assert hub.is_connected is True
                assert live_feed.feed_state == "DEGRADED"
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                assert len(issues) == 1
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_failed_rest_recovery_marks_open_bar_incomplete_across_boundary(
    tmp_path: Path,
) -> None:
    """Contract: Failed REST recovery marks interrupted open bars incomplete so ticks cannot close.

    Defect caught: Raising out of _recover_single_timeframe_rest on REST timeout/503 without marking
    existing active open bars incomplete, letting subsequent ticks finalize an unconfirmed bar.
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    hist_mode = ["ok"]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-fail-open"})
        if request.url.path == "/api/History/retrieveBars":
            if hist_mode[0] == "fail":
                return httpx.Response(503, json={"success": False, "errorMessage": "down"})
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", upstream_max_retries=0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-fail-open")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.01,
                reconnect_max_delay_sec=0.02,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Open BASE_T bar while LIVE.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 50, 5950.0, 2.0))
                await asyncio.sleep(0.03)

                # Fail REST during reconnect recovery, then advance across boundary BASE_T + 60.
                hist_mode[0] = "fail"
                clock_ref[0] = BASE_T + 75
                await transports[0].drop()
                for _ in range(100):
                    if len(transports) >= 2 and live_feed.feed_state == "DEGRADED":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "DEGRADED"

                await transports[1].push_frame(_quote_frame(CID_MES, BASE_T + 75, 5955.0, 3.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()

                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert await parquet_store.read_bars(key, start_ts=BASE_T, end_ts=BASE_T) == []
                wm = await sqlite_store.get_watermark(key)
                assert wm is not None and wm.last_closed_bar_time < BASE_T
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_rest_triggered_transient_jwt_refresh_failure_recovers_via_watchdog(
    tmp_path: Path,
) -> None:
    """Contract: Transient JWT refresh failure triggered by REST recovers via watchdog to LIVE.

    Defect caught: Gating watchdog retry_login() on _auth_retry_pending, leaving the feed stuck
    DISCONNECTED when a REST request (not watchdog) triggered the transient refresh failure.
    """
    from datetime import timedelta

    from app.topstep.errors import UpstreamHTTPError

    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []
    login_mode = ["v1"]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            if login_mode[0] == "v1":
                return httpx.Response(200, json={"success": True, "token": "jwt-rest-v1"})
            if login_mode[0] == "outage":
                return httpx.Response(503, json={"success": False, "errorMessage": "unavailable"})
            return httpx.Response(200, json={"success": True, "token": "jwt-rest-v2"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        jwt_refresh_hours=1,
        upstream_max_retries=0,
        signalr_reconnect_initial_delay_sec=0.01,
        signalr_reconnect_max_delay_sec=0.02,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-rest-jwt")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
                reconnect_initial_delay_sec=0.01,
                reconnect_max_delay_sec=0.02,
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # REST call triggers JWT refresh during 503 outage (never touching watchdog yet).
                login_mode[0] = "outage"
                assert auth._token_acquired_at is not None
                auth._token_acquired_at = auth._token_acquired_at - timedelta(hours=2)
                with pytest.raises(UpstreamHTTPError):
                    await rest.retrieve_bars_paginated(
                        contract_id=CID_MES,
                        live=False,
                        timeframe="1m",
                        start_ts=BASE_T - 120,
                        end_ts=BASE_T,
                        include_partial_bar=False,
                    )
                assert auth.is_connected is False
                assert auth.can_retry_login is True

                # Watchdog runs while still in outage -> DISCONNECTED, then recovers when 200 OK.
                await live_feed.run_watchdog_tick()
                assert live_feed.feed_state == "DISCONNECTED"

                login_mode[0] = "v2"
                await asyncio.sleep(0.02)
                await live_feed.run_watchdog_tick()
                for _ in range(100):
                    if len(transports) >= 2 and live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.02)
                assert len(transports) >= 2
                assert live_feed.feed_state == "LIVE"
                assert live_feed.live_alerts_enabled is True
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_f03_reconcile_recovery_preserves_unconfirmed_future_incomplete_markers() -> None:
    """Contract: reconcile_recovery keeps pending incomplete markers newer than closed history.

    Defect caught: Clearing all _incomplete_open_buckets[stream_key] during reconciliation of an
    earlier open bar (T), losing incomplete markers already queued for a future bucket (T+60).
    """
    from app.engine.bar_aggregator import NormalizedTick

    agg = BarAggregator(timeframes=("1m",))
    agg.process_tick(NormalizedTick(CID_MES, False, BASE_T + 10, 5950.0, 1.0), now_utc=BASE_T + 10)
    agg.mark_open_bucket_incomplete(CID_MES, False, BASE_T + 65)

    # Reconcile prior closed history up to BASE_T - 60 while BASE_T open bar is rebuilt.
    hist_bar = OHLCVBar(BASE_T - 60, 5948.0, 5949.0, 5947.0, 5948.5, 5.0, True)
    agg.reconcile_recovery(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="1m",
        historical_bars=[hist_bar],
        buffered_ticks=[NormalizedTick(CID_MES, False, BASE_T + 20, 5951.0, 2.0)],
        now_utc=BASE_T + 30,
        mark_open_incomplete=False,
    )

    # Now tick arrives in BASE_T + 60, then rolls into BASE_T + 120 -> BASE_T + 60 must discard!
    r1 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 70, 5952.0, 1.0),
        now_utc=BASE_T + 70,
    )
    assert r1.accepted is True
    r2 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 130, 5953.0, 1.0),
        now_utc=BASE_T + 130,
    )
    assert [e for e in r2.events if e.bar.is_closed] == []
    discarded = agg.pop_discarded_incomplete_bars(contract_id=CID_MES, live_mode=False)
    assert (CID_MES, False, "1m", BASE_T + 60) in discarded


async def test_f03_malformed_tick_during_recovering_keeps_valid_ticks_buffered_for_htf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Malformed tick during RECOVERING keeps buffering valid ticks until rebuild ends.

    Defect caught: Flipping c_state.state to DEGRADED mid-recovery on malformed tick, causing
    concurrent valid ticks to bypass _recovery_queue and be overwritten by HTF rebuild.
    """
    clock_ref = [BASE_T + 65]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-htf-mal"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_read_bars = parquet_store.read_bars
        htf_gate_entered = asyncio.Event()
        htf_gate_release = asyncio.Event()

        async def gated_read_bars(
            key: StorageDatasetKey,
            *,
            start_ts: int | None = None,
            end_ts: int | None = None,
            include_provisional: bool = False,
        ) -> list[OHLCVBar]:
            # Pause specifically when _recover_higher_timeframes_from_1m reads 1m history.
            if key.timeframe == "1m" and end_ts == BASE_T + 65:
                htf_gate_entered.set()
                await htf_gate_release.wait()
            return await orig_read_bars(
                key,
                start_ts=start_ts,
                end_ts=end_ts,
                include_provisional=include_provisional,
            )

        monkeypatch.setattr(parquet_store, "read_bars", gated_read_bars)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-htf-mal")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            agg = BarAggregator(timeframes=("1m", "5m"))
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                signalr_hub=hub,
                recovery_timeframes=("1m", "5m"),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                await asyncio.wait_for(htf_gate_entered.wait(), timeout=2.0)

                # Malformed tick + valid trade arrive while HTF rebuild is awaiting Parquet.
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 66, 5950.0, -2.0))
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 68, 5958.0, 4.0))
                await asyncio.sleep(0.03)
                assert live_feed._contracts[CID_MES].state == "RECOVERING"

                htf_gate_release.set()
                for _ in range(100):
                    if live_feed.feed_state == "DEGRADED":
                        break
                    await asyncio.sleep(0.01)
                await live_feed.flush_pending_tasks()

                assert live_feed.feed_state == "DEGRADED"
                open_1m = agg.get_open_bar(CID_MES, False, "1m")
                open_5m = agg.get_open_bar(CID_MES, False, "5m")
                assert open_1m is not None and open_1m.close == 5958.0 and open_1m.volume == 4.0
                # 5m bar combines the closed 1m history bar at BASE_T (vol=5.0) + live trade (4.0).
                assert open_5m is not None and open_5m.close == 5958.0 and open_5m.volume == 9.0
            finally:
                htf_gate_release.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_rest_out_of_window_bar_during_recovery_quarantined_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: Out-of-window REST bars during recovery are quarantined and transition DEGRADED.

    Defect caught: Passing valid-OHLCV REST bars outside [interval_start, interval_end] directly
    into reconcile_recovery() and Parquet instead of quarantining out_of_window_timestamp.
    """
    clock_ref = [BASE_T + 180]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return _MockSignalRTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-oow"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(payload)
            # Inject an out-of-window bar 1 hour older than requested backfill window.
            bars.insert(0, _wire_bar(BASE_T - 3600, 99.0, 100.0, 98.0, 99.5, 5.0))
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
        await sqlite_store.upsert_watermark(key, BASE_T + 120)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-oow")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "DEGRADED":
                        break
                    await asyncio.sleep(0.01)
                await live_feed.flush_pending_tasks()
                assert live_feed.feed_state == "DEGRADED"
                issues = await sqlite_store.list_quality_issues(key)
                assert any(i["issue_code"] == "out_of_window_timestamp" for i in issues)
                assert (
                    await parquet_store.read_bars(
                        key, start_ts=BASE_T - 3600, end_ts=BASE_T - 3600
                    )
                    == []
                )
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_signalr_backoff_resets_after_successful_session_drop(
    tmp_path: Path,
) -> None:
    """Contract: Reconnect backoff resets to initial_delay after a connected session later drops.

    Defect caught: Only setting session_connected=True on normal return instead of after initial
    handshake+subscribe, causing backoff to stay elevated across distinct session drops.
    """
    dial_times: list[float] = []

    class _SessionResetTransport(_MockSignalRTransport):
        def __init__(self, *, mode: str) -> None:
            super().__init__()
            self._mode = mode

        async def recv(self) -> str:
            if self._mode == "fail_dial":
                raise ConnectionError("dial failed")
            item = await self.incoming.get()
            if isinstance(item, Exception):
                raise item
            return item

    transports: list[_SessionResetTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _SessionResetTransport:
        idx = len(dial_times)
        dial_times.append(time.monotonic())
        mode = "fail_dial" if idx in {0, 1} else "ok"
        t = _SessionResetTransport(mode=mode)
        transports.append(t)
        return t

    settings = Settings(data_dir=tmp_path / "data")
    events: asyncio.Queue[Any] = asyncio.Queue()
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="jwt-backoff-reset"),
        ws_factory=ws_factory,
        contracts=[CID_MES],
        event_queue=events,
        reconnect_initial_delay_sec=0.06,
        reconnect_max_delay_sec=0.50,
    )
    hub.start()
    try:
        # Wait for Session 2 (after two failed dials: ~0.06s + ~0.12s) to connect cleanly.
        for _ in range(100):
            if len(dial_times) >= 3 and hub.is_connected:
                break
            await asyncio.sleep(0.01)
        assert len(dial_times) >= 3 and hub.is_connected is True

        # Drop healthy Session 2 -> backoff MUST reset to initial ~0.06s (NOT ~0.24s!).
        drop_at = time.monotonic()
        await transports[2].drop(ConnectionError("mid-stream drop"))
        for _ in range(100):
            if len(dial_times) >= 4 and hub.is_connected:
                break
            await asyncio.sleep(0.01)
        assert len(dial_times) >= 4
        gap_fail_0 = dial_times[1] - dial_times[0]
        gap_fail_1 = dial_times[2] - dial_times[1]
        gap_after_healthy = dial_times[3] - drop_at
        assert gap_fail_1 > gap_fail_0 * 1.4
        assert gap_after_healthy < gap_fail_1 * 0.85
    finally:
        await hub.stop()


def test_f03_incomplete_1m_with_empty_open_bar_propagates_to_htf_and_blocks_close() -> None:
    """Contract: Mid-candle incomplete 1m bucket with open_1m_bar=None propagates to HTF open bar.

    Defect caught: Only checking _incomplete_open_bars in reconcile_from_1m_bars() when
    open_1m_bar is not None, letting an incomplete 5m bar finalize closed when buffer was empty.
    """
    agg = BarAggregator(timeframes=("1m", "5m"))
    closed_1m = [
        OHLCVBar(BASE_T + (i * 60), 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 4.0, True)
        for i in range(4)
    ]
    now_mid = BASE_T + 4 * 60 + 30  # 14:04:30Z
    recon_1m = agg.reconcile_recovery(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="1m",
        historical_bars=closed_1m,
        buffered_ticks=(),
        now_utc=now_mid,
        mark_open_incomplete=True,
    )
    assert recon_1m.open_bar is None

    recon_5m = agg.reconcile_from_1m_bars(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="5m",
        closed_1m_bars=closed_1m,
        open_1m_bar=None,
        now_utc=now_mid,
        covered_start_utc=BASE_T,
    )
    assert recon_5m.open_bar is not None

    flushed = agg.flush_closed_bars(now_utc=BASE_T + 5 * 60 + 5)
    assert flushed == []
    discarded = agg.pop_discarded_incomplete_bars(contract_id=CID_MES, live_mode=False)
    assert (CID_MES, False, "5m", BASE_T) in discarded


async def test_f03_healing_historical_gap_while_new_open_bucket_malformed_stays_degraded(
    tmp_path: Path,
) -> None:
    """Contract: Healing an older historical gap never masks a new open-bucket gap during recovery.

    Defect caught: Storing only min(prev_gap, bar_open_1m) so healing 14:00 cleared a concurrent
    malformed tick gap in the active 14:01 bucket and falsely transitioned to LIVE.
    """
    clock_ref = [BASE_T + 10]
    transports: list[_MockSignalRTransport] = []
    rest_gate = asyncio.Event()
    rest_entered = asyncio.Event()
    gate_rest = [False]

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-two-gaps"})
        if request.url.path == "/api/History/retrieveBars":
            if gate_rest[0]:
                rest_entered.set()
                await rest_gate.wait()
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-two-gaps")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                # Historical gap in BASE_T (14:00).
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 20, 5950.0, -1.0))
                await asyncio.sleep(0.03)
                assert live_feed._contracts[CID_MES].unhealed_gap_starts["1m"] == BASE_T

                # Recover BASE_T at 14:01:10 while a NEW gap + valid tick arrive in 14:01!
                clock_ref[0] = BASE_T + 70
                gate_rest[0] = True
                rec_task = asyncio.create_task(live_feed.trigger_recovery())
                await asyncio.wait_for(rest_entered.wait(), timeout=2.0)

                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 72, 5952.0, -2.0))
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 75, 5955.0, 3.0))
                await asyncio.sleep(0.02)

                gate_rest[0] = False
                rest_gate.set()
                await asyncio.wait_for(rec_task, timeout=2.0)
                await live_feed.flush_pending_tasks()

                # BASE_T healed, but BASE_T+60 MUST remain unhealed and DEGRADED!
                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
                assert live_feed._contracts[CID_MES].unhealed_gap_starts["1m"] == BASE_T + 60

                # Crossing into BASE_T+120 must NOT close/persist incomplete BASE_T+60 bar.
                clock_ref[0] = BASE_T + 125
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 125, 5958.0, 1.0))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert (
                    await parquet_store.read_bars(
                        key, start_ts=BASE_T + 60, end_ts=BASE_T + 60
                    )
                    == []
                )
            finally:
                rest_gate.set()
                await live_feed.stop()
    finally:
        sqlite_store.close()


async def test_f03_finite_tick_volumes_overflowing_to_inf_quarantined_and_degraded(
    tmp_path: Path,
) -> None:
    """Contract: Finite tick volumes whose sum overflows to inf are rejected and quarantined.

    Defect caught: Returning accepted=True when existing.volume + volume overflows to inf,
    silently dropping the tick without quarantining or transitioning to DEGRADED.
    """
    from app.engine.bar_aggregator import NormalizedTick

    agg = BarAggregator(timeframes=("1m", "5m"))
    r1 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 10, 5950.0, 1e308),
        now_utc=BASE_T + 15,
    )
    assert r1.accepted is True
    r2 = agg.process_tick(
        NormalizedTick(CID_MES, False, BASE_T + 20, 5951.0, 1e308),
        now_utc=BASE_T + 25,
    )
    assert r2.accepted is False
    assert r2.reason is not None
    assert agg.get_open_bar(CID_MES, False, "1m") is not None
    assert agg.get_open_bar(CID_MES, False, "1m").volume == 1e308

    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-vol-ovf"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-vol-ovf")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await live_feed.set_active_contracts([CID_MES], live_mode=False)
                await live_feed.start()
                for _ in range(100):
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"

                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 10, 5950.0, 1e308))
                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 20, 5951.0, 1e308))
                await asyncio.sleep(0.03)
                await live_feed.flush_pending_tasks()

                assert live_feed.feed_state == "DEGRADED"
                assert live_feed.live_alerts_enabled is False
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                issues = await sqlite_store.list_quality_issues(key)
                assert any(i["issue_code"] == "invalid_tick" for i in issues)
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_f03_websockets_debug_frame_and_handshake_logs_redact_jwt(tmp_path: Path) -> None:
    """Contract: WebSocket Frame and handshake DEBUG logs never expose active JWTs.

    Defect caught: Passing raw websockets.frames.Frame objects through SecretRedactionFilter
    without redacting Frame.__str__ before logging formatter renders DEBUG frames.
    """
    from websockets.frames import Frame, Opcode

    secret_jwt = "jwt-super-secret-token-123"
    settings = Settings(data_dir=tmp_path / "data")
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result=secret_jwt),
        contracts=[CID_MES],
    )
    hub._recent_tokens.append(secret_jwt)

    captured: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _ListHandler()
    prev_level = hub._ws_logger.level
    hub._ws_logger.setLevel(logging.DEBUG)
    hub._ws_logger.addHandler(handler)
    try:
        frame = Frame(
            Opcode.TEXT,
            f'{{"type":7,"error":"unauthorized {secret_jwt}"}}'.encode(),
        )
        hub._ws_logger.debug("< %s", frame)
        hub._ws_logger.debug("> GET wss://rtc.topstepx.com/hubs/market?access_token=%s", secret_jwt)
        assert len(captured) == 2
        for line in captured:
            assert secret_jwt not in line
            assert "***REDACTED***" in line
    finally:
        hub._ws_logger.removeHandler(handler)
        hub._ws_logger.setLevel(prev_level)


@pytest.mark.asyncio
async def test_f03_last_subscriber_disconnect_racing_with_new_subscribe_keeps_hub_active(
    tmp_path: Path,
) -> None:
    """Contract: P1-001 serializes sync_ws_contracts(empty) and concurrent subscribe_ws_contract.
    Defect caught: hub.stop() cancelling/clearing a concurrent new subscriber's hub task.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_enabled=True,
        signalr_ack_timeout_sec=0.5,
    )
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    transports: list[_MockSignalRTransport] = []

    class _SlowCloseTransport(_MockSignalRTransport):
        async def close(self) -> None:
            if not close_entered.is_set():
                close_entered.set()
                await asyncio.wait_for(close_release.wait(), timeout=2.0)
            await super().close()

    async def _ws_factory(_url: str, _headers: dict[str, str]) -> _SlowCloseTransport:
        tr = _SlowCloseTransport()
        transports.append(tr)
        return tr

    class _EmptyRest:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            return type("R", (), {"raw_bars": [], "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        hub = SignalRMarketHub(
            settings,
            token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
            contracts=[],
            ws_factory=_ws_factory,
        )
        svc = LiveFeedService(
            settings,
            auth,
            _EmptyRest(),
            parquet,
            sqlite,
            signalr_hub=hub,
            recovery_timeframes=("1m",),
            clock=lambda: BASE_T + 15,
        )
        try:
            await svc.start()
            await svc.subscribe_ws_contract(CID_MES)
            for _ in range(50):
                if hub.is_connected and transports:
                    break
                await asyncio.sleep(0.01)
            assert hub.is_connected

            t_disc = asyncio.create_task(svc.sync_ws_contracts(set()))
            await asyncio.wait_for(close_entered.wait(), timeout=2.0)
            t_sub = asyncio.create_task(svc.subscribe_ws_contract(CID_MES))
            await asyncio.sleep(0.02)
            assert not t_sub.done()

            close_release.set()
            await asyncio.wait_for(asyncio.gather(t_disc, t_sub), timeout=2.0)
            for _ in range(50):
                if hub.is_connected and len(transports) >= 2:
                    break
                await asyncio.sleep(0.01)

            assert len(transports) >= 2
            assert hub.is_connected
            assert CID_MES in svc.get_status_snapshot()["contracts"]
        finally:
            close_release.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_dynamic_mid_candle_contract_add_keeps_first_partial_minute_incomplete(
    tmp_path: Path,
) -> None:
    """Contract: P2-DYNAMIC-001 marks a newly added contract's initial open minute incomplete.
    Defect caught: mid-candle dynamic add using older hub connect_utc and closing partial bar.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    now_val = [BASE_T + 15]

    class _RestStub:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            start_ts = int(kw.get("start_ts", BASE_T))
            end_ts = int(kw.get("end_ts", BASE_T))
            closed_cutoff = now_val[0] - (now_val[0] % 60)
            bars = [
                {
                    "t": ts,
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.5,
                    "v": 5.0,
                }
                for ts in range(start_ts - (start_ts % 60), end_ts, 60)
                if ts < closed_cutoff
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    tr = _MockSignalRTransport()

    async def _ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        return tr

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        hub = SignalRMarketHub(
            settings,
            token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
            contracts=[CID_MES],
            ws_factory=_ws_factory,
        )
        svc = LiveFeedService(
            settings,
            auth,
            _RestStub(),
            parquet,
            sqlite,
            signalr_hub=hub,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: now_val[0],
        )
        mnq = "CON.F.US.MNQ.Z26"
        try:
            await svc.set_active_contracts([CID_MES])
            await svc.start()
            for _ in range(50):
                if hub.is_connected and svc._contracts[CID_MES].state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert svc._contracts[CID_MES].state == "LIVE"

            now_val[0] = BASE_T + 150
            await svc.subscribe_ws_contract(mnq)
            for _ in range(50):
                if (
                    mnq in svc._contracts
                    and svc._contracts[mnq].state == "LIVE"
                    and (svc._recovery_task is None or svc._recovery_task.done())
                ):
                    break
                await asyncio.sleep(0.01)
            assert svc._contracts[mnq].state == "LIVE"

            now_val[0] = BASE_T + 160
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 160, 200.0, 1.0, "GatewayTrade", 1.0)
            )
            now_val[0] = BASE_T + 190
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 190, 201.0, 1.0, "GatewayTrade", 1.1)
            )
            now_val[0] = BASE_T + 245
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 245, 202.0, 1.0, "GatewayTrade", 1.2)
            )
            if svc._live_tick_tasks:
                await asyncio.gather(*list(svc._live_tick_tasks))

            mnq_bars = await parquet.read_bars(
                StorageDatasetKey("topstepx", False, mnq, "1m"),
                start_ts=BASE_T,
                end_ts=BASE_T + 300,
                include_provisional=False,
            )
            assert all(b.time != BASE_T + 120 for b in mnq_bars)
            assert any(b.time == BASE_T + 180 and b.close == 201.0 for b in mnq_bars)
            assert svc._contracts[mnq].state == "DEGRADED"
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_htf_volume_overflow_from_1m_bars_quarantined_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: P1-003/P2-OHLCV-003 quarantines invalid aggregated HTF bars & degrades feed.
    Defect caught: silent HTF drop allowing LIVE/watermark advance over overflowed 5m bucket.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    overflow_active = [True]

    class _Overflow1mRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            vol = 1e308 if overflow_active[0] else 10.0
            bars = [
                {
                    "t": t0 + i * 60,
                    "o": 100.0,
                    "h": 102.0,
                    "l": 99.0,
                    "c": 101.0,
                    "v": vol,
                }
                for i in range(5)
                if s_ts <= t0 + i * 60 < e_ts
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _Overflow1mRest(),
            parquet,
            sqlite,
            recovery_timeframes=("1m", "5m"),
            initial_backfill_bars=5,
            clock=lambda: t0 + 310,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            await svc._run_recovery(now_utc=t0 + 310)
            snap = svc.get_status_snapshot()
            assert snap["contracts"][CID_MES]["state"] == "DEGRADED"
            assert snap["live_alerts_enabled"] is False

            key_5m = StorageDatasetKey("topstepx", False, CID_MES, "5m")
            wm_5m = await sqlite.get_watermark(key_5m)
            assert wm_5m is None
            assert await sqlite.is_interval_complete(key_5m, t0, t0 + 300) is False
            issues = await sqlite.list_quality_issues(key_5m)
            assert any(
                i["interval_start"] == t0 and "volume" in str(i["issue_code"]) for i in issues
            )
            bars_5m = await parquet.read_bars(
                key_5m, start_ts=t0, end_ts=t0 + 300, include_provisional=True
            )
            assert bars_5m == []

            # Repaired 1m data heals HTF bar and SQLite CORRUPTED coverage to COMPLETE (P1-002).
            overflow_active[0] = False
            await svc._run_recovery(now_utc=t0 + 310)
            healed_snap = svc.get_status_snapshot()
            assert healed_snap["contracts"][CID_MES]["state"] == "LIVE"
            assert healed_snap["live_alerts_enabled"] is True
            assert await sqlite.is_interval_complete(key_5m, t0, t0 + 300) is True
            cov_5m = await sqlite.get_coverage_intervals(key_5m)
            assert cov_5m and all(row.status == "COMPLETE" for row in cov_5m)
            healed_wm = await sqlite.get_watermark(key_5m)
            assert healed_wm is not None and healed_wm.last_closed_bar_time == t0
            healed_bars = await parquet.read_bars(key_5m, start_ts=t0, end_ts=t0 + 300)
            assert len(healed_bars) == 1
            assert healed_bars[0].time == t0 and healed_bars[0].volume == 50.0
        finally:
            await svc.stop()
            sqlite.close()


def test_f03_htf_open_bar_preserves_earliest_tick_ts_after_rebuild() -> None:
    """Contract: P1-002 preserves open_1m first_ts in HTF _open_bar_ts_bounds on rebuild.
    Defect caught: sub-second out-of-order tick failing to update rebuilt HTF open price.
    """
    from app.engine.bar_aggregator import NormalizedTick

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    agg = BarAggregator(timeframes=("1m", "5m"))
    recon_1m = agg.reconcile_recovery(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="1m",
        historical_bars=[],
        buffered_ticks=[NormalizedTick(CID_MES, False, t0 + 5.9, 110.0, 1.0)],
        now_utc=t0 + 10,
    )
    assert recon_1m.open_bar is not None
    agg.reconcile_from_1m_bars(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="5m",
        closed_1m_bars=[],
        open_1m_bar=recon_1m.open_bar.bar,
        now_utc=t0 + 10,
    )
    res = agg.process_tick(
        NormalizedTick(CID_MES, False, t0 + 5.1, 100.0, 1.0),
        now_utc=t0 + 10,
    )
    bars_by_tf = {evt.timeframe: evt.bar for evt in res.events}
    assert bars_by_tf["1m"].open == 100.0
    assert bars_by_tf["5m"].open == 100.0


def test_f03_websockets_debug_frame_long_jwt_exceeding_max_log_size_redacts_fragments(
    tmp_path: Path,
) -> None:
    """Contract: P2-REDACTION-002 redacts Frame payloads before Frame.__str__ truncates them.
    Defect caught: >300-byte JWT inside Frame.__str__ leaking prefix/suffix fragments in logs.
    """
    from websockets.frames import Frame, Opcode

    hdr = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    pay = "eyJzdWIiOiJ1c2VyMTIzIiwicm9sZSI6InRyYWRlciIsImV4cCI6MTk5OTk5OTk5OSwibm9uY2UiOiJhYmMxIn0"
    sig = "A" * 280 + "TailSecretSignature12345"
    long_jwt = f"{hdr}.{pay}.{sig}"
    assert len(long_jwt) > 350

    settings = Settings(data_dir=tmp_path / "data")
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result=long_jwt),
        contracts=[CID_MES],
    )
    hub._recent_tokens.append(long_jwt)

    captured: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _ListHandler()
    prev_level = hub._ws_logger.level
    hub._ws_logger.setLevel(logging.DEBUG)
    hub._ws_logger.addHandler(handler)
    try:
        frame = Frame(
            Opcode.TEXT,
            f'{{"type":7,"error":"unauthorized {long_jwt}"}}'.encode(),
        )
        hub._ws_logger.debug("< %s", frame)
        hub._ws_logger.debug("< %s", str(frame))
        assert len(captured) == 2
        for line in captured:
            assert long_jwt not in line
            assert hdr not in line
            assert "TailSecretSignature12345" not in line
            assert "***REDACTED***" in line
    finally:
        hub._ws_logger.removeHandler(handler)
        hub._ws_logger.setLevel(prev_level)


@pytest.mark.asyncio
async def test_f03_superseded_recovery_preserves_already_drained_ticks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 preserves already-drained pending_by_contract ticks when recovery cancels.
    Defect caught: local pending_by_contract losing drained ticks of another contract on cancel.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    mnq = "CON.F.US.MNQ.Z26"
    es = "CON.F.US.ES.Z26"
    now_t = BASE_T + 20
    mes_post_drain = asyncio.Event()
    release_mes = asyncio.Event()

    class _BarrierRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            cid = kw.get("contract_id")
            if cid == CID_MES:
                bars = [
                    {"t": BASE_T - 60, "o": 5000.0, "h": 5001.0, "l": 4999.0, "c": 5000.0, "v": 2}
                ]
                return type(
                    "R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False}
                )()
            bars = [{"t": BASE_T - 60, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1}]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars

    async def _gated_upsert(*args: Any, **kwargs: Any) -> Any:
        key = args[0]
        if key.contract_id == CID_MES and not mes_post_drain.is_set():
            mes_post_drain.set()
            await asyncio.wait_for(release_mes.wait(), timeout=2.0)
        return await orig_upsert(*args, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _BarrierRest(),
            parquet,
            sqlite,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: now_t,
        )
        try:
            await svc.set_active_contracts([CID_MES, mnq])
            svc._last_connected_utc = BASE_T - 30
            svc._transition_all_contracts("RECOVERING")
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 5, 20050.0, 7.0, "GatewayTrade", 1.0)
            )
            svc._recovery_queue.put_nowait(
                SignalRTickEvent(CID_MES, False, BASE_T - 60, 5000.0, 2.0, "GatewayTrade", 0.5)
            )
            t1 = svc._start_recovery_task(now_utc=now_t)
            await asyncio.wait_for(mes_post_drain.wait(), timeout=2.0)
            assert mnq in svc._pending_by_contract

            svc.register_contract(es, live_mode=False)
            svc._bump_generation_and_cancel_live_tasks()
            t2 = svc._start_recovery_task(now_utc=now_t)
            release_mes.set()
            await asyncio.gather(t1, return_exceptions=True)
            await asyncio.wait_for(t2, timeout=2.0)

            open_mnq = svc._aggregator.get_open_bar(mnq, False, "1m")
            assert open_mnq is not None
            assert open_mnq.high == 20050.0
            assert open_mnq.volume == 7.0
        finally:
            release_mes.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_live_mode_switch_cancels_inflight_ws_factory_dial_and_isolates_ticks(
    tmp_path: Path,
) -> None:
    """Contract: P2-001 cancels in-flight _ws_factory dial when live_mode switches mid-dial.
    Defect caught: Sim ws_factory completing after Sim->Live switch and emitting ticks as Live.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    dial_entered = asyncio.Event()
    dial_cancelled = asyncio.Event()
    release_old_dial = asyncio.Event()
    dials: list[tuple[str, _MockSignalRTransport]] = []
    emitted: list[Any] = []

    async def ws_factory(url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        tr = _MockSignalRTransport()
        dials.append((url, tr))
        if len(dials) == 1:
            dial_entered.set()
            try:
                await asyncio.wait_for(release_old_dial.wait(), timeout=2.0)
            except asyncio.CancelledError:
                dial_cancelled.set()
                raise
        return tr

    def token_prov() -> Any:
        mode_tag = "live-jwt" if hub.live_mode else "sim-jwt"
        return asyncio.sleep(0, result=mode_tag)

    hub = SignalRMarketHub(
        settings,
        token_provider=token_prov,
        contracts=[CID_MES],
        live_mode=False,
        ws_factory=ws_factory,
        on_event=lambda evt: emitted.append(evt),
    )
    try:
        hub.start()
        await asyncio.wait_for(dial_entered.wait(), timeout=2.0)
        assert "access_token=sim-jwt" in dials[0][0]

        await hub.update_contracts([CID_MES], live_mode=True)
        for _ in range(50):
            if hub.is_connected and len(dials) >= 2:
                break
            await asyncio.sleep(0.01)

        assert dial_cancelled.is_set()
        assert dials[0][1].sent_frames == []
        assert len(dials) >= 2
        assert "access_token=live-jwt" in dials[-1][0]
        await dials[-1][1].push_frame(
            {
                "type": 1,
                "target": "GatewayTrade",
                "arguments": [
                    CID_MES,
                    {"price": 5000.0, "volume": 1.0, "timestamp": BASE_T + 10},
                ],
            }
        )
        await asyncio.sleep(0.02)
        tick_evts = [e for e in emitted if isinstance(e, SignalRTickEvent)]
        assert tick_evts and all(e.live_mode is True for e in tick_evts)
    finally:
        release_old_dial.set()
        await hub.stop()


def test_f03_websocket_server_handshake_debug_log_redacts_session_cookie() -> None:
    """Contract: P2-003 redacts Cookie/Set-Cookie and topstep_session in WS DEBUG logs.
    Defect caught: Uvicorn websockets.server '< %s: %s' ('Cookie', 'topstep_session=...') leak.
    """
    from app.security import install_global_log_redaction

    install_global_log_redaction()
    ws_srv_logger = logging.getLogger("websockets.server")
    captured: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _ListHandler()
    prev_level = ws_srv_logger.level
    ws_srv_logger.setLevel(logging.DEBUG)
    ws_srv_logger.addHandler(handler)
    sess_val = "topstep_session=sess_secret_987654321; theme=dark"
    try:
        ws_srv_logger.debug("< %s: %s", "Cookie", sess_val)
        ws_srv_logger.debug("< %s: %s", "Cookie", "opaque_cookie_abc999")
        ws_srv_logger.debug("> Set-Cookie: %s", sess_val)
        assert len(captured) == 3
        for line in captured:
            assert "sess_secret_987654321" not in line
            assert "opaque_cookie_abc999" not in line
            assert "***REDACTED***" in line
    finally:
        ws_srv_logger.removeHandler(handler)
        ws_srv_logger.setLevel(prev_level)


@pytest.mark.asyncio
async def test_f03_multi_timeframe_recovery_without_1m_replays_buffered_ticks_to_all_tfs(
    tmp_path: Path,
) -> None:
    """Contract: P1-002 replays the same buffered tick snapshot across all non-1m timeframes.
    Defect caught: first timeframe popping pending_by_contract so later timeframes miss open ticks.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "15m")
    rest_entered = asyncio.Event()
    rest_release = asyncio.Event()

    class _BlockingMultiTfRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            tf = kw.get("timeframe")
            if tf == "5m" and not rest_entered.is_set():
                rest_entered.set()
                await asyncio.wait_for(rest_release.wait(), timeout=2.0)
            step = 300 if tf == "5m" else 900
            bars = [
                {
                    "t": t0 - step,
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.0,
                    "v": 5.0,
                }
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        agg = BarAggregator(timeframes=("5m", "15m"))
        svc = LiveFeedService(
            settings,
            auth,
            _BlockingMultiTfRest(),
            parquet,
            sqlite,
            aggregator=agg,
            recovery_timeframes=("5m", "15m"),
            initial_backfill_bars=1,
            clock=lambda: t0 + 45,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = t0 - 30
            svc._transition_all_contracts("RECOVERING")
            rec_task = asyncio.create_task(svc._run_recovery(now_utc=t0 + 45))
            await asyncio.wait_for(rest_entered.wait(), timeout=2.0)
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 15, 105.5, 9.0, "GatewayTrade", 1.0)
            )
            rest_release.set()
            await asyncio.wait_for(rec_task, timeout=2.0)

            open_5m = agg.get_open_bar(CID_MES, False, "5m")
            open_15m = agg.get_open_bar(CID_MES, False, "15m")
            assert open_5m is not None and open_5m.close == 105.5 and open_5m.volume == 9.0
            assert open_15m is not None and open_15m.close == 105.5 and open_15m.volume == 9.0
        finally:
            rest_release.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_malformed_tick_prior_to_backfill_window_preserved_when_window_heals(
    tmp_path: Path,
) -> None:
    """Contract: P2-002 only clears healed gap buckets within [backfill_start, closed_end).
    Defect caught: gap < backfill_start injected during REST await being wiped by window heal.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    now_t = BASE_T + 125
    rest_entered = asyncio.Event()
    rest_release = asyncio.Event()

    class _BarrierCleanRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            if not rest_entered.is_set():
                rest_entered.set()
                await asyncio.wait_for(rest_release.wait(), timeout=2.0)
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            bars = [
                {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 2.0}
                for ts in range(s_ts, e_ts, 60)
                if ts < (now_t - (now_t % 60))
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _BarrierCleanRest(),
            parquet,
            sqlite,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: now_t,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = BASE_T - 30
            svc._transition_all_contracts("RECOVERING")
            rec_task = asyncio.create_task(svc._run_recovery(now_utc=now_t))
            await asyncio.wait_for(rest_entered.wait(), timeout=2.0)

            old_gap_ts = BASE_T - 600
            svc.handle_hub_event(
                SignalRMalformedTickEvent(
                    contract_id=CID_MES,
                    live_mode=False,
                    reason="corrupt historical price",
                    issue_code="invalid_tick",
                    raw_summary="{}",
                    bar_time=old_gap_ts,
                    received_monotonic=1.0,
                )
            )
            rest_release.set()
            await asyncio.wait_for(rec_task, timeout=2.0)

            c_state = svc._contracts[CID_MES]
            assert old_gap_ts in c_state.unhealed_gap_buckets.get("1m", set())
            assert c_state.state == "DEGRADED"
            assert svc.live_alerts_enabled is False
        finally:
            rest_release.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_signalr_huge_integer_timestamp_quarantined_without_overflow_error(
    tmp_path: Path,
) -> None:
    """Contract: P1-003 converts huge integer timestamps cleanly to SignalRMalformedTickEvent.
    Defect caught: 10**400 JSON int raising OverflowError and tearing down the SignalR session.
    """
    from app.engine.bar_aggregator import NormalizedTick

    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    tr = _MockSignalRTransport()
    emitted: list[Any] = []

    async def _ws_factory(_u: str, _h: dict[str, str]) -> _MockSignalRTransport:
        return tr

    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
        contracts=[CID_MES],
        ws_factory=_ws_factory,
        on_event=lambda evt: emitted.append(evt),
    )
    try:
        hub.start()
        for _ in range(50):
            if hub.is_connected:
                break
            await asyncio.sleep(0.01)
        assert hub.is_connected

        await tr.push_frame(
            {
                "type": 1,
                "target": "GatewayTrade",
                "arguments": [CID_MES, {"price": 5000.0, "volume": 1.0, "timestamp": 10**400}],
            }
        )
        await asyncio.sleep(0.02)
        malformed = [e for e in emitted if isinstance(e, SignalRMalformedTickEvent)]
        assert len(malformed) == 1
        assert malformed[0].contract_id == CID_MES
        assert hub.is_connected

        agg = BarAggregator(timeframes=("1m",))
        ok_res = agg.process_tick(
            NormalizedTick(CID_MES, False, BASE_T + 5, 5000.0, 2.0),
            now_utc=BASE_T + 10,
        )
        assert ok_res.accepted is True
        bad_res = agg.process_tick(
            NormalizedTick(CID_MES, False, 10**400, 9999.0, 99.0),
            now_utc=BASE_T + 10,
        )
        assert bad_res.accepted is False
        assert agg.get_open_bar(CID_MES, False, "1m") == OHLCVBar(
            time=BASE_T,
            open=5000.0,
            high=5000.0,
            low=5000.0,
            close=5000.0,
            volume=2.0,
            is_closed=False,
        )
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_f03_watchdog_expired_heartbeat_prevents_bar_close_and_signal(
    tmp_path: Path,
) -> None:
    """Contract: P1-001 checks heartbeat timeout BEFORE watchdog bar flush/persistence/signal.
    Defect caught: expired heartbeat letting watchdog finalize & emit tag=None stale bar.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_enabled=True,
        signalr_heartbeat_timeout_sec=0.5,
    )
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    now_t = [BASE_T + 10]
    emitted: list[tuple[str, int, str | None]] = []

    class _RestEmpty:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            return type("R", (), {"raw_bars": [], "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _RestEmpty(),
            parquet,
            sqlite,
            recovery_timeframes=("1m",),
            on_bar_signal=lambda evt, tag: emitted.append((evt.timeframe, evt.bar.time, tag)),
            clock=lambda: now_t[0],
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._contracts[CID_MES].state = "LIVE"
            svc._feed_state = "LIVE"
            svc._last_connected_utc = BASE_T - 30

            await svc._process_live_tick(
                SignalRTickEvent(CID_MES, False, BASE_T + 5, 100.0, 2.0, "GatewayTrade", 1.0),
                now_utc=BASE_T + 10,
            )
            assert svc._aggregator.get_open_bar(CID_MES, False, "1m") is not None

            expired_mono = time.monotonic() - 5.0
            svc._last_event_monotonic = expired_mono
            svc._hub._last_event_monotonic = expired_mono
            now_t[0] = BASE_T + 70

            await svc.run_watchdog_tick()

            assert svc.feed_state == "DISCONNECTED"
            assert emitted == []
            persisted = await parquet.read_bars(
                StorageDatasetKey("topstepx", False, CID_MES, "1m"),
                start_ts=BASE_T - 60,
                end_ts=BASE_T + 120,
                include_provisional=True,
            )
            assert persisted == []
            assert svc._aggregator.flush_closed_bars(BASE_T + 70, contract_id=CID_MES) == []
        finally:
            await svc.stop()
            sqlite.close()


def test_f03_non_loopback_plaintext_ws_rtc_url_rejected_before_token_or_ws(
    tmp_path: Path,
) -> None:
    """Contract: P2-002 rejects non-loopback ws:// Market Hub URLs before token/WS calls.
    Defect caught: plaintext ws:// sending TopstepX JWTs in URL/headers over network.
    """
    token_called = False
    ws_called = False

    def _token_prov() -> Any:
        nonlocal token_called
        token_called = True
        return asyncio.sleep(0, result="secret-jwt")

    async def _ws_factory(_u: str, _h: dict[str, str]) -> _MockSignalRTransport:
        nonlocal ws_called
        ws_called = True
        return _MockSignalRTransport()

    with pytest.raises(ValueError, match="wss://"):
        Settings(data_dir=tmp_path / "data", topstepx_rtc_url="ws://rtc.topstepx.com/hubs/market")

    bad_settings = Settings(data_dir=tmp_path / "data").model_copy(
        update={"topstepx_rtc_url": "ws://rtc.topstepx.com/hubs/market"}
    )
    with pytest.raises(ValueError, match="wss://"):
        SignalRMarketHub(
            bad_settings,
            token_provider=_token_prov,
            contracts=[CID_MES],
            ws_factory=_ws_factory,
        )
    assert token_called is False
    assert ws_called is False

    loopback_s = Settings(
        data_dir=tmp_path / "data",
        topstepx_rtc_url="ws://127.0.0.1:8080/hubs/market",
    )
    assert loopback_s.topstepx_rtc_url == "ws://127.0.0.1:8080/hubs/market"


@pytest.mark.asyncio
async def test_f03_unaligned_rest_bar_timestamp_quarantined_and_degrades(
    tmp_path: Path,
) -> None:
    """Contract: P1-003 quarantines off-boundary REST timestamps and degrades the contract.
    Defect caught: aligned 1m bars + extra off-boundary bar (t+1) corrupting 5m volume/Parquet.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")

    class _OffBoundaryRest:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            bars = [
                {
                    "t": t0 + i * 60,
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.5,
                    "v": 2.0,
                }
                for i in range(5)
            ]
            bars.append(
                {"t": t0 + 1, "o": 50.0, "h": 250.0, "l": 10.0, "c": 120.0, "v": 999.0}
            )
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _OffBoundaryRest(),
            parquet,
            sqlite,
            recovery_timeframes=("1m", "5m"),
            initial_backfill_bars=5,
            clock=lambda: t0 + 310,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            await svc._run_recovery(now_utc=t0 + 310)

            assert svc.feed_state == "DEGRADED"
            assert svc.live_alerts_enabled is False
            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            issues = await sqlite.list_quality_issues(key_1m)
            assert any(i["issue_code"] == "unaligned_bar_timestamp" for i in issues)

            bars_1m = await parquet.read_bars(
                key_1m, start_ts=t0, end_ts=t0 + 300, include_provisional=True
            )
            assert all(b.time == t0 + i * 60 for i, b in enumerate(bars_1m))
            assert all(b.time != t0 + 1 for b in bars_1m)
            bars_5m = await parquet.read_bars(
                StorageDatasetKey("topstepx", False, CID_MES, "5m"),
                start_ts=t0,
                end_ts=t0 + 300,
                include_provisional=True,
            )
            assert bars_5m == []
            assert await sqlite.get_watermark(key_1m) is None
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_superseded_non_1m_recovery_does_not_double_apply_buffered_tick_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-002 avoids re-applying buffered ticks to TFs already reconciled before cancel.
    Defect caught: cancelling ('5m','15m') recovery after 5m reconcile doubling 5m volume (9->18).
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "15m")
    upsert_5m_entered = asyncio.Event()
    release_upsert = asyncio.Event()

    class _Rest5m15m:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            step = 300 if kw.get("timeframe") == "5m" else 900
            bars = [
                {"t": t0 - step, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 4.0}
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars

    async def _gated_upsert(*args: Any, **kwargs: Any) -> Any:
        key = args[0]
        if key.timeframe == "5m" and not upsert_5m_entered.is_set():
            upsert_5m_entered.set()
            await asyncio.wait_for(release_upsert.wait(), timeout=2.0)
        return await orig_upsert(*args, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        agg = BarAggregator(timeframes=("5m", "15m"))
        svc = LiveFeedService(
            settings,
            auth,
            _Rest5m15m(),
            parquet,
            sqlite,
            aggregator=agg,
            recovery_timeframes=("5m", "15m"),
            initial_backfill_bars=1,
            clock=lambda: t0 + 45,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = t0 - 30
            svc._transition_all_contracts("RECOVERING")
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 15, 105.5, 9.0, "GatewayTrade", 1.0)
            )
            t1 = svc._start_recovery_task(now_utc=t0 + 45)
            await asyncio.wait_for(upsert_5m_entered.wait(), timeout=2.0)

            svc._bump_generation_and_cancel_live_tasks()
            t2 = svc._start_recovery_task(now_utc=t0 + 45)
            release_upsert.set()
            await asyncio.gather(t1, return_exceptions=True)
            await asyncio.wait_for(t2, timeout=2.0)

            open_5m = agg.get_open_bar(CID_MES, False, "5m")
            open_15m = agg.get_open_bar(CID_MES, False, "15m")
            assert open_5m is not None and open_5m.volume == 9.0
            assert open_15m is not None and open_15m.volume == 9.0
        finally:
            release_upsert.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_cancelled_recovery_maintains_pending_tick_count_within_maxsize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P2-001 keeps _pending_recovery_ticks_count consistent on recovery cancel.
    Defect caught: finally resetting count=0 while ticks stay in _pending_by_contract.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_enabled=True,
        signalr_recovery_queue_maxsize=4,
    )
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    mnq = "CON.F.US.MNQ.Z26"
    mes_gate = asyncio.Event()
    release_mes = asyncio.Event()

    class _StubRest:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            bars = [{"t": BASE_T - 60, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars

    async def _gated_upsert(*args: Any, **kwargs: Any) -> Any:
        if args[0].contract_id == CID_MES and not mes_gate.is_set():
            mes_gate.set()
            await asyncio.wait_for(release_mes.wait(), timeout=2.0)
        return await orig_upsert(*args, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _StubRest(),
            parquet,
            sqlite,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: BASE_T + 20,
        )
        try:
            await svc.set_active_contracts([CID_MES, mnq])
            svc._last_connected_utc = BASE_T - 30
            svc._transition_all_contracts("RECOVERING")
            for idx in range(4):
                svc.handle_hub_event(
                    SignalRTickEvent(
                        mnq, False, BASE_T + idx + 1, 20000.0, 1.0, "GatewayTrade", float(idx)
                    )
                )
            t1 = svc._start_recovery_task(now_utc=BASE_T + 20)
            await asyncio.wait_for(mes_gate.wait(), timeout=2.0)

            t1.cancel()
            release_mes.set()
            await asyncio.gather(t1, return_exceptions=True)

            assert len(svc._pending_by_contract.get(mnq, ())) == 4
            assert svc._pending_recovery_ticks_count == 4
            assert (
                svc._recovery_queue.qsize() + svc._pending_recovery_ticks_count
                <= settings.signalr_recovery_queue_maxsize
            )
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 10, 20001.0, 1.0, "GatewayTrade", 10.0)
            )
            assert svc._recovery_overflowed is True
            assert svc._recovery_queue.qsize() == 0
        finally:
            release_mes.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_delayed_subscribe_ack_across_minute_boundary_marks_new_minute_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-F03-001 updates subscribed_at_utc AFTER subscribe ACK completes.
    Defect caught: subscribe at 14:00:59 finishing ACK at 14:01:01 closing partial 14:01 bar.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_enabled=True,
        upstream_connect_timeout_sec=1.0,
    )
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    now_val = [BASE_T + 15]
    mnq = "CON.F.US.MNQ.Z26"

    class _RestStub:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw.get("start_ts", BASE_T))
            e_ts = int(kw.get("end_ts", BASE_T))
            cutoff = now_val[0] - (now_val[0] % 60)
            bars = [
                {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 2.0}
                for ts in range(s_ts - (s_ts % 60), e_ts, 60)
                if ts < cutoff
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    tr = _MockSignalRTransport()
    ack_held = asyncio.Event()
    held_inv_ids: list[str] = []
    orig_send = tr.send

    async def _defer_ack_send(data: str) -> None:
        held_frames: list[str] = []
        for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
            if not chunk.strip():
                continue
            frame = json.loads(chunk)
            if (
                frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                and mnq in frame.get("arguments", ())
                and frame.get("invocationId") is not None
            ):
                tr.sent_frames.append(frame)
                held_inv_ids.append(str(frame["invocationId"]))
                ack_held.set()
            else:
                held_frames.append(chunk + SIGNALR_RECORD_SEPARATOR)
        if held_frames:
            await orig_send("".join(held_frames))

    monkeypatch.setattr(tr, "send", _defer_ack_send)

    async def _ws_factory(_u: str, _h: dict[str, str]) -> _MockSignalRTransport:
        return tr

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        hub = SignalRMarketHub(
            settings,
            token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
            contracts=[CID_MES],
            ws_factory=_ws_factory,
        )
        svc = LiveFeedService(
            settings,
            auth,
            _RestStub(),
            parquet,
            sqlite,
            signalr_hub=hub,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: now_val[0],
        )
        try:
            await svc.set_active_contracts([CID_MES])
            await svc.start()
            for _ in range(50):
                if hub.is_connected and svc._contracts[CID_MES].state == "LIVE":
                    break
                await asyncio.sleep(0.01)
            assert svc._contracts[CID_MES].state == "LIVE"

            now_val[0] = BASE_T + 59
            sub_task = asyncio.create_task(svc.subscribe_ws_contract(mnq))
            await asyncio.wait_for(ack_held.wait(), timeout=2.0)
            await asyncio.sleep(0.01)

            now_val[0] = BASE_T + 61
            for inv_id in held_inv_ids:
                await tr.push_frame({"type": 3, "invocationId": inv_id})
            await asyncio.wait_for(sub_task, timeout=2.0)
            now_val[0] = BASE_T + 62
            if svc._recovery_task is not None:
                await asyncio.wait_for(svc._recovery_task, timeout=2.0)
            assert svc._contracts[mnq].state == "LIVE"

            now_val[0] = BASE_T + 70
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 70, 200.0, 1.0, "GatewayTrade", 1.0)
            )
            now_val[0] = BASE_T + 125
            svc.handle_hub_event(
                SignalRTickEvent(mnq, False, BASE_T + 125, 201.0, 1.0, "GatewayTrade", 1.1)
            )
            if svc._live_tick_tasks:
                await asyncio.gather(*list(svc._live_tick_tasks))

            key_mnq = StorageDatasetKey("topstepx", False, mnq, "1m")
            mnq_bars = await parquet.read_bars(
                key_mnq, start_ts=BASE_T, end_ts=BASE_T + 180, include_provisional=False
            )
            assert all(b.time != BASE_T + 60 for b in mnq_bars)
            wm = await sqlite.get_watermark(key_mnq)
            assert wm is not None and wm.last_closed_bar_time < BASE_T + 60
            assert svc._contracts[mnq].state == "DEGRADED"
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_non_1m_recovery_heals_malformed_tick_and_overflow_back_to_live(
    tmp_path: Path,
) -> None:
    """Contract: P1-F03-002 records malformed/overflow gaps on configured TFs when 1m disabled.
    Defect caught: hardcoding '1m' gap key leaving ('5m','15m') stuck DEGRADED forever.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "15m")
    now_val = [t0 + 60]

    class _CompleteRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            tf = str(kw.get("timeframe"))
            step = 300 if tf == "5m" else 900
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            aligned = first_bucket_open_at_or_after(s_ts, tf)
            cutoff = now_val[0] - (now_val[0] % step)
            bars = [
                {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 5.0}
                for ts in range(aligned, e_ts, step)
                if ts < cutoff
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        agg = BarAggregator(timeframes=("5m", "15m"))
        svc = LiveFeedService(
            settings,
            auth,
            _CompleteRest(),
            parquet,
            sqlite,
            aggregator=agg,
            recovery_timeframes=("5m", "15m"),
            initial_backfill_bars=1,
            clock=lambda: now_val[0],
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = t0 - 30
            await svc.trigger_recovery(now_utc=now_val[0])
            assert svc.feed_state == "LIVE"

            svc.handle_hub_event(
                SignalRMalformedTickEvent(
                    contract_id=CID_MES,
                    live_mode=False,
                    reason="bad trade price",
                    issue_code="invalid_tick",
                    raw_summary="{}",
                    bar_time=t0 + 30,
                    received_monotonic=1.0,
                )
            )
            c_state = svc._contracts[CID_MES]
            assert c_state.state == "DEGRADED"
            assert "1m" not in c_state.unhealed_gap_buckets
            assert t0 in c_state.unhealed_gap_buckets.get("5m", set())
            assert t0 in c_state.unhealed_gap_buckets.get("15m", set())
            assert svc.live_alerts_enabled is False

            now_val[0] = t0 + 905
            await svc.trigger_recovery(now_utc=now_val[0])
            assert c_state.state == "LIVE"
            assert c_state.unhealed_gap_buckets == {}
            assert c_state.unhealed_gap_starts == {}
            assert c_state.unresolved_gaps == 0
            assert svc.feed_state == "LIVE"
            assert svc.live_alerts_enabled is True

            svc._record_recovery_overflow(
                c_state,
                tick=SignalRTickEvent(CID_MES, False, now_val[0], 101.0, 1.0, "GatewayTrade", 2.0),
                eff_now=now_val[0],
            )
            assert "1m" not in c_state.unhealed_gap_buckets
            assert (t0 + 900) in c_state.unhealed_gap_buckets.get("5m", set())
            assert (t0 + 900) in c_state.unhealed_gap_buckets.get("15m", set())
            now_val[0] = t0 + 1805
            await svc.trigger_recovery(now_utc=now_val[0])
            assert c_state.state == "LIVE"
            assert c_state.unhealed_gap_buckets == {}
            assert svc.live_alerts_enabled is True
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_watchdog_flush_publishes_closed_bar_before_next_open_bar_during_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 publishes watchdog closed bars before awaiting Parquet persistence.
    Defect caught: next-minute live tick publishing open B+1 ahead of closed B during await.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    now_t = [BASE_T + 10]
    published: list[tuple[int, bool]] = []
    upsert_entered = asyncio.Event()
    release_upsert = asyncio.Event()

    broadcaster = WsBroadcaster(client_queue_maxsize=16, send_timeout_sec=1.0)
    monkeypatch.setattr(
        broadcaster,
        "publish_bar",
        lambda event: published.append((event.bar.time, event.bar.is_closed)),
    )

    class _RestEmpty:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            return type("R", (), {"raw_bars": [], "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars

    async def _gated_upsert(*args: Any, **kwargs: Any) -> Any:
        if not upsert_entered.is_set():
            upsert_entered.set()
            await asyncio.wait_for(release_upsert.wait(), timeout=2.0)
        return await orig_upsert(*args, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _RestEmpty(),
            parquet,
            sqlite,
            aggregator=BarAggregator(timeframes=("1m",)),
            broadcaster=broadcaster,
            recovery_timeframes=("1m",),
            clock=lambda: now_t[0],
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._contracts[CID_MES].state = "LIVE"
            svc._feed_state = "LIVE"
            svc._last_connected_utc = BASE_T - 30
            now_mono = time.monotonic()
            svc.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, BASE_T + 5, 100.0, 2.0, "GatewayTrade", now_mono
                )
            )
            await svc.flush_pending_tasks()
            published.clear()

            now_t[0] = BASE_T + 65
            wd_task = asyncio.create_task(svc.run_watchdog_tick())
            await asyncio.wait_for(upsert_entered.wait(), timeout=2.0)

            svc.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, BASE_T + 66, 101.0, 3.0, "GatewayTrade", now_mono + 1.0
                )
            )
            release_upsert.set()
            await asyncio.wait_for(wd_task, timeout=2.0)
            await svc.flush_pending_tasks()

            assert published == [(BASE_T, True), (BASE_T + 60, False)]
        finally:
            release_upsert.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_cancelled_non_1m_recovery_preserves_replay_guard_when_retry_rest_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-002 preserves per-tick TF replay guard across tail drain after REST failure.
    Defect caught: retry REST error before replayed_ticks leaving tail drain to double-add 5m vol.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "15m")
    upsert_5m_entered = asyncio.Event()
    release_upsert = asyncio.Event()
    fail_5m = False

    class _SwitchRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            tf = kw.get("timeframe")
            if fail_5m and tf == "5m":
                raise ConnectionError("5m REST temporarily unreachable")
            step = 300 if tf == "5m" else 900
            bars = [{"t": t0 - step, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 4.0}]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars

    async def _gated_upsert(*args: Any, **kwargs: Any) -> Any:
        if args[0].timeframe == "5m" and not upsert_5m_entered.is_set():
            upsert_5m_entered.set()
            await asyncio.wait_for(release_upsert.wait(), timeout=2.0)
        return await orig_upsert(*args, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        agg = BarAggregator(timeframes=("5m", "15m"))
        svc = LiveFeedService(
            settings,
            auth,
            _SwitchRest(),
            parquet,
            sqlite,
            aggregator=agg,
            recovery_timeframes=("5m", "15m"),
            initial_backfill_bars=1,
            clock=lambda: t0 + 45,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = t0 - 30
            svc._transition_all_contracts("RECOVERING")
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 15, 105.5, 9.0, "GatewayTrade", 1.0)
            )
            t1 = svc._start_recovery_task(now_utc=t0 + 45)
            await asyncio.wait_for(upsert_5m_entered.wait(), timeout=2.0)

            fail_5m = True
            svc._bump_generation_and_cancel_live_tasks()
            t2 = svc._start_recovery_task(now_utc=t0 + 45)
            release_upsert.set()
            await asyncio.gather(t1, return_exceptions=True)
            await asyncio.wait_for(t2, timeout=2.0)

            assert svc.feed_state == "DEGRADED"
            open_5m = agg.get_open_bar(CID_MES, False, "5m")
            open_15m = agg.get_open_bar(CID_MES, False, "15m")
            assert open_5m is not None and open_5m.volume == 9.0
            assert open_15m is not None and open_15m.volume == 9.0
        finally:
            release_upsert.set()
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_watchdog_retry_login_keeps_disconnected_while_signalr_dial_fails(
    tmp_path: Path,
) -> None:
    """Contract: P1-003 keeps feed DISCONNECTED after retry_login until SignalR connected.
    Defect caught: transition to RECOVERING on retry_login getting stuck if WS dial fails.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        signalr_enabled=True,
        signalr_reconnect_initial_delay_sec=0.02,
    )
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    dial_succeeds = False
    dial_attempts = 0

    async def _ws_factory(_u: str, _h: dict[str, str]) -> _MockSignalRTransport:
        nonlocal dial_attempts
        dial_attempts += 1
        if not dial_succeeds:
            raise ConnectionError("rtc dial refused")
        return _MockSignalRTransport()

    class _RestStub:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            bars = [{"t": BASE_T - 60, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 2.0}]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-retried"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        auth._username = "alice"
        auth._api_key = "api-key"
        hub = SignalRMarketHub(
            settings,
            auth_service=auth,
            contracts=[CID_MES],
            ws_factory=_ws_factory,
            reconnect_initial_delay_sec=0.02,
            reconnect_max_delay_sec=0.02,
        )
        svc = LiveFeedService(
            settings,
            auth,
            _RestStub(),
            parquet,
            sqlite,
            signalr_hub=hub,
            recovery_timeframes=("1m",),
            initial_backfill_bars=1,
            clock=lambda: BASE_T + 10,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._running = True
            await svc.run_watchdog_tick()
            await asyncio.sleep(0.05)

            assert auth.is_connected is True
            assert dial_attempts >= 1
            assert svc.feed_state == "DISCONNECTED"
            assert svc._contracts[CID_MES].state == "DISCONNECTED"

            dial_succeeds = True
            for _ in range(50):
                if svc.feed_state == "LIVE":
                    break
                await asyncio.sleep(0.02)
            assert svc.feed_state == "LIVE"
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_signalr_non_dict_element_in_batch_and_scalar_payload_quarantined(
    tmp_path: Path,
) -> None:
    """Contract: P2-R04-001 quarantines non-dict elements/scalars in GatewayTrade/Quote frames.
    Defect caught: silent filtering of non-Mapping items skipping quarantine & DEGRADED state.
    """
    from app.topstep.signalr_hub import parse_market_invocation_events

    batch_frame = {
        "type": 1,
        "target": "GatewayTrade",
        "arguments": [
            CID_MES,
            [
                {"price": 5000.0, "volume": 1.0, "timestamp": BASE_T + 5},
                None,
                {"price": 5001.0, "volume": 2.0, "timestamp": BASE_T + 6},
            ],
        ],
    }
    parsed = parse_market_invocation_events(batch_frame, live_mode=False, received_monotonic=1.0)
    assert len(parsed) == 3
    assert isinstance(parsed[0], SignalRTickEvent)
    assert isinstance(parsed[1], SignalRMalformedTickEvent)
    assert parsed[1].issue_code == "malformed_tick_payload"
    assert isinstance(parsed[2], SignalRTickEvent)

    scalar_frame = {"type": 1, "target": "GatewayQuote", "arguments": [CID_MES, "bad-scalar"]}
    scalar_parsed = parse_market_invocation_events(
        scalar_frame, live_mode=False, received_monotonic=2.0
    )
    assert len(scalar_parsed) == 1
    assert isinstance(scalar_parsed[0], SignalRMalformedTickEvent)
    assert scalar_parsed[0].issue_code == "malformed_tick_payload"

    settings = Settings(data_dir=tmp_path / "data")
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    class _RestEmpty:
        async def retrieve_bars_paginated(self, **_kw: Any) -> Any:
            return type("R", (), {"raw_bars": [], "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        svc = LiveFeedService(
            settings,
            auth,
            _RestEmpty(),
            parquet,
            sqlite,
            recovery_timeframes=("1m",),
            clock=lambda: BASE_T + 10,
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._contracts[CID_MES].state = "LIVE"
            svc._feed_state = "LIVE"
            svc.handle_hub_event(parsed[1])
            await svc.flush_pending_tasks()

            assert svc._contracts[CID_MES].state == "DEGRADED"
            issues = await sqlite.list_quality_issues(
                StorageDatasetKey("topstepx", False, CID_MES, "1m")
            )
            assert any(i["issue_code"] == "malformed_tick_payload" for i in issues)
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_quarantine_delayed_after_quality_issues_cannot_overwrite_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 serializes generation check & coverage write so CORRUPTED never overwrites
    a healed COMPLETE interval after record_quality_issues completes.
    Defect caught: TOCTOU race between generation check and record_coverage(CORRUPTED).
    """
    clock_ref = [BASE_T + 15]
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-cov"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    orig_record_issues = sqlite_store.record_quality_issues
    post_issues_gate = asyncio.Event()
    release_post_issues = asyncio.Event()

    async def after_issues_gate(key: StorageDatasetKey, issues: list[Any]) -> int:
        res = await orig_record_issues(key, issues)
        post_issues_gate.set()
        await asyncio.wait_for(release_post_issues.wait(), timeout=2.0)
        return res

    monkeypatch.setattr(sqlite_store, "record_quality_issues", after_issues_gate)
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-cov")
            rest = TopstepRestClient(settings, auth, client)
            hub = SignalRMarketHub(
                settings,
                auth_service=auth,
                ws_factory=ws_factory,
                contracts=[CID_MES],
            )
            svc = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                signalr_hub=hub,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                await svc.start()
                for _ in range(100):
                    if svc.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert svc.feed_state == "LIVE"

                await transports[0].push_frame(_quote_frame(CID_MES, BASE_T + 20, 5951.0, -3.0))
                await asyncio.wait_for(post_issues_gate.wait(), timeout=2.0)
                assert svc.feed_state == "DEGRADED"

                clock_ref[0] = BASE_T + 65
                await svc.trigger_recovery()
                assert svc.feed_state == "LIVE"

                release_post_issues.set()
                await svc.flush_pending_tasks()
                key = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert await sqlite_store.is_interval_complete(key, BASE_T, BASE_T + 60) is True
                await sqlite_store.record_coverage(
                    key,
                    BASE_T,
                    BASE_T + 60,
                    status="CORRUPTED",
                    quarantined_count=1,
                    skip_if_complete=True,
                )
                assert await sqlite_store.is_interval_complete(key, BASE_T, BASE_T + 60) is True
                assert svc.feed_state == "LIVE"
            finally:
                release_post_issues.set()
                await svc.stop()
    finally:
        sqlite_store.close()


@pytest.mark.asyncio
async def test_f03_tail_drain_sorts_out_of_order_ticks_by_market_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-002 sorts tail-drained buffered ticks by market timestamp before process_tick.
    Defect caught: B+65 arriving before B+5 dropping B tick silently while declaring LIVE.
    """
    b0 = BASE_T
    b1 = BASE_T + 60
    b2 = BASE_T + 120
    b3 = BASE_T + 180
    clock_ref = [b2 + 20]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ooo-tail"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(
                payload,
                overrides={b0: _wire_bar(b0, 100.0, 102.0, 99.0, 101.0, 5.0)},
            )
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_recovery_timeout_sec=5.0)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        orig_upsert = parquet_store.upsert_bars
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-ooo-tail")
            rest = TopstepRestClient(settings, auth, client)
            agg = BarAggregator(timeframes=("1m",))
            svc = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                aggregator=agg,
                recovery_timeframes=("1m",),
                clock=lambda: clock_ref[0],
            )
            await svc.set_active_contracts([CID_MES], live_mode=False)
            svc._transition_all_contracts("RECOVERING")
            svc._last_connected_utc = b1 + 30

            injected = False

            async def hook_upsert(*args: Any, **kwargs: Any) -> Any:
                nonlocal injected
                bars_arg = args[1] if len(args) > 1 else []
                if not injected and any(getattr(b, "time", None) == b1 for b in bars_arg):
                    injected = True
                    clock_ref[0] = b3 + 20
                    svc.handle_hub_event(
                        SignalRTickEvent(CID_MES, False, b3 + 5, 115.0, 3.0, "GatewayTrade", 1.0)
                    )
                    svc.handle_hub_event(
                        SignalRTickEvent(CID_MES, False, b2 + 5, 110.0, 2.0, "GatewayTrade", 2.0)
                    )
                return await orig_upsert(*args, **kwargs)

            monkeypatch.setattr(parquet_store, "upsert_bars", hook_upsert)

            await svc.trigger_recovery(now_utc=b2 + 20)
            assert injected is True
            assert svc.feed_state == "LIVE"
            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
            persisted = await parquet_store.read_bars(key_1m, start_ts=b0, end_ts=b3)
            assert [bar.time for bar in persisted] == [b0, b1, b2]
            assert persisted[2].volume == 2.0
            wm_1m = await sqlite_store.get_watermark(key_1m)
            assert wm_1m is not None and wm_1m.last_closed_bar_time == b2
            assert agg.get_open_bar(CID_MES, False, "1m") == OHLCVBar(
                b3, 115.0, 115.0, 115.0, 115.0, 3.0, is_closed=False
            )
    finally:
        sqlite_store.close()


@pytest.mark.asyncio
async def test_f03_default_ws_connect_disables_http_redirects_against_hub_downgrade(
    tmp_path: Path,
) -> None:
    """Contract: PASS2-002 disables HTTP 3xx redirects in _default_ws_connect.
    Defect caught: /hubs/market 302 redirect hijacking authenticated WebSocket connections.
    """
    import websockets

    settings = Settings(data_dir=tmp_path / "data")
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="jwt-redirect-test"),
        contracts=[CID_MES],
    )

    requested_paths: list[str] = []

    async def _handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            parts = req_line.decode("ascii", errors="replace").split()
            if len(parts) >= 2:
                requested_paths.append(parts[1])
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if line in {b"\r\n", b"\n", b""}:
                    break
            writer.write(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /hubs/other\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus):
            await hub._default_ws_connect(
                f"ws://127.0.0.1:{port}/hubs/market?access_token=jwt-redirect-test",
                {"Authorization": "Bearer jwt-redirect-test"},
            )
        assert requested_paths == ["/hubs/market?access_token=jwt-redirect-test"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_f03_late_tick_after_local_watchdog_flush_triggers_gap_and_rest_reconcile(
    tmp_path: Path,
) -> None:
    """Contract: PASS2-001 surfaces late ticks for locally-closed (unconfirmed) bars as gaps.
    Defect caught: watchdog flush advancing _last_closed_times and silently dropping T+59.9 trade.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    now_t = [t0 + 10]
    omit_t0 = [False]

    class _RestAuthoritative:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = now_t[0] - (now_t[0] % 60)
            bars = []
            for ts in range(s_ts - (s_ts % 60), e_ts, 60):
                if ts < cutoff:
                    if ts == t0:
                        if omit_t0[0]:
                            continue
                        bars.append(
                            {"t": ts, "o": 100.0, "h": 105.0, "l": 100.0, "c": 105.0, "v": 4.0}
                        )
                    else:
                        bars.append(
                            {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}
                        )
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "test-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "api-key")
        agg = BarAggregator(timeframes=("1m", "5m"))
        svc = LiveFeedService(
            settings,
            auth,
            _RestAuthoritative(),
            parquet,
            sqlite,
            aggregator=agg,
            recovery_timeframes=("1m", "5m"),
            initial_backfill_bars=1,
            clock=lambda: now_t[0],
        )
        try:
            await svc.set_active_contracts([CID_MES])
            svc._last_connected_utc = t0 - 30
            await svc.trigger_recovery(now_utc=now_t[0])
            assert svc.feed_state == "LIVE"

            now_mono = time.monotonic()
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 10, 100.0, 1.0, "GatewayTrade", now_mono)
            )
            await svc.flush_pending_tasks()

            # Watchdog flushes locally-closed t0 (vol=1.0), NOT yet REST-confirmed.
            now_t[0] = t0 + 61
            await svc.run_watchdog_tick()
            assert svc.feed_state == "LIVE"

            # Late trade for t0 + 59.9 arrives at t0 + 61 -> MUST flag gap & DEGRADED!
            svc.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, t0 + 59.9, 105.0, 3.0, "GatewayTrade", now_mono + 1.0
                )
            )
            await svc.flush_pending_tasks()
            c_state = svc._contracts[CID_MES]
            assert c_state.state == "DEGRADED"
            assert t0 in c_state.unhealed_gap_buckets.get("1m", set())
            assert svc.live_alerts_enabled is False

            key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")

            # First REST recovery omits t0: cached stale bar MUST NOT self-heal the gap!
            omit_t0[0] = True
            now_t[0] = t0 + 62
            await svc.trigger_recovery(now_utc=now_t[0])
            assert c_state.state == "DEGRADED"
            assert svc.live_alerts_enabled is False
            assert t0 in c_state.unhealed_gap_buckets.get("1m", set())
            assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is False

            # REST reconciliation returns repaired t0 (vol=4.0, high=105.0) -> heals to LIVE.
            omit_t0[0] = False
            now_t[0] = t0 + 65
            await svc.trigger_recovery(now_utc=now_t[0])
            assert svc.feed_state == "LIVE"
            persisted = await parquet.read_bars(key_1m, start_ts=t0, end_ts=t0 + 60)
            assert any(b.time == t0 and b.volume == 4.0 and b.high == 105.0 for b in persisted)

            # Once REST-confirmed, an even later duplicate tick for t0 skips cleanly while LIVE.
            svc.handle_hub_event(
                SignalRTickEvent(
                    CID_MES, False, t0 + 59.8, 105.0, 3.0, "GatewayTrade", now_mono + 2.0
                )
            )
            await svc.flush_pending_tasks()
            assert c_state.state == "LIVE"
        finally:
            await svc.stop()
            sqlite.close()


@pytest.mark.asyncio
async def test_f03_signalr_bounded_event_queue_overflow_surfaces_error_and_disconnect(
    tmp_path: Path,
) -> None:
    """Contract: Bounded event_queue overflow records telemetry and surfaces error/disconnect.
    Defect caught: silent asyncio.QueueFull suppression losing ticks and disconnects.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    event_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    overflow_records: list[tuple[str, int]] = []
    transports: list[_MockSignalRTransport] = []

    async def fake_ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        tr = _MockSignalRTransport()
        transports.append(tr)
        return tr

    def record_overflow(evt: Any, count: int) -> None:
        overflow_records.append((type(evt).__name__, count))

    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
        ws_factory=fake_ws_factory,
        event_queue=event_queue,
        on_queue_overflow=record_overflow,
        contracts=[CID_MES],
        reconnect_initial_delay_sec=0.5,
        reconnect_max_delay_sec=1.0,
    )
    task = hub.start()
    try:
        for _ in range(50):
            if transports and hub.is_connected:
                break
            await asyncio.sleep(0.01)
        assert hub.is_connected is True
        first = await asyncio.wait_for(event_queue.get(), timeout=1.0)
        assert isinstance(first, SignalRConnectedEvent)

        # Fill bounded queue (maxsize=1) with tick0, then push tick1 to trigger QueueFull.
        tr0 = transports[0]
        await tr0.push_frame(_quote_frame(CID_MES, BASE_T + 1, 5950.0, 1.0))
        for _ in range(50):
            if event_queue.full():
                break
            await asyncio.sleep(0.01)
        assert event_queue.full() is True

        await tr0.push_frame(_quote_frame(CID_MES, BASE_T + 2, 5951.0, 2.0))
        for _ in range(50):
            if hub.queue_overflow_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert hub.queue_overflow_count >= 1
        assert overflow_records[0] == ("SignalRTickEvent", 1)

        # Consumer draining queue receives an explicit SignalRErrorEvent instead of silent loss.
        surfaced_err = await asyncio.wait_for(event_queue.get(), timeout=1.0)
        assert isinstance(surfaced_err, SignalRErrorEvent)
        assert "overflowed" in surfaced_err.message

        # Refill queue with tick2, then drop socket while queue is full -> disconnect MUST surface!
        await tr0.push_frame(_quote_frame(CID_MES, BASE_T + 3, 5952.0, 1.0))
        for _ in range(50):
            if event_queue.full():
                break
            await asyncio.sleep(0.01)
        assert event_queue.full() is True

        prev_overflows = hub.queue_overflow_count
        await tr0.drop()
        for _ in range(50):
            if hub.queue_overflow_count > prev_overflows:
                break
            await asyncio.sleep(0.01)
        assert hub.queue_overflow_count > prev_overflows

        drained = await asyncio.wait_for(event_queue.get(), timeout=1.0)
        assert isinstance(drained, SignalRDisconnectedEvent)
    finally:
        await hub.stop()
        assert task.done()


@pytest.mark.asyncio
async def test_f03_same_generation_quarantine_delayed_until_after_rest_heal_keeps_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 ensures delayed same-gen quarantine cannot overwrite REST-healed COMPLETE.
    Defect caught: coverage_write_lock only guarding queued_gen != _generation.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)

    rest_entered = asyncio.Event()
    release_rest = asyncio.Event()
    issues_entered = asyncio.Event()
    release_issues = asyncio.Event()
    hold_rest = [False]
    hold_issues = [False]

    class _GatedRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            if hold_rest[0]:
                rest_entered.set()
                await asyncio.wait_for(release_rest.wait(), timeout=2.0)
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            bars = [
                {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 2.0}
                for ts in range(s_ts - (s_ts % 60), e_ts, 60)
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_record_issues = sqlite_store.record_quality_issues

    async def gated_record_issues(key: StorageDatasetKey, issues: list[Any]) -> int:
        res = await orig_record_issues(key, issues)
        if hold_issues[0]:
            issues_entered.set()
            await asyncio.wait_for(release_issues.wait(), timeout=2.0)
        return res

    monkeypatch.setattr(sqlite_store, "record_quality_issues", gated_record_issues)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-samegen"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-samegen")
            now_ref = [BASE_T + 65]
            svc = LiveFeedService(
                settings,
                auth,
                _GatedRest(),
                parquet_store,
                sqlite_store,
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                hold_rest[0] = True
                hold_issues[0] = True
                svc._contracts[CID_MES].state = "RECOVERING"
                svc._last_connected_utc = BASE_T + 5
                rec_task = asyncio.create_task(svc._run_recovery(now_utc=BASE_T + 65))
                await asyncio.wait_for(rest_entered.wait(), timeout=2.0)

                # Bad tick for closed minute BASE_T arrives during RECOVERING in same generation.
                svc._handle_malformed_signalr_tick(
                    SignalRMalformedTickEvent(
                        contract_id=CID_MES,
                        live_mode=False,
                        reason="bad volume",
                        issue_code="invalid_tick",
                        raw_summary="{'v': -3}",
                        bar_time=BASE_T + 20,
                        received_monotonic=time.monotonic(),
                    )
                )
                await asyncio.wait_for(issues_entered.wait(), timeout=2.0)

                # Same-generation REST finishes healing [..., BASE_T + 60) -> LIVE!
                release_rest.set()
                await asyncio.wait_for(rec_task, timeout=2.0)
                assert svc._contracts[CID_MES].state == "LIVE"
                assert svc._contracts[CID_MES].unresolved_gaps == 0

                # Delayed same-generation quarantine releases after heal -> COMPLETE stays intact!
                release_issues.set()
                await svc.flush_pending_tasks()
                key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
                assert await sqlite_store.is_interval_complete(key_1m, BASE_T, BASE_T + 60) is True
                assert svc._contracts[CID_MES].unresolved_gaps == 0
                assert svc.feed_state == "LIVE"
            finally:
                release_rest.set()
                release_issues.set()
                await svc.stop()
    finally:
        sqlite_store.close()


@pytest.mark.asyncio
async def test_f03_buffered_late_tick_in_recovering_after_local_watermark_stays_degraded(
    tmp_path: Path,
) -> None:
    """Contract: PASS2-001 never treats a local-only watermark as REST-authoritative in recovery.
    Defect caught: local watermark suppressing buffered late tick in RECOVERING & healing stale T.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    now_t = [t0 + 10]
    omit_t0 = [True]

    class _RestReconnect:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = now_t[0] - (now_t[0] % 60)
            bars = []
            for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60):
                if ts == t0:
                    if omit_t0[0]:
                        continue
                    bars.append(
                        {"t": ts, "o": 100.0, "h": 105.0, "l": 100.0, "c": 105.0, "v": 4.0}
                    )
                else:
                    bars.append(
                        {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0}
                    )
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-p2-wm"})

    key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")

            # Phase 1: Confirm < t0 via REST, ingest tick in t0, flush locally at t0+61 (vol=1).
            svc1 = LiveFeedService(
                settings,
                auth,
                _RestReconnect(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_t[0],
            )
            try:
                await svc1.set_active_contracts([CID_MES])
                svc1._last_connected_utc = t0 - 30
                await svc1.trigger_recovery(now_utc=now_t[0])
                assert svc1.feed_state == "LIVE"

                svc1.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, t0 + 10, 100.0, 1.0, "GatewayTrade", time.monotonic()
                    )
                )
                await svc1.flush_pending_tasks()
                now_t[0] = t0 + 61
                await svc1.run_watchdog_tick()
                wm = await sqlite.get_watermark(key_1m)
                assert wm is not None and wm.last_closed_bar_time == t0
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is False
            finally:
                await svc1.stop()

            # Phase 2: Fresh service reads local watermark t0 during recovery; late tick for t0
            # is buffered in RECOVERING while REST returns < t0 (omitting t0) -> MUST stay DEGRADED!
            now_t[0] = t0 + 65
            svc2 = LiveFeedService(
                settings,
                auth,
                _RestReconnect(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_t[0],
            )
            try:
                await svc2.set_active_contracts([CID_MES])
                svc2._contracts[CID_MES].state = "RECOVERING"
                svc2._last_connected_utc = t0 + 64
                svc2.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, t0 + 59.9, 105.0, 3.0, "GatewayTrade", time.monotonic()
                    )
                )
                await svc2._run_recovery(now_utc=now_t[0])
                assert svc2.feed_state == "DEGRADED"
                assert svc2.live_alerts_enabled is False
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is False

                # Once REST returns repaired t0 (vol=4.0, high=105.0), recovery heals -> LIVE!
                omit_t0[0] = False
                now_t[0] = t0 + 66
                await svc2._run_recovery(now_utc=now_t[0])
                assert svc2.feed_state == "LIVE"
                assert svc2.live_alerts_enabled is True
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is True
                persisted = await parquet.read_bars(key_1m, start_ts=t0, end_ts=t0 + 60)
                assert any(b.time == t0 and b.volume == 4.0 and b.high == 105.0 for b in persisted)
            finally:
                await svc2.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_tail_drain_supersession_preserves_unprocessed_ticks_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: PASS2-TAIL-001 preserves unprocessed tail ticks and count across supersession.

    Defect caught: Clearing pending_by_contract and _replayed_tick_tfs before iterating
    remaining_indexed silently drops tail ticks (e.g. T+70) when recovery is superseded
    during bar-close persistence of an earlier tail tick (T+65).
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    b_prev = BASE_T - 60
    t0 = BASE_T
    mnq = "CON.F.US.MNQ.Z26"
    now_ref = [t0 + 20]
    tail_upsert_entered = asyncio.Event()
    release_tail_upsert = asyncio.Event()

    class _DynamicRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = now_ref[0] - (now_ref[0] % 60)
            bars = [
                {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 2.0}
                for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60)
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    orig_upsert = parquet.upsert_bars
    svc_ref: list[LiveFeedService] = []
    injected_tail = False

    async def _gated_upsert(key: StorageDatasetKey, bars: Any, **kwargs: Any) -> Any:
        nonlocal injected_tail
        if (
            not injected_tail
            and key.contract_id == CID_MES
            and any(getattr(b, "time", None) == b_prev for b in bars)
        ):
            injected_tail = True
            now_ref[0] = t0 + 75
            svc = svc_ref[0]
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 5, 100.0, 2.0, "GatewayTrade", 1.0)
            )
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 65, 102.0, 3.0, "GatewayTrade", 1.1)
            )
            svc.handle_hub_event(
                SignalRTickEvent(CID_MES, False, t0 + 70, 105.0, 4.0, "GatewayTrade", 1.2)
            )
        if (
            key.contract_id == CID_MES
            and any(getattr(b, "time", None) == t0 for b in bars)
            and not tail_upsert_entered.is_set()
        ):
            tail_upsert_entered.set()
            await asyncio.wait_for(release_tail_upsert.wait(), timeout=2.0)
        return await orig_upsert(key, bars, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _gated_upsert)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-tail-sup"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            svc = LiveFeedService(
                settings,
                auth,
                _DynamicRest(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=1,
                clock=lambda: now_ref[0],
            )
            svc_ref.append(svc)
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                svc._last_connected_utc = b_prev - 30
                svc._transition_all_contracts("RECOVERING")
                t1 = svc._start_recovery_task(now_utc=t0 + 20)

                await asyncio.wait_for(tail_upsert_entered.wait(), timeout=2.0)
                assert injected_tail is True
                # Unprocessed tail tick T+70 MUST still be accounted for while awaiting persistence.
                assert svc._pending_recovery_ticks_count >= 1

                # Supersede recovery (e.g. subscribing another contract + bumping generation).
                svc.register_contract(mnq, live_mode=False)
                svc._bump_generation_and_cancel_live_tasks()
                svc._transition_all_contracts("RECOVERING")
                t2 = svc._start_recovery_task(now_utc=now_ref[0])

                release_tail_upsert.set()
                await asyncio.gather(t1, return_exceptions=True)
                await asyncio.wait_for(t2, timeout=2.0)

                assert svc.feed_state == "LIVE"
                open_bar = svc._aggregator.get_open_bar(CID_MES, False, "1m")
                assert open_bar is not None
                assert open_bar.time == t0 + 60
                assert open_bar.open == 102.0
                assert open_bar.high == 105.0
                assert open_bar.close == 105.0
                assert open_bar.volume == 7.0
            finally:
                release_tail_upsert.set()
                await svc.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_rest_failure_tail_drain_skips_unconfirmed_historical_bar_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 blocks tail drain from closing/broadcasting unconfirmed historical bars.

    Defect caught: After REST backfill failure on a fresh aggregator at T+65, buffered ticks at
    T+59.9 and T+65 finalize and persist/broadcast closed bar T without REST confirmation.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = BASE_T
    now_ref = [t0 + 65]
    fail_rest = [True]

    class _FlakyRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            if fail_rest[0]:
                raise ConnectionError("REST history unreachable")
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = now_ref[0] - (now_ref[0] % 60)
            bars = []
            for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60):
                if ts == t0:
                    bars.append(
                        {"t": ts, "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 5.0}
                    )
                else:
                    bars.append(
                        {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 2.0}
                    )
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    upserted_closed: list[tuple[str, int, float]] = []
    orig_upsert = parquet.upsert_bars

    async def _spy_upsert(key: StorageDatasetKey, bars: Any, **kwargs: Any) -> Any:
        for b in bars:
            if b.is_closed:
                upserted_closed.append((key.timeframe, b.time, b.volume))
        return await orig_upsert(key, bars, **kwargs)

    monkeypatch.setattr(parquet, "upsert_bars", _spy_upsert)

    broadcasted_closed: list[tuple[str, int, float]] = []
    broadcaster = WsBroadcaster()
    orig_publish_bar = broadcaster.publish_bar

    def _spy_publish_bar(evt: Any) -> None:
        if evt.bar.is_closed:
            broadcasted_closed.append((evt.timeframe, evt.bar.time, evt.bar.volume))
        orig_publish_bar(evt)

    monkeypatch.setattr(broadcaster, "publish_bar", _spy_publish_bar)

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-p1-001"})

    key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            svc = LiveFeedService(
                settings,
                auth,
                _FlakyRest(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                broadcaster=broadcaster,
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                svc._last_connected_utc = t0 - 30
                svc._transition_all_contracts("RECOVERING")

                svc.handle_hub_event(
                    SignalRTickEvent(CID_MES, False, t0 + 59.9, 101.0, 2.0, "GatewayTrade", 1.0)
                )
                svc.handle_hub_event(
                    SignalRTickEvent(CID_MES, False, t0 + 65.0, 103.0, 3.0, "GatewayTrade", 1.1)
                )
                await svc._run_recovery(now_utc=now_ref[0])

                assert svc.feed_state == "DEGRADED"
                assert svc.live_alerts_enabled is False
                assert not any(bt == t0 for _, bt, _ in upserted_closed)
                assert not any(bt == t0 for _, bt, _ in broadcasted_closed)
                assert await parquet.read_bars(key_1m, start_ts=t0, end_ts=t0) == []
                assert await sqlite.get_watermark(key_1m) is None

                # Subsequent successful REST recovery MUST heal bucket t0 cleanly -> LIVE.
                fail_rest[0] = False
                now_ref[0] = t0 + 66
                await svc.trigger_recovery(now_utc=now_ref[0])

                assert svc.feed_state == "LIVE"
                assert svc.live_alerts_enabled is True
                persisted = await parquet.read_bars(key_1m, start_ts=t0, end_ts=t0)
                assert len(persisted) == 1
                assert persisted[0].volume == 5.0
                assert persisted[0].high == 102.0
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is True
                open_cur = svc._aggregator.get_open_bar(CID_MES, False, "1m")
                assert open_cur is not None and open_cur.time == t0 + 60 and open_cur.volume == 3.0
            finally:
                await svc.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_signalr_queue_overflow_eviction_marks_task_done_for_join(
    tmp_path: Path,
) -> None:
    """Contract: P1-002 calls task_done() when evicting oldest event on bounded queue overflow.

    Defect caught: _surface_queue_overflow calling get_nowait() without task_done() leaks an
    unfinished task on every overflow eviction, causing queue.join() to hang forever.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    event_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    hub = SignalRMarketHub(
        settings,
        token_provider=lambda: asyncio.sleep(0, result="test-jwt"),
        event_queue=event_queue,
        contracts=[CID_MES],
    )
    tick1 = SignalRTickEvent(CID_MES, False, BASE_T + 1, 5950.0, 1.0, "GatewayTrade", 1.0)
    tick2 = SignalRTickEvent(CID_MES, False, BASE_T + 2, 5951.0, 2.0, "GatewayTrade", 2.0)

    await hub._emit(tick1)
    await hub._emit(tick2)

    replacement = event_queue.get_nowait()
    assert isinstance(replacement, SignalRErrorEvent)
    event_queue.task_done()

    assert event_queue.empty() is True
    await asyncio.wait_for(event_queue.join(), timeout=0.2)


def test_f03_signalr_parse_rejects_symbol_id_fallback_without_contract_id() -> None:
    """Contract: P2-F02-001 requires explicit contractId or callback contract, never symbolId.

    Defect caught: parse_market_invocation_events falling back to root symbolId ('F.US.MES')
    when contractId and arguments[0] contract ID are absent, risking cross-expiry dataset mixing.
    """
    frame = {
        "type": 1,
        "target": "GatewayTrade",
        "arguments": [
            {
                "symbolId": "F.US.MES",
                "price": 5950.0,
                "volume": 2.0,
                "timestamp": BASE_T + 10,
            }
        ],
    }
    events = parse_market_invocation_events(frame, live_mode=False, received_monotonic=1.0)
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, SignalRMalformedTickEvent)
    assert evt.issue_code == "invalid_contract"
    assert not any(isinstance(e, SignalRTickEvent) and e.contract_id == "F.US.MES" for e in events)


@pytest.mark.asyncio
async def test_f03_live_feed_quarantines_untracked_contract_and_mismatched_live_mode_ticks(
    tmp_path: Path,
) -> None:
    """Contract: P2-F02-001 rejects/quarantines untracked contract_ids and live_mode mismatches.

    Defect caught: handle_hub_event ingesting ticks for unregistered contracts (F.US.MES) or
    mismatched live_mode into BarAggregator and ParquetStore under arbitrary dataset keys.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    clock_ref = [BASE_T + 10]

    class _OkRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = clock_ref[0] - (clock_ref[0] % 60)
            bars = [
                {"t": ts, "o": 5950.0, "h": 5951.0, "l": 5949.0, "c": 5950.0, "v": 2.0}
                for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60)
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-f02"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            agg = BarAggregator(timeframes=("1m",))
            svc = LiveFeedService(
                settings,
                auth,
                _OkRest(),
                parquet,
                sqlite,
                aggregator=agg,
                recovery_timeframes=("1m",),
                initial_backfill_bars=1,
                clock=lambda: clock_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                svc._last_connected_utc = BASE_T - 30
                await svc.trigger_recovery(now_utc=clock_ref[0])
                assert svc.feed_state == "LIVE"

                # Feed untracked root symbol tick + mismatched live_mode tick, then cross minute.
                svc.handle_hub_event(
                    SignalRTickEvent(
                        "F.US.MES", False, BASE_T + 10, 5950.0, 2.0, "GatewayTrade", 1.0
                    )
                )
                svc.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, True, BASE_T + 15, 5951.0, 3.0, "GatewayTrade", 1.1
                    )
                )
                clock_ref[0] = BASE_T + 70
                svc.handle_hub_event(
                    SignalRTickEvent(
                        "F.US.MES", False, BASE_T + 65, 5952.0, 1.0, "GatewayTrade", 1.2
                    )
                )
                svc.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, True, BASE_T + 66, 5953.0, 1.0, "GatewayTrade", 1.3
                    )
                )
                await svc.flush_pending_tasks()

                bad_root_key = StorageDatasetKey("topstepx", False, "F.US.MES", "1m")
                bad_mode_key = StorageDatasetKey("topstepx", True, CID_MES, "1m")
                assert (
                    await parquet.read_bars(
                        bad_root_key, start_ts=BASE_T - 60, end_ts=BASE_T + 120
                    )
                    == []
                )
                assert (
                    await parquet.read_bars(
                        bad_mode_key, start_ts=BASE_T - 60, end_ts=BASE_T + 120
                    )
                    == []
                )
                assert agg.get_open_bar("F.US.MES", False, "1m") is None
                assert agg.get_open_bar(CID_MES, True, "1m") is None

                root_issues = await sqlite.list_quality_issues(bad_root_key)
                assert len(root_issues) >= 1
                mode_issues = await sqlite.list_quality_issues(bad_mode_key)
                assert len(mode_issues) >= 1
            finally:
                await svc.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_late_tick_auto_recovery_fences_generation_and_other_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-002 fences generation & transitions all contracts to RECOVERING on late tick.

    Defect caught: _handle_stale_local_buckets calling _start_recovery_task() without bumping
    _generation or transitioning other LIVE contracts to RECOVERING, leaving live ticks on
    contract B to spawn uncancelled persistence tasks that overwrite REST HTF bars.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    mnq = "CON.F.US.MNQ.Z26"
    clock_ref = [t0 + 10]
    block_mes_rest = [False]
    mes_rest_entered = asyncio.Event()
    release_mes_rest = asyncio.Event()

    class _FenceRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            cid = str(kw["contract_id"])
            if cid == CID_MES and block_mes_rest[0]:
                mes_rest_entered.set()
                await asyncio.wait_for(release_mes_rest.wait(), timeout=2.0)
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = clock_ref[0] - (clock_ref[0] % 60)
            bars = []
            for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60):
                vol = 10.0 if (cid == mnq and t0 <= ts < t0 + 300) else 2.0
                bars.append({"t": ts, "o": 100.0, "h": 105.0, "l": 99.0, "c": 102.0, "v": vol})
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-fence"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            agg = BarAggregator(timeframes=("1m", "5m"))
            svc = LiveFeedService(
                settings,
                auth,
                _FenceRest(),
                parquet,
                sqlite,
                aggregator=agg,
                recovery_timeframes=("1m", "5m"),
                initial_backfill_bars=1,
                clock=lambda: clock_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES, mnq], live_mode=False)
                svc._last_connected_utc = t0 - 30
                await svc.trigger_recovery(now_utc=clock_ref[0])
                assert svc.feed_state == "LIVE"

                # Feed live ticks on CID_MES and MNQ, then watchdog-flush locally at t0 + 61.
                now_mono = time.monotonic()
                svc.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, t0 + 10, 100.0, 1.0, "GatewayTrade", now_mono
                    )
                )
                svc.handle_hub_event(
                    SignalRTickEvent(
                        mnq, False, t0 + 10, 20000.0, 1.0, "GatewayTrade", now_mono
                    )
                )
                await svc.flush_pending_tasks()
                clock_ref[0] = t0 + 61
                await svc.run_watchdog_tick()
                assert svc.feed_state == "LIVE"

                # Enable service/hub flags and block CID_MES REST before late tick triggers.
                svc._running = True
                monkeypatch.setattr(svc._hub, "_connected", True)
                block_mes_rest[0] = True
                gen_before = svc._generation
                clock_ref[0] = t0 + 301

                svc.handle_hub_event(
                    SignalRTickEvent(
                        CID_MES, False, t0 + 59.9, 105.0, 3.0, "GatewayTrade", now_mono + 1.0
                    )
                )

                assert svc.feed_state == "RECOVERING"
                assert svc._contracts[CID_MES].state == "RECOVERING"
                assert svc._contracts[mnq].state == "RECOVERING"
                assert svc._generation > gen_before

                await asyncio.wait_for(mes_rest_entered.wait(), timeout=2.0)

                # MNQ tick at t0+305 during MES recovery must buffer, not spawn live tasks.
                svc.handle_hub_event(
                    SignalRTickEvent(
                        mnq, False, t0 + 305, 20010.0, 1.0, "GatewayTrade", now_mono + 2.0
                    )
                )
                assert len(svc._live_tick_tasks) == 0

                release_mes_rest.set()
                await svc.flush_pending_tasks()
                assert svc.feed_state == "LIVE"

                # MNQ's authoritative 5m bar at t0 (5 x 1m @ vol 10 = 50.0) must remain intact.
                key_mnq_5m = StorageDatasetKey("topstepx", False, mnq, "5m")
                bars_5m = await parquet.read_bars(key_mnq_5m, start_ts=t0, end_ts=t0)
                assert len(bars_5m) == 1
                assert bars_5m[0].volume == 50.0
            finally:
                release_mes_rest.set()
                svc._running = False
                await svc.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_cancelled_recovery_shields_coverage_write_lock_until_thread_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001 shields record_coverage inside _coverage_write_lock across cancellation.

    Defect caught: CancelledError releasing _coverage_write_lock while an older to_thread
    _record_coverage_sync worker is still queued/running, letting stale CORRUPTED overwrite
    newer COMPLETE coverage after supersession.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = BASE_T
    now_t = t0 + 65
    corrupt_rest = [True]
    thread_entered = threading.Event()
    release_thread = threading.Event()

    orig_record_sync = sqlite._record_coverage_sync

    def _gated_record_coverage_sync(*args: Any, **kwargs: Any) -> Any:
        status = kwargs.get("status")
        if status is None and len(args) >= 4:
            status = args[3]
        if status == "CORRUPTED" and not thread_entered.is_set():
            thread_entered.set()
            release_thread.wait(timeout=3.0)
        return orig_record_sync(*args, **kwargs)

    monkeypatch.setattr(sqlite, "_record_coverage_sync", _gated_record_coverage_sync)

    class _CorruptThenCleanRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = now_t - (now_t % 60)
            if corrupt_rest[0]:
                # Trigger CORRUPTED coverage via invalid negative price in REST payload.
                bars = [{"t": t0, "o": -1.0, "h": 100.0, "l": 99.0, "c": 100.0, "v": 1.0}]
            else:
                bars = [
                    {"t": ts, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 2.0}
                    for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60)
                ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-cov"})

    key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            svc = LiveFeedService(
                settings,
                auth,
                _CorruptThenCleanRest(),
                parquet,
                sqlite,
                recovery_timeframes=("1m",),
                initial_backfill_bars=1,
                clock=lambda: now_t,
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=False)
                svc._last_connected_utc = t0 - 30
                svc._transition_all_contracts("RECOVERING")
                t1 = svc._start_recovery_task(now_utc=now_t)

                for _ in range(200):
                    if thread_entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert thread_entered.is_set() is True

                # Supersede recovery while t1's CORRUPTED worker thread is paused.
                corrupt_rest[0] = False
                svc._bump_generation_and_cancel_live_tasks()
                t2 = svc._start_recovery_task(now_utc=now_t)

                # Wait briefly; t1 MUST hold _coverage_write_lock until its thread finishes.
                await asyncio.sleep(0.05)
                assert not t1.done()
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is False

                release_thread.set()
                await asyncio.gather(t1, return_exceptions=True)
                await asyncio.wait_for(t2, timeout=2.0)

                assert svc.feed_state == "LIVE"
                assert await sqlite.is_interval_complete(key_1m, t0, t0 + 60) is True
            finally:
                release_thread.set()
                await svc.stop()
    finally:
        sqlite.close()


def test_f03_reconcile_recovery_no_open_bar_prunes_rest_confirmed_incomplete_for_htf() -> None:
    """Contract: P1-F03-001 prunes REST-confirmed incomplete markers even when rebuilt_open is None.

    Defect caught: reconcile_recovery only pruning <= latest_hist_closed inside the
    rebuilt_open is not None branch, leaving stale 1m markers to poison 5m HTF rebuilds.
    """
    t0 = first_bucket_open_at_or_after(BASE_T, "5m")
    agg = BarAggregator(timeframes=("1m", "5m"))
    agg.mark_open_bucket_incomplete(CID_MES, False, t0, timeframes=("1m",))

    hist_1m = [
        OHLCVBar(t0 + (i * 60), 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 10.0, is_closed=True)
        for i in range(4)
    ]
    now_140410 = t0 + 250
    recon_1m = agg.reconcile_recovery(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="1m",
        historical_bars=hist_1m,
        buffered_ticks=(),
        now_utc=now_140410,
        mark_open_incomplete=False,
    )
    assert recon_1m.open_bar is None

    agg.reconcile_from_1m_bars(
        contract_id=CID_MES,
        live_mode=False,
        timeframe="5m",
        closed_1m_bars=[e.bar for e in recon_1m.closed_bars],
        open_1m_bar=None,
        now_utc=now_140410,
        covered_start_utc=t0,
    )

    res = agg.process_tick(
        NormalizedTick(CID_MES, False, t0 + 270, 106.0, 5.0),
        now_utc=t0 + 270,
    )
    assert res.accepted is True

    flushed = agg.flush_closed_bars(now_utc=t0 + 300, contract_id=CID_MES, live_mode=False)
    assert agg.pop_discarded_incomplete_bars(contract_id=CID_MES, live_mode=False) == []
    flushed_by_tf = {evt.timeframe: evt.bar for evt in flushed}
    assert "1m" in flushed_by_tf
    assert flushed_by_tf["1m"] == OHLCVBar(
        t0 + 240, 106.0, 106.0, 106.0, 106.0, 5.0, is_closed=True
    )
    assert "5m" in flushed_by_tf
    assert flushed_by_tf["5m"] == OHLCVBar(
        t0, 100.0, 106.0, 99.0, 106.0, 45.0, is_closed=True
    )


@pytest.mark.asyncio
async def test_f03_mismatched_live_mode_malformed_tick_preserves_live_state_and_coverage(
    tmp_path: Path,
) -> None:
    """Contract: P2-F02-002 isolates mismatched live_mode malformed ticks from active mode.

    Defect caught: _handle_malformed_signalr_tick overwriting event.live_mode with c_state mode,
    corrupting an active LIVE contract's state, open bar, and SQLite coverage on stale Sim events.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = BASE_T
    clock_ref = [t0 + 10]

    class _OkRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            cutoff = clock_ref[0] - (clock_ref[0] % 60)
            bars = [
                {"t": ts, "o": 5950.0, "h": 5951.0, "l": 5949.0, "c": 5950.0, "v": 2.0}
                for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60)
            ]
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-mode-malf"})

    key_live_1m = StorageDatasetKey("topstepx", True, CID_MES, "1m")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")
            agg = BarAggregator(timeframes=("1m",))
            svc = LiveFeedService(
                settings,
                auth,
                _OkRest(),
                parquet,
                sqlite,
                aggregator=agg,
                recovery_timeframes=("1m",),
                initial_backfill_bars=1,
                clock=lambda: clock_ref[0],
            )
            try:
                await svc.set_active_contracts([CID_MES], live_mode=True)
                svc._last_connected_utc = t0 - 30
                await svc.trigger_recovery(now_utc=clock_ref[0])
                assert svc.feed_state == "LIVE"
                assert await sqlite.is_interval_complete(key_live_1m, t0 - 60, t0) is True

                # Open an active LIVE bar at t0.
                svc.handle_hub_event(
                    SignalRTickEvent(CID_MES, True, t0 + 10, 5950.0, 2.0, "GatewayTrade", 1.0)
                )
                assert agg.get_open_bar(CID_MES, True, "1m") is not None

                # Stale Sim (live_mode=False) malformed tick arrives for the same contract_id.
                svc.handle_hub_event(
                    SignalRMalformedTickEvent(
                        contract_id=CID_MES,
                        live_mode=False,
                        reason="stale sim invalid price",
                        issue_code="invalid_price",
                        raw_summary="{'price': -1}",
                        bar_time=t0 + 20,
                        received_monotonic=2.0,
                    )
                )
                await svc.flush_pending_tasks()

                c_state = svc._contracts[CID_MES]
                assert svc.feed_state == "LIVE"
                assert c_state.state == "LIVE"
                assert c_state.unresolved_gaps == 0
                assert await sqlite.is_interval_complete(key_live_1m, t0 - 60, t0) is True
                live_cov = await sqlite.get_coverage_intervals(key_live_1m)
                assert not any(c.status == "CORRUPTED" for c in live_cov)

                # Active LIVE open bar at t0 stays valid; crossing t0+60 closes it cleanly.
                clock_ref[0] = t0 + 65
                svc.handle_hub_event(
                    SignalRTickEvent(CID_MES, True, t0 + 65, 5952.0, 1.0, "GatewayTrade", 3.0)
                )
                await svc.flush_pending_tasks()
                persisted = await parquet.read_bars(key_live_1m, start_ts=t0, end_ts=t0)
                assert len(persisted) == 1
                assert persisted[0].volume == 2.0
            finally:
                await svc.stop()
    finally:
        sqlite.close()


@pytest.mark.asyncio
async def test_f03_restart_restores_persisted_corrupted_gap_prior_to_watermark_overlap(
    tmp_path: Path,
) -> None:
    """Contract: P1-001/P2-001 restores persisted non-COMPLETE SQLite gaps across restart.

    Defect caught: Restarting after a late malformed tick left CORRUPTED [14:02, 14:03) with
    watermark at 14:09 computed backfill_start=14:07, skipping 14:02 and falsely going LIVE.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite = SqliteStore(tmp_path / "meta.db")
    sqlite.initialize()
    parquet = ParquetStore(settings.data_dir, sqlite_store=sqlite)

    t0 = BASE_T  # 14:00:00
    t_gap = t0 + 120  # 14:02:00
    t_wm = t0 + 540  # 14:09:00
    now_t = t0 + 610  # 14:10:10

    key_1m = StorageDatasetKey("topstepx", False, CID_MES, "1m")
    rest_calls: list[tuple[int, int]] = []
    rest_mode = ["fail"]  # "fail" -> "partial" -> "heal"

    class _RestartRest:
        async def retrieve_bars_paginated(self, **kw: Any) -> Any:
            s_ts = int(kw["start_ts"])
            e_ts = int(kw["end_ts"])
            rest_calls.append((s_ts, e_ts))
            if rest_mode[0] == "fail":
                raise ConnectionError("REST down during restart")
            cutoff = now_t - (now_t % 60)
            bars = []
            for ts in range(s_ts - (s_ts % 60), min(e_ts, cutoff), 60):
                if rest_mode[0] == "partial" and ts < t0 + 420:  # < 14:07
                    continue
                bars.append(
                    {"t": ts, "o": 5950.0, "h": 5952.0, "l": 5949.0, "c": 5951.0, "v": 4.0}
                )
            return type("R", (), {"raw_bars": bars, "truncated": False, "provider_gap": False})()

    def _auth_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errorCode": 0, "token": "jwt-restart"})

    try:
        # Seed persisted state before restart: COMPLETE 14:00..14:09, watermark=14:09,
        # then a late malformed tick at 14:02 writes CORRUPTED [14:02, 14:03).
        await sqlite.record_coverage(key_1m, t0, t_wm + 60, status="COMPLETE")
        await sqlite.upsert_watermark(key_1m, t_wm)
        await sqlite.record_coverage(
            key_1m, t_gap, t_gap + 60, status="CORRUPTED", quarantined_count=1
        )
        assert await sqlite.is_interval_complete(key_1m, t_gap, t_gap + 60) is False

        async with httpx.AsyncClient(transport=httpx.MockTransport(_auth_handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "api-key")

            # Restarted service #1: REST fails -> MUST stay DEGRADED with unresolved_gaps > 0.
            svc1 = LiveFeedService(
                settings,
                auth,
                _RestartRest(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_t,
            )
            try:
                await svc1.set_active_contracts([CID_MES], live_mode=False)
                svc1._last_connected_utc = t0 - 30
                await svc1.trigger_recovery(now_utc=now_t)
                assert svc1.feed_state == "DEGRADED"
                assert svc1.live_alerts_enabled is False
                assert svc1.get_status_snapshot()["unresolved_gaps"] > 0
                assert rest_calls[-1][0] <= t_gap
            finally:
                await svc1.stop()

            # Restarted service #2: REST omits 14:02 -> MUST stay DEGRADED with unresolved_gaps > 0.
            rest_mode[0] = "partial"
            svc2 = LiveFeedService(
                settings,
                auth,
                _RestartRest(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_t,
            )
            try:
                await svc2.set_active_contracts([CID_MES], live_mode=False)
                svc2._last_connected_utc = t0 - 30
                await svc2.trigger_recovery(now_utc=now_t)
                assert svc2.feed_state == "DEGRADED"
                assert svc2.live_alerts_enabled is False
                assert svc2.get_status_snapshot()["unresolved_gaps"] > 0
                assert rest_calls[-1][0] <= t_gap
                assert await sqlite.is_interval_complete(key_1m, t_gap, t_gap + 60) is False
            finally:
                await svc2.stop()

            # Restarted service #3: REST returns repaired 14:02..14:09 -> heals to LIVE!
            rest_mode[0] = "heal"
            svc3 = LiveFeedService(
                settings,
                auth,
                _RestartRest(),
                parquet,
                sqlite,
                aggregator=BarAggregator(timeframes=("1m",)),
                recovery_timeframes=("1m",),
                initial_backfill_bars=2,
                clock=lambda: now_t,
            )
            try:
                await svc3.set_active_contracts([CID_MES], live_mode=False)
                svc3._last_connected_utc = t0 - 30
                await svc3.trigger_recovery(now_utc=now_t)
                assert rest_calls[-1][0] <= t_gap
                assert svc3.feed_state == "LIVE"
                assert svc3.live_alerts_enabled is True
                assert svc3.get_status_snapshot()["unresolved_gaps"] == 0
                assert await sqlite.is_interval_complete(key_1m, t_gap, t_gap + 60) is True
            finally:
                await svc3.stop()
    finally:
        sqlite.close()



