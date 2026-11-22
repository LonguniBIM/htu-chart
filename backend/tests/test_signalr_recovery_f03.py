"""Value-first tests for F03 SignalR Market Hub, recovery reconciliation, and status telemetry."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.engine.bar_aggregator import BarAggregator
from app.engine.instruments import StorageDatasetKey
from app.engine.ohlcv import OHLCVBar
from app.main import create_app
from app.services.live_feed_service import LiveFeedService
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import (
    SIGNALR_RECORD_SEPARATOR,
    SignalRMarketHub,
)

BASE_T = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
CID_MES = "CON.F.US.MES.Z26"


class _MockSignalRTransport:
    """In-memory SignalR WebSocket transport speaking 0x1e-delimited JSON frames."""

    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.closed = False
        self.handshake_done = asyncio.Event()

    async def send(self, data: str) -> None:
        for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
            if chunk.strip():
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
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


def _quote_frame(contract_id: str, ts: int, price: float, volume: float) -> dict[str, Any]:
    return {
        "type": 1,
        "target": "GatewayTrade",
        "arguments": [
            contract_id,
            {
                "contractId": contract_id,
                "price": price,
                "volume": volume,
                "timestamp": datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z"),
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
        if attempt <= 3:
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
    assert sleep_delays[:3] == [2.0, 4.0, 8.0]
    err_msgs = [e.message for e in events if hasattr(e, "message")]
    assert len(err_msgs) == 3
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
    clock_now = b4 + 30

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
                # Initial connect backfill: b0 closed
                return httpx.Response(
                    200,
                    json={"success": True, "bars": [_wire_bar(b0, 5950, 5952, 5949, 5951, 10)]},
                )
            # Reconnect backfill: hold open so ticks stream into bounded recovery queue!
            backfill_started.set()
            await allow_backfill_finish.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        _wire_bar(b0, 5950, 5952, 5949, 5951, 10),
                        _wire_bar(b1, 5951, 5955, 5950, 5954, 25),
                        _wire_bar(b2, 5954, 5956, 5953, 5955, 18),
                        _wire_bar(b3, 5955, 5958, 5954, 5957, 22),
                    ],
                },
            )
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
                states_observed_during_signal.append(live_feed.feed_state)

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
                clock=lambda: clock_now,
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

            # Seed live tick in b1, then drop socket mid-candle!
            await transports[0].push_frame(_quote_frame(CID_MES, b1 + 10, 5952.0, 5.0))
            await asyncio.sleep(0.02)
            assert agg.get_open_bar(CID_MES, False, "1m") is not None

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
