"""Value-first tests for /ws/market auth/origin enforcement and R06 slow-consumer containment."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.engine.bar_aggregator import AggregatedBarEvent
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import first_bucket_open_at_or_after, has_open_session_in_range
from app.main import create_app
from app.services.live_feed_service import LiveFeedService
from app.services.ws_broadcaster import WS_CLOSE_SLOW_CONSUMER, WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import SIGNALR_RECORD_SEPARATOR

TEST_PASSWORD = "ws-market-test-password"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)
BASE_T = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())


class _FakeWsClient:
    """Controllable async WebSocket double for testing slow-consumer containment."""

    def __init__(self, *, stall_event: asyncio.Event | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed_calls: list[tuple[int, str | None]] = []
        self._stall_event = stall_event

    async def send_json(self, data: Any) -> None:
        if self._stall_event is not None:
            await self._stall_event.wait()
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_calls.append((code, reason))


def test_r06_ws_market_enforces_session_and_origin_before_accept(tmp_path: Path) -> None:
    """Contract: /ws/market rejects unauthenticated or foreign-Origin upgrades pre-accept.

    Defect caught: Cross-origin or unauthenticated WebSocket clients streaming live market data.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH, data_dir=tmp_path / "data")
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        # 1) Unauthenticated upgrade -> rejected with 1008 before accept
        with pytest.raises(WebSocketDisconnect) as exc_unauth:
            with client.websocket_connect(
                "/ws/market",
                headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
            ):
                pass
        assert exc_unauth.value.code == 1008

        # Authenticate caller session
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies["topstep_session"]

        # 2) Disallowed Origin -> rejected with 1008 before accept
        with pytest.raises(WebSocketDisconnect) as exc_origin:
            with client.websocket_connect(
                "/ws/market",
                headers={
                    "Host": "127.0.0.1:8000",
                    "Origin": "https://evil.attacker.example",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ):
                pass
        assert exc_origin.value.code == 1008

        # 3) Disallowed Host -> rejected with 1008 before accept
        with pytest.raises(WebSocketDisconnect) as exc_host:
            with client.websocket_connect(
                "/ws/market",
                headers={
                    "Host": "evil.attacker.example",
                    "Origin": "http://127.0.0.1:8000",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ):
                pass
        assert exc_host.value.code == 1008

        # 4) Valid session + Host + Origin -> accepted, subscribes, and receives bar_update
        with client.websocket_connect(
            "/ws/market",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "Cookie": f"topstep_session={session_cookie}",
            },
        ) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "feed_status"
            assert initial["feed_state"] == "DISCONNECTED"

            ws.send_json(
                {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
            )
            sub_ack = ws.receive_json()
            assert sub_ack == {
                "type": "subscribed",
                "contract_id": "CON.F.US.MES.Z26",
                "timeframe": "1m",
            }

            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            while pong.get("type") == "feed_status":
                pong = ws.receive_json()
            assert pong == {"type": "pong"}

            # Trigger bar broadcast directly via app's broadcaster
            app.state.ws_broadcaster.publish_bar(
                AggregatedBarEvent(
                    contract_id="CON.F.US.MES.Z26",
                    live_mode=False,
                    timeframe="1m",
                    bar=OHLCVBar(
                        time=BASE_T,
                        open=5950.0,
                        high=5951.5,
                        low=5949.5,
                        close=5951.0,
                        volume=12.0,
                        is_closed=True,
                    ),
                )
            )
            pushed = ws.receive_json()
            assert pushed["type"] == "bar_update"
            assert pushed["contract_id"] == "CON.F.US.MES.Z26"
            assert pushed["bar"]["is_closed"] is True
            assert pushed["bar"]["close"] == 5951.0


async def test_r06_slow_consumer_send_timeout_isolated_from_healthy_client() -> None:
    """Contract: Stalled WS client times out and disconnects while healthy client receives updates.

    Defect caught: Inline await on slow client blocking healthy clients or market ingestion.
    """
    broadcaster = WsBroadcaster(client_queue_maxsize=16, send_timeout_sec=0.08)
    stall_gate = asyncio.Event()
    slow_ws = _FakeWsClient(stall_event=stall_gate)
    healthy_ws = _FakeWsClient()

    slow_sess = broadcaster.register(slow_ws, client_id="slow-1")
    healthy_sess = broadcaster.register(healthy_ws, client_id="healthy-1")
    broadcaster.subscribe("slow-1", "CON.F.US.MES.Z26", "1m")
    broadcaster.subscribe("healthy-1", "CON.F.US.MES.Z26", "1m")

    t0 = time.monotonic()
    for step in range(5):
        broadcaster.publish_bar(
            AggregatedBarEvent(
                contract_id="CON.F.US.MES.Z26",
                live_mode=False,
                timeframe="1m",
                bar=OHLCVBar(
                    time=BASE_T,
                    open=5950.0,
                    high=5950.0 + step,
                    low=5949.0,
                    close=5950.0 + step,
                    volume=float(step + 1),
                    is_closed=False,
                ),
            )
        )
    broadcaster.publish_bar(
        AggregatedBarEvent(
            contract_id="CON.F.US.MES.Z26",
            live_mode=False,
            timeframe="1m",
            bar=OHLCVBar(
                time=BASE_T,
                open=5950.0,
                high=5955.0,
                low=5949.0,
                close=5955.0,
                volume=20.0,
                is_closed=True,
            ),
        )
    )
    publish_elapsed = time.monotonic() - t0
    assert publish_elapsed < 0.05

    # Healthy client receives updates promptly before slow client even finishes timing out!
    await asyncio.sleep(0.02)
    assert len(healthy_ws.sent) >= 1
    assert any(m["bar"]["is_closed"] is True for m in healthy_ws.sent)

    # Slow client disconnects cleanly after send_timeout_sec
    await asyncio.wait_for(slow_sess.closed_event.wait(), timeout=1.0)
    assert broadcaster.slow_disconnect_count == 1
    assert broadcaster.client_count == 1
    assert slow_ws.closed_calls
    assert slow_ws.closed_calls[0][0] == WS_CLOSE_SLOW_CONSUMER
    assert not healthy_sess.closed_event.is_set()

    stall_gate.set()
    await broadcaster.close_all()


async def test_r06_open_bar_coalescing_preserves_closed_bars_and_disconnects_on_overflow() -> None:
    """Contract: Rapid open bars coalesce without dropping closed bars; overflow drops slow client.

    Defect caught: Queue overflow from open-bar tick bursts or overwriting finalized closed bars.
    """
    broadcaster = WsBroadcaster(client_queue_maxsize=4, send_timeout_sec=1.0)
    gate = asyncio.Event()
    blocked_ws = _FakeWsClient(stall_event=gate)
    healthy_ws = _FakeWsClient()

    sess = broadcaster.register(blocked_ws, client_id="burst-1")
    healthy_sess = broadcaster.register(healthy_ws, client_id="healthy-2")
    broadcaster.subscribe("burst-1", "CON.F.US.MES.Z26", "1m")
    broadcaster.subscribe("healthy-2", "CON.F.US.MES.Z26", "1m")

    # First message enters sender_loop and blocks on gate; later messages stay queued.
    broadcaster.publish_bar(
        AggregatedBarEvent(
            contract_id="CON.F.US.MES.Z26",
            live_mode=False,
            timeframe="1m",
            bar=OHLCVBar(BASE_T, 5950.0, 5950.0, 5950.0, 5950.0, 1.0, is_closed=True),
        )
    )
    await asyncio.sleep(0.01)

    # Queue is empty while sender is blocked on msg 0.
    # 20 rapid provisional open-bar updates must coalesce into 1 slot without overflow!
    for i in range(1, 21):
        broadcaster.publish_bar(
            AggregatedBarEvent(
                contract_id="CON.F.US.MES.Z26",
                live_mode=False,
                timeframe="1m",
                bar=OHLCVBar(
                    BASE_T + 60,
                    5951.0,
                    5951.0 + i,
                    5950.5,
                    5951.0 + i,
                    float(i),
                    is_closed=False,
                ),
            )
        )

    assert len(sess.queue) == 1
    assert broadcaster.slow_disconnect_count == 0

    # Finalized closed bar must NOT overwrite or get overwritten by subsequent open bars.
    broadcaster.publish_bar(
        AggregatedBarEvent(
            contract_id="CON.F.US.MES.Z26",
            live_mode=False,
            timeframe="1m",
            bar=OHLCVBar(BASE_T + 60, 5951.0, 5971.0, 5950.5, 5971.0, 20.0, is_closed=True),
        )
    )
    # Next candle provisional open bar
    broadcaster.publish_bar(
        AggregatedBarEvent(
            contract_id="CON.F.US.MES.Z26",
            live_mode=False,
            timeframe="1m",
            bar=OHLCVBar(BASE_T + 120, 5971.0, 5972.0, 5971.0, 5972.0, 1.0, is_closed=False),
        )
    )
    assert len(sess.queue) == 3

    # Release gate and verify closed bars, coalesced latest open payload, and next open bar.
    gate.set()
    await asyncio.sleep(0.03)
    closed_times = [m["bar"]["time"] for m in blocked_ws.sent if m["bar"]["is_closed"]]
    assert closed_times == [BASE_T, BASE_T + 60]
    coalesced_open = [
        m for m in blocked_ws.sent if not m["bar"]["is_closed"] and m["bar"]["time"] == BASE_T + 60
    ]
    assert len(coalesced_open) == 1
    assert coalesced_open[0]["bar"]["close"] == 5971.0
    assert coalesced_open[0]["bar"]["volume"] == 20.0
    assert blocked_ws.sent[-1]["bar"]["time"] == BASE_T + 120
    assert blocked_ws.sent[-1]["bar"]["is_closed"] is False

    # Re-block gate and overflow blocked_ws with 5 non-coalescible closed bars (maxsize=4)
    # while yielding between batches so healthy_ws drains normally.
    gate.clear()
    broadcaster.publish_bar(
        AggregatedBarEvent(
            contract_id="CON.F.US.MES.Z26",
            live_mode=False,
            timeframe="1m",
            bar=OHLCVBar(BASE_T + 180, 5972.0, 5973.0, 5972.0, 5973.0, 1.0, is_closed=True),
        )
    )
    await asyncio.sleep(0.01)
    for k in range(3):
        broadcaster.publish_bar(
            AggregatedBarEvent(
                contract_id="CON.F.US.MES.Z26",
                live_mode=False,
                timeframe="1m",
                bar=OHLCVBar(
                    BASE_T + 240 + (k * 60),
                    5973.0,
                    5974.0,
                    5973.0,
                    5974.0,
                    1.0,
                    is_closed=True,
                ),
            )
        )
    await asyncio.sleep(0.01)  # healthy_ws drains all 3; blocked_ws now has 3 queued
    for k in range(3, 5):
        broadcaster.publish_bar(
            AggregatedBarEvent(
                contract_id="CON.F.US.MES.Z26",
                live_mode=False,
                timeframe="1m",
                bar=OHLCVBar(
                    BASE_T + 240 + (k * 60),
                    5973.0,
                    5974.0,
                    5973.0,
                    5974.0,
                    1.0,
                    is_closed=True,
                ),
            )
        )

    await asyncio.wait_for(sess.closed_event.wait(), timeout=1.0)
    await asyncio.sleep(0.02)
    assert broadcaster.slow_disconnect_count == 1
    assert broadcaster.client_count == 1
    assert not healthy_sess.closed_event.is_set()
    assert blocked_ws.closed_calls[-1][0] == WS_CLOSE_SLOW_CONSUMER
    assert len(healthy_ws.sent) >= 9

    gate.set()
    await broadcaster.close_all()


class _MockSignalRTransport:
    """Minimal SignalR WebSocket transport for full-app /ws/market -> SignalR integration test."""

    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.subscribed_contracts: set[str] = set()
        self.subscribed_event = asyncio.Event()

    async def send(self, data: str) -> None:
        for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
            if chunk.strip():
                frame = json.loads(chunk)
                self.sent_frames.append(frame)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                elif frame.get("target") in {
                    "SubscribeContractQuotes",
                    "SubscribeContractTrades",
                }:
                    for arg in frame.get("arguments", []):
                        self.subscribed_contracts.add(str(arg))
                    if frame.get("invocationId") is not None:
                        ack = {"type": 3, "invocationId": frame["invocationId"]}
                        await self.incoming.put(json.dumps(ack) + SIGNALR_RECORD_SEPARATOR)
                    self.subscribed_event.set()

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        await self.incoming.put(ConnectionError("closed"))

    async def push_trade(self, contract_id: str, ts: int, price: float, volume: float) -> None:
        frame = {
            "type": 1,
            "target": "GatewayTrade",
            "arguments": [
                contract_id,
                {
                    "symbolId": "F.US.MES",
                    "price": price,
                    "volume": volume,
                    "timestamp": ts,
                },
            ],
        }
        await self.incoming.put(json.dumps(frame) + SIGNALR_RECORD_SEPARATOR)

    async def push_quote(
        self,
        contract_id: str,
        ts: int,
        last_price: float,
        cumulative_volume: float,
    ) -> None:
        frame = {
            "type": 1,
            "target": "GatewayQuote",
            "arguments": [
                contract_id,
                {
                    "symbolId": "F.US.MES",
                    "lastPrice": last_price,
                    "volume": cumulative_volume,
                    "timestamp": ts,
                },
            ],
        }
        await self.incoming.put(json.dumps(frame) + SIGNALR_RECORD_SEPARATOR)


def _receive_json_with_timeout(ws: Any, timeout_sec: float) -> dict[str, Any]:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ws.receive_json)
    try:
        return future.result(timeout=timeout_sec)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _bars_for_history_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_start = str(payload["startTime"]).replace("Z", "+00:00")
    raw_end = str(payload["endTime"]).replace("Z", "+00:00")
    req_start = int(datetime.fromisoformat(raw_start).timestamp())
    req_end = int(datetime.fromisoformat(raw_end).timestamp())
    limit = int(payload.get("limit") or 5000)
    cursor = first_bucket_open_at_or_after(req_start, "1m")
    bars: list[dict[str, Any]] = []
    while cursor + 60 <= req_end:
        if has_open_session_in_range(cursor, cursor + 60):
            bars.append(
                {
                    "t": cursor,
                    "o": 5950.0,
                    "h": 5952.0,
                    "l": 5949.0,
                    "c": 5951.0,
                    "v": 10.0,
                }
            )
        cursor += 60
    return bars[-limit:] if len(bars) > limit else bars


def test_r06_ws_subscribe_triggers_upstream_signalr_and_streams_live_bars(
    tmp_path: Path,
) -> None:
    """Contract: /ws/market subscribe activates upstream SignalR and delivers live bar_update.

    Defect caught: WS subscribe updating broadcaster only without starting SignalR (P1-001/P2-002).
    """
    clock_now = BASE_T + 15  # Deterministic open CME session timestamp (no wall-clock drift)
    bar_open_ts = BASE_T
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-e2e-ws"})
        if request.url.path == "/api/History/retrieveBars":
            req_payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(req_payload)
            if not bars:
                bars = [
                    {
                        "t": bar_open_ts - 60,
                        "o": 5950.0,
                        "h": 5952.0,
                        "l": 5949.0,
                        "c": 5951.0,
                        "v": 10.0,
                    }
                ]
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        settings=settings,
        http_client=upstream,
        signalr_ws_factory=ws_factory,
        clock=lambda: clock_now,
    )

    try:
        with TestClient(
            app,
            client=("127.0.0.1", 50000),
            base_url="http://127.0.0.1:8000",
        ) as client:
            login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
            assert login_resp.status_code == 200
            csrf = login_resp.json()["csrf_token"]
            session_cookie = login_resp.cookies["topstep_session"]

            conn = client.post(
                "/api/connect",
                json={"userName": "alice", "apiKey": "key-e2e"},
                headers={"X-CSRF-Token": csrf},
            )
            assert conn.status_code == 200

            with client.websocket_connect(
                "/ws/market",
                headers={
                    "Host": "127.0.0.1:8000",
                    "Origin": "http://127.0.0.1:8000",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ) as ws:
                initial = ws.receive_json()
                assert initial["type"] == "feed_status"

                ws.send_json(
                    {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
                )
                sub_ack = ws.receive_json()
                assert sub_ack["type"] == "subscribed"

                # Wait briefly for SignalRMarketHub transport creation & recovery to reach LIVE.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if transports and app.state.live_feed_service.feed_state == "LIVE":
                        break
                    time.sleep(0.02)

                assert app.state.live_feed_service.feed_state == "LIVE"
                assert len(transports) == 1
                assert "CON.F.US.MES.Z26" in transports[0].subscribed_contracts

                # Push GatewayQuote (cumulative vol=12000) + GatewayTrade (vol=4.0) through SignalR
                # and verify client receives bar_update for CON.F.US.MES.Z26 with volume=4.0!
                portal = client.portal
                assert portal is not None
                portal.call(
                    transports[0].push_quote, "CON.F.US.MES.Z26", clock_now, 5955.0, 12000.0
                )
                portal.call(transports[0].push_trade, "CON.F.US.MES.Z26", clock_now, 5955.25, 4.0)

                deadline = time.monotonic() + 2.0
                received_bar = None
                while time.monotonic() < deadline:
                    remaining = max(0.05, deadline - time.monotonic())
                    msg = _receive_json_with_timeout(ws, remaining)
                    if msg.get("type") == "bar_update" and msg["bar"]["close"] == 5955.25:
                        received_bar = msg
                        break
                assert received_bar is not None
                assert received_bar["contract_id"] == "CON.F.US.MES.Z26"
                assert received_bar["bar"]["volume"] == 4.0
    finally:
        asyncio.run(upstream.aclose())


def test_r06_revoked_session_closes_passive_websocket_before_push(tmp_path: Path) -> None:
    """Contract: Revoking or expiring caller session stops pushes and closes passive WS (1008).

    Defect caught: Passive receive-only WebSocket continuing to stream after logout (P2-007).
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH, data_dir=tmp_path / "data")
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        csrf = login_resp.json()["csrf_token"]
        session_cookie = login_resp.cookies["topstep_session"]

        with client.websocket_connect(
            "/ws/market",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "Cookie": f"topstep_session={session_cookie}",
            },
        ) as ws:
            assert ws.receive_json()["type"] == "feed_status"
            ws.send_json(
                {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
            )
            assert ws.receive_json()["type"] == "subscribed"

            # Revoke session via /api/auth/logout while client stays completely passive on WS.
            logout_resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            assert logout_resp.status_code == 200

            # Attempt to push a bar update -> must NOT be delivered; WS closes with 1008!
            portal = client.portal
            assert portal is not None
            portal.call(
                app.state.ws_broadcaster.publish_bar,
                AggregatedBarEvent(
                    contract_id="CON.F.US.MES.Z26",
                    live_mode=False,
                    timeframe="1m",
                    bar=OHLCVBar(BASE_T, 5950.0, 5952.0, 5949.0, 5951.5, 8.0, is_closed=True),
                ),
            )
            with pytest.raises(WebSocketDisconnect) as exc_revoked:
                msg = ws.receive_json()
                while msg.get("type") == "feed_status":
                    msg = ws.receive_json()
                pytest.fail(f"Expected WebSocketDisconnect(1008), but received {msg!r}")
            assert exc_revoked.value.code == 1008


async def test_r06_large_historical_recovery_does_not_overflow_healthy_ws_client(
    tmp_path: Path,
) -> None:
    """Contract: Recovering >64 closed bars never overflows or disconnects healthy WS clients.

    Defect caught: Publishing all recovered historical closed bars individually into bounded client
    queues (maxsize=64) causing false 4008 disconnects (PASS1-002 / P2-003).
    """
    clock_now = BASE_T + (90 * 60) + 15  # 90 closed 1m bars available

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-large-rec"})
        if request.url.path == "/api/History/retrieveBars":
            req_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(req_payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", ws_client_queue_maxsize=64)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        broadcaster = WsBroadcaster(client_queue_maxsize=64, send_timeout_sec=1.0)
        healthy_ws = _FakeWsClient()
        broadcaster.register(healthy_ws, client_id="healthy-client")
        broadcaster.subscribe("healthy-client", "CON.F.US.MES.Z26", "1m")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-large-rec")
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                broadcaster=broadcaster,
                recovery_timeframes=("1m",),
                initial_backfill_bars=92,
                clock=lambda: clock_now,
            )
            await live_feed.set_active_contracts(["CON.F.US.MES.Z26"], live_mode=False)
            await live_feed.trigger_recovery(now_utc=clock_now)
            await asyncio.sleep(0.05)

            assert live_feed.feed_state == "LIVE"
            assert live_feed.recovered_historical_count >= 90
            assert broadcaster.client_count == 1
            assert broadcaster.slow_disconnect_count == 0
            resync_msgs = [m for m in healthy_ws.sent if m.get("type") == "resync_required"]
            assert len(resync_msgs) == 1
            assert resync_msgs[0]["reason"] == "recovery_complete"
            assert resync_msgs[0]["contract_id"] == "CON.F.US.MES.Z26"

            await broadcaster.close_all()
            await live_feed.stop()
    finally:
        sqlite_store.close()
