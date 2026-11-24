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
from app.services.ws_broadcaster import (
    WS_CLOSE_SLOW_CONSUMER,
    WS_CLOSE_TRY_AGAIN_LATER,
    WsBroadcaster,
    WsCapacityExceededError,
)
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

            # Trigger bar broadcast directly via app's broadcaster on the portal loop
            portal = client.portal
            assert portal is not None
            portal.call(
                app.state.ws_broadcaster.publish_bar,
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
                ),
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


def test_r06_ws_subscribe_rejects_bogus_and_unsafe_contract_ids_without_disrupting_live_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001/SEC-WS-001 rejects unsupported or unsafe contract_ids on /ws/market.

    Defect caught: /ws/market accepting CON.F.US.BOGUS.Z99 or 'A B' and passing them into
    LiveFeedService.subscribe_ws_contract, calling hub.update_contracts and breaking LIVE state.
    """
    clock_now = BASE_T + 15
    transports: list[_MockSignalRTransport] = []

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _MockSignalRTransport:
        t = _MockSignalRTransport()
        transports.append(t)
        return t

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ws-val"})
        if request.url.path == "/api/History/retrieveBars":
            req_payload = json.loads(request.content.decode("utf-8"))
            bars = _bars_for_history_payload(req_payload)
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
                json={"userName": "alice", "apiKey": "key-ws-val"},
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
                assert ws.receive_json()["type"] == "feed_status"
                ws.send_json(
                    {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
                )
                assert ws.receive_json()["type"] == "subscribed"

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if transports and app.state.live_feed_service.feed_state == "LIVE":
                        break
                    time.sleep(0.02)
                assert app.state.live_feed_service.feed_state == "LIVE"

                update_calls: list[list[str]] = []
                orig_update = app.state.live_feed_service.hub.update_contracts

                async def spy_update(contracts: Any, *, live_mode: bool) -> None:
                    update_calls.append(list(contracts))
                    await orig_update(contracts, live_mode=live_mode)

                monkeypatch.setattr(
                    app.state.live_feed_service.hub, "update_contracts", spy_update
                )

                for bad_cid in ("CON.F.US.BOGUS.Z99", "A B"):
                    ws.send_json({"type": "subscribe", "contract_id": bad_cid, "timeframe": "1m"})
                    resp = ws.receive_json()
                    while resp.get("type") in {"feed_status", "resync_required"}:
                        resp = ws.receive_json()
                    assert resp["type"] == "error", f"Expected error for {bad_cid!r}, got {resp!r}"

                assert update_calls == []
                assert app.state.live_feed_service.feed_state == "LIVE"
                assert app.state.live_feed_service.live_alerts_enabled is True
                assert app.state.live_feed_service.active_contracts == ("CON.F.US.MES.Z26",)
    finally:
        asyncio.run(upstream.aclose())


async def test_r06_ws_subscribe_enforces_per_client_and_total_ws_contract_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: P1-001/SEC-WS-001 enforces per-client and total WS contract subscription caps.

    Defect caught: Unbounded subscribe_ws_contract / WsBroadcaster.subscribe allowing arbitrary
    fan-out of subscriptions and SignalR contract churn.
    """
    broadcaster = WsBroadcaster(max_subscriptions_per_client=2)
    ws_client = _FakeWsClient()
    session = broadcaster.register(ws_client, client_id="capped-client")
    try:
        broadcaster.subscribe("capped-client", "CON.F.US.MES.Z26", "1m")
        broadcaster.subscribe("capped-client", "CON.F.US.MES.Z26", "5m")
        # Idempotent re-subscribe at cap succeeds.
        broadcaster.subscribe("capped-client", "CON.F.US.MES.Z26", "1m")
        with pytest.raises(ValueError, match=r"(?i)limit|cap|maximum"):
            broadcaster.subscribe("capped-client", "CON.F.US.MNQ.Z26", "1m")
        assert len(session.subscriptions) == 2
    finally:
        await broadcaster.close_all()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-cap"})
        if request.url.path == "/api/History/retrieveBars":
            req_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(req_payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
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
                initial_backfill_bars=2,
                max_ws_contracts=2,
                clock=lambda: BASE_T + 15,
            )
            try:
                await live_feed.subscribe_ws_contract("CON.F.US.MES.Z26")
                await live_feed.subscribe_ws_contract("CON.F.US.MNQ.Z26")
                # Re-subscribing an existing WS contract succeeds at cap.
                await live_feed.subscribe_ws_contract("CON.F.US.MES.Z26")

                update_calls: list[list[str]] = []
                orig_update = live_feed.hub.update_contracts

                async def spy_update(contracts: Any, *, live_mode: bool) -> None:
                    update_calls.append(list(contracts))
                    await orig_update(contracts, live_mode=live_mode)

                monkeypatch.setattr(live_feed.hub, "update_contracts", spy_update)

                with pytest.raises(ValueError, match=r"(?i)limit|cap|maximum"):
                    await live_feed.subscribe_ws_contract("CON.F.US.MYM.Z26")
                assert update_calls == []
                assert set(live_feed.active_contracts) == {
                    "CON.F.US.MES.Z26",
                    "CON.F.US.MNQ.Z26",
                }
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


def test_r06_ws_binary_frame_returns_structured_error_without_crash(tmp_path: Path) -> None:
    """Contract: SEC-WS-002 handles binary WS frames cleanly with a structured JSON error.

    Defect caught: websocket.receive_json() raising unhandled KeyError on binary WS send_bytes.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH, data_dir=tmp_path / "data")
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
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
            ws.send_bytes(b"x")
            err = ws.receive_json()
            assert err["type"] == "error"
            assert "Invalid WebSocket JSON payload" in err["detail"]

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def _ws_headers(session_cookie: str) -> dict[str, str]:
    return {
        "Host": "127.0.0.1:8000",
        "Origin": "http://127.0.0.1:8000",
        "Cookie": f"topstep_session={session_cookie}",
    }


def test_r06_subscribe_ack_not_emitted_before_upstream_admission_on_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12): Rejected /ws/market subscribe emits only 'error', never 'subscribed'.

    Defect caught: Emitting {'type': 'subscribed'} before live_feed.subscribe_ws_contract
    completes and leaking contract_id when hub.update_contracts raises RuntimeError/ValueError.
    """
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies["topstep_session"]

        live_feed: LiveFeedService = app.state.live_feed_service
        live_feed._max_ws_contracts = 1

        async def reject_runtime(contracts: Any, *, live_mode: bool) -> None:
            if contracts:
                raise RuntimeError("hub update failed with apiKey=super-secret-value")

        monkeypatch.setattr(live_feed.hub, "update_contracts", reject_runtime)

        with client.websocket_connect("/ws/market", headers=_ws_headers(session_cookie)) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "feed_status"

            ws.send_json(
                {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
            )
            ws.send_json({"type": "ping"})
            received: list[dict[str, Any]] = []
            while True:
                msg = _receive_json_with_timeout(ws, 2.0)
                if msg.get("type") == "pong":
                    break
                received.append(msg)

            assert [m["type"] for m in received] == ["error"]
            assert "super-secret-value" not in received[0]["detail"]
            assert "CON.F.US.MES.Z26" not in live_feed.active_contracts
            assert "CON.F.US.MES.Z26" not in live_feed._ws_contracts
            assert "CON.F.US.MES.Z26" not in app.state.ws_broadcaster.active_contract_ids()


async def test_r06_subscribe_ack_not_emitted_before_upstream_admission_rollback_preserves_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12): Selective rollback discards only newly added state on failure/cancel.

    Defect caught: Leaking failed contract state or over-broad rollback removing shared/explicit
    contracts on hub.update_contracts RuntimeError or asyncio.CancelledError.
    """
    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=False)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        broadcaster = WsBroadcaster(client_queue_maxsize=16, send_timeout_sec=1.0)
        async with httpx.AsyncClient() as client:
            auth = TopstepAuthService(settings, client)
            rest = TopstepRestClient(settings, auth, client)
            live_feed = LiveFeedService(
                settings,
                auth,
                rest,
                parquet_store,
                sqlite_store,
                broadcaster=broadcaster,
                recovery_timeframes=("1m",),
                max_ws_contracts=2,
            )
            shared_cid = "CON.F.US.MES.Z26"
            new_cid = "CON.F.US.MNQ.Z26"
            await live_feed.set_active_contracts([shared_cid], live_mode=False)

            async def fail_runtime(_contracts: Any, *, live_mode: bool) -> None:
                raise RuntimeError("upstream boom")

            monkeypatch.setattr(live_feed.hub, "update_contracts", fail_runtime)
            with pytest.raises(RuntimeError, match="upstream boom"):
                await live_feed.subscribe_ws_contract(new_cid)
            assert shared_cid in live_feed.active_contracts
            assert new_cid not in live_feed.active_contracts
            assert new_cid not in live_feed._ws_contracts

            async def fail_cancel(_contracts: Any, *, live_mode: bool) -> None:
                raise asyncio.CancelledError()

            monkeypatch.setattr(live_feed.hub, "update_contracts", fail_cancel)
            with pytest.raises(asyncio.CancelledError):
                await live_feed.subscribe_ws_contract(shared_cid)
            assert shared_cid in live_feed.active_contracts
            assert shared_cid not in live_feed._ws_contracts

            await broadcaster.close_all()
            await live_feed.stop()
    finally:
        sqlite_store.close()


def test_r06_subscribe_ack_not_emitted_before_upstream_admission_mid_await_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12): No bar_update or subscribed is sent before upstream admission completes,
    and mid-await disconnect exercises _commit_local_sub liveness rollback cleanly.

    Defect caught: Committing broadcaster.subscribe before await live_feed.subscribe_ws_contract,
    delivering premature bars and leaving orphaned contracts if client disconnects mid-await.
    """
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        portal = client.portal
        assert portal is not None
        entered_update = portal.call(asyncio.Event)
        release_update = portal.call(asyncio.Event)

        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies["topstep_session"]

        live_feed: LiveFeedService = app.state.live_feed_service
        broadcaster: WsBroadcaster = app.state.ws_broadcaster
        orig_update = live_feed.hub.update_contracts

        async def gated_update(contracts: Any, *, live_mode: bool) -> None:
            if "CON.F.US.MES.Z26" in contracts:
                entered_update.set()
                await asyncio.wait_for(release_update.wait(), timeout=5.0)
            await orig_update(contracts, live_mode=live_mode)

        monkeypatch.setattr(live_feed.hub, "update_contracts", gated_update)
        sent_during_await: list[dict[str, Any]] = []

        with client.websocket_connect("/ws/market", headers=_ws_headers(session_cookie)) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "feed_status"

            assert len(broadcaster._clients) == 1
            ws_sess = next(iter(broadcaster._clients.values()))
            orig_send_json = ws_sess.transport.send_json

            async def spy_send_json(data: Any) -> Any:
                sent_during_await.append(data)
                return await orig_send_json(data)

            monkeypatch.setattr(ws_sess.transport, "send_json", spy_send_json)

            ws.send_json(
                {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
            )
            portal.call(asyncio.wait_for, entered_update.wait(), 2.0)

            # Publish a bar while subscribe_ws_contract is suspended at await update_contracts.
            portal.call(
                broadcaster.publish_bar,
                AggregatedBarEvent(
                    contract_id="CON.F.US.MES.Z26",
                    live_mode=False,
                    timeframe="1m",
                    bar=OHLCVBar(BASE_T, 5950.0, 5952.0, 5949.0, 5951.0, 5.0, is_closed=True),
                ),
            )
            portal.call(asyncio.sleep, 0.05)

            # Unregister client while gated_update is suspended, then release so
            # subscribe_ws_contract completes update_contracts and triggers _commit_local_sub
            # liveness rollback before releasing _hub_lifecycle_lock.
            portal.call(broadcaster.unregister, ws_sess.client_id)
            portal.call(release_update.set)
            portal.call(asyncio.sleep, 0.05)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if live_feed.active_contracts == () and broadcaster.client_count == 0:
                break
            time.sleep(0.02)

        premature_bars = [
            m for m in sent_during_await if m.get("type") in {"bar_update", "subscribed"}
        ]
        assert premature_bars == []
        assert live_feed.active_contracts == ()
        assert live_feed.hub.contracts == ()
        assert broadcaster.active_contract_ids() == set()


def test_r06_ws_max_clients_cap_rejects_excess_and_releases_slot(tmp_path: Path) -> None:
    """Contract (NC-13): /ws/market enforces ws_max_clients with 1013 and releases slots cleanly.

    Defect caught: Unbounded concurrent WebSocket client connections exhausting queues/tasks.
    """
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
        ws_max_clients=2,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies["topstep_session"]
        headers = _ws_headers(session_cookie)

        with client.websocket_connect("/ws/market", headers=headers) as ws1:
            assert ws1.receive_json()["type"] == "feed_status"
            with client.websocket_connect("/ws/market", headers=headers) as ws2:
                assert ws2.receive_json()["type"] == "feed_status"

                # Unauthenticated / invalid-origin probe at capacity still gets 1008, not 1013.
                with pytest.raises(WebSocketDisconnect) as exc_unauth:
                    with client.websocket_connect(
                        "/ws/market",
                        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
                    ):
                        pass
                assert exc_unauth.value.code == 1008

                # (N+1)th valid client must be rejected with 1013 Try Again Later.
                with pytest.raises(WebSocketDisconnect) as exc_cap:
                    with client.websocket_connect("/ws/market", headers=headers):
                        pytest.fail("Expected WebSocketDisconnect(1013) for (N+1)th client")
                assert exc_cap.value.code == 1013

            # Once ws2 disconnects, slot opens and a new client can connect.
            with client.websocket_connect("/ws/market", headers=headers) as ws3:
                assert ws3.receive_json()["type"] == "feed_status"


def test_r06_subscribe_ack_concurrent_slot_contention_and_ordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12): Contending for last max_ws_contracts slot gives winner
    ['subscribed', 'feed_status'] in order and loser ['error'] only.
    """
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        portal = client.portal
        assert portal is not None
        entered_update = portal.call(asyncio.Event)
        release_update = portal.call(asyncio.Event)

        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        headers = _ws_headers(login_resp.cookies["topstep_session"])

        live_feed: LiveFeedService = app.state.live_feed_service
        live_feed._max_ws_contracts = 1
        orig_update = live_feed.hub.update_contracts

        async def gated_update(contracts: Any, *, live_mode: bool) -> None:
            if "CON.F.US.MES.Z26" in contracts:
                entered_update.set()
                await asyncio.wait_for(release_update.wait(), timeout=5.0)
            await orig_update(contracts, live_mode=live_mode)

        monkeypatch.setattr(live_feed.hub, "update_contracts", gated_update)

        with client.websocket_connect("/ws/market", headers=headers) as ws1:
            assert ws1.receive_json()["type"] == "feed_status"
            with client.websocket_connect("/ws/market", headers=headers) as ws2:
                assert ws2.receive_json()["type"] == "feed_status"

                ws1.send_json(
                    {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
                )
                portal.call(asyncio.wait_for, entered_update.wait(), 2.0)

                # While ws1 holds _hub_lifecycle_lock, ws2 contends for the same single slot.
                ws2.send_json(
                    {"type": "subscribe", "contract_id": "CON.F.US.MNQ.Z26", "timeframe": "1m"}
                )
                portal.call(asyncio.sleep, 0.03)
                portal.call(release_update.set)

                ws1.send_json({"type": "ping"})
                ws2.send_json({"type": "ping"})

                ws1_msgs: list[dict[str, Any]] = []
                while True:
                    m = _receive_json_with_timeout(ws1, 2.0)
                    if m.get("type") == "pong":
                        break
                    ws1_msgs.append(m)

                ws2_msgs: list[dict[str, Any]] = []
                while True:
                    m = _receive_json_with_timeout(ws2, 2.0)
                    if m.get("type") == "pong":
                        break
                    ws2_msgs.append(m)

                assert [m["type"] for m in ws1_msgs] == ["subscribed", "feed_status"]
                assert [m["type"] for m in ws2_msgs if m["type"] != "feed_status"] == ["error"]
                assert all(m["type"] != "subscribed" for m in ws2_msgs)
                assert live_feed.active_contracts == ("CON.F.US.MES.Z26",)


def test_r06_subscribe_session_expired_mid_await_closes_1008_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12 + F01): Session revocation mid-await closes WS 1008 and syncs upstream."""
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        portal = client.portal
        assert portal is not None
        entered_update = portal.call(asyncio.Event)
        release_update = portal.call(asyncio.Event)

        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        csrf = login_resp.json()["csrf_token"]
        headers = _ws_headers(login_resp.cookies["topstep_session"])

        live_feed: LiveFeedService = app.state.live_feed_service
        orig_update = live_feed.hub.update_contracts

        async def gated_update(contracts: Any, *, live_mode: bool) -> None:
            if "CON.F.US.MES.Z26" in contracts:
                entered_update.set()
                await asyncio.wait_for(release_update.wait(), timeout=5.0)
            await orig_update(contracts, live_mode=live_mode)

        monkeypatch.setattr(live_feed.hub, "update_contracts", gated_update)

        with client.websocket_connect("/ws/market", headers=headers) as ws:
            assert ws.receive_json()["type"] == "feed_status"
            ws.send_json(
                {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
            )
            portal.call(asyncio.wait_for, entered_update.wait(), 2.0)

            logout_resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
            assert logout_resp.status_code == 200
            portal.call(release_update.set)

            with pytest.raises(WebSocketDisconnect) as exc_expired:
                msg = _receive_json_with_timeout(ws, 2.0)
                pytest.fail(f"Expected WebSocketDisconnect(1008), got {msg!r}")
            assert exc_expired.value.code == 1008

        assert live_feed.active_contracts == ()
        assert live_feed.hub.contracts == ()


def test_r06_ws_max_clients_cap_accept_failure_releases_slot_without_leaking_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-13): Exception inside websocket.accept() frees client slot and leaks 0 tasks."""
    from starlette.websockets import WebSocket as StarletteWebSocket

    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
        ws_max_clients=1,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        headers = _ws_headers(login_resp.cookies["topstep_session"])
        broadcaster: WsBroadcaster = app.state.ws_broadcaster
        portal = client.portal
        assert portal is not None

        orig_accept = StarletteWebSocket.accept
        fail_once = [True]

        async def flaky_accept(self: StarletteWebSocket, *args: Any, **kwargs: Any) -> None:
            if fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("accept transport error")
            await orig_accept(self, *args, **kwargs)

        monkeypatch.setattr(StarletteWebSocket, "accept", flaky_accept)

        with pytest.raises((WebSocketDisconnect, RuntimeError)):
            with client.websocket_connect("/ws/market", headers=headers):
                pass

        assert broadcaster.client_count == 0
        leaked_senders = portal.call(
            lambda: [
                t.get_name()
                for t in asyncio.all_tasks()
                if t.get_name().startswith("ws-sender-") and not t.done()
            ]
        )
        assert leaked_senders == []

        with client.websocket_connect("/ws/market", headers=headers) as ws_ok:
            assert ws_ok.receive_json()["type"] == "feed_status"
            assert broadcaster.client_count == 1


async def test_r06_ws_broadcaster_capacity_and_deferred_sender_unit() -> None:
    """Contract (NC-12/NC-13): WsBroadcaster enforces max_clients, deferred sender, and
    non-mutating check_can_subscribe.
    """
    assert WS_CLOSE_TRY_AGAIN_LATER == 1013
    with pytest.raises(ValueError, match="max_clients"):
        WsBroadcaster(max_clients=0)

    broadcaster = WsBroadcaster(max_clients=1, max_subscriptions_per_client=1)
    ws1 = _FakeWsClient()
    ws2 = _FakeWsClient()

    sess1 = broadcaster.register(ws1, client_id="c1", defer_sender_start=True)
    assert broadcaster.is_registered("c1") is True
    assert sess1.sender_task is None

    # check_can_subscribe validates without mutating session.subscriptions
    cid, tf = broadcaster.check_can_subscribe(
        "c1", "CON.F.US.MES.Z26", "1m", require_registered=True
    )
    assert (cid, tf) == ("CON.F.US.MES.Z26", "1m")
    assert sess1.subscriptions == set()

    with pytest.raises(ValueError, match="already registered"):
        broadcaster.register(ws2, client_id="c1")

    with pytest.raises(WsCapacityExceededError):
        broadcaster.register(ws2, client_id="c2")
    assert broadcaster.client_count == 1

    # Enqueue before start_sender; sender_task starts cleanly on start_sender()
    assert broadcaster.send_to_client("c1", {"type": "feed_status", "feed_state": "LIVE"}) is True
    assert ws1.sent == []
    broadcaster.start_sender("c1")
    assert sess1.sender_task is not None
    await asyncio.sleep(0.02)
    assert ws1.sent == [{"type": "feed_status", "feed_state": "LIVE"}]

    with pytest.raises(ValueError, match="closed"):
        broadcaster.check_can_subscribe(
            "missing", "CON.F.US.MES.Z26", "1m", require_registered=True
        )

    await broadcaster.close_all()
    assert broadcaster.is_registered("c1") is False


def test_r06_concurrent_disconnect_sync_does_not_evict_in_flight_subscribe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract (NC-12 / P1-001): Concurrent client disconnect calling sync_ws_contracts()
    while Client 1 is inside subscribe_ws_contract must never evict Client 1's newly admitted cid.
    """
    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        portal = client.portal
        assert portal is not None
        entered_update = portal.call(asyncio.Event)
        release_update = portal.call(asyncio.Event)

        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        headers = _ws_headers(login_resp.cookies["topstep_session"])

        live_feed: LiveFeedService = app.state.live_feed_service
        orig_update = live_feed.hub.update_contracts

        async def gated_update(contracts: Any, *, live_mode: bool) -> None:
            if "CON.F.US.MES.Z26" in contracts:
                entered_update.set()
                await asyncio.wait_for(release_update.wait(), timeout=5.0)
            await orig_update(contracts, live_mode=live_mode)

        monkeypatch.setattr(live_feed.hub, "update_contracts", gated_update)

        with client.websocket_connect("/ws/market", headers=headers) as ws1:
            assert ws1.receive_json()["type"] == "feed_status"
            broadcaster: WsBroadcaster = app.state.ws_broadcaster
            ws1_cid = next(iter(broadcaster._clients))
            with client.websocket_connect("/ws/market", headers=headers) as ws2:
                assert ws2.receive_json()["type"] == "feed_status"
                ws2_cid = next(cid for cid in broadcaster._clients if cid != ws1_cid)

                ws1.send_json(
                    {"type": "subscribe", "contract_id": "CON.F.US.MES.Z26", "timeframe": "1m"}
                )
                portal.call(asyncio.wait_for, entered_update.wait(), 2.0)

                # Simulate ws2 disconnecting while ws1 holds _hub_lifecycle_lock, so
                # sync_ws_contracts() parks on _hub_lifecycle_lock before ws1 commits.
                async def _disconnect_and_park_sync() -> asyncio.Task[None]:
                    await broadcaster.unregister(ws2_cid)
                    return asyncio.create_task(live_feed.sync_ws_contracts())

                sync_task = portal.call(_disconnect_and_park_sync)
                portal.call(asyncio.sleep, 0.03)
                assert not sync_task.done()

                portal.call(release_update.set)
                portal.call(asyncio.wait_for, sync_task, 2.0)

            ws1.send_json({"type": "ping"})
            ws1_msgs: list[dict[str, Any]] = []
            while True:
                msg = _receive_json_with_timeout(ws1, 2.0)
                if msg.get("type") == "pong":
                    break
                ws1_msgs.append(msg)

            assert [m["type"] for m in ws1_msgs[:2]] == ["subscribed", "feed_status"]
            assert "CON.F.US.MES.Z26" in live_feed.active_contracts
            assert "CON.F.US.MES.Z26" in live_feed.hub.contracts


async def test_r06_signalr_hub_cancel_rollback_resyncs_hub_contracts(
    tmp_path: Path,
) -> None:
    """Contract (NC-12 / P1-002): Cancelling subscribe_ws_contract on a real SignalRMarketHub
    while awaiting dynamic subscribe ACK rolls back both live_feed and hub.contracts.
    """
    shared_cid = "CON.F.US.MES.Z26"
    new_cid = "CON.F.US.MNQ.Z26"
    clock_now = BASE_T + 15
    mnq_sub_sent = asyncio.Event()

    class _WithholdMnqAckTransport(_MockSignalRTransport):
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
                        args = [str(a) for a in frame.get("arguments", [])]
                        for arg in args:
                            self.subscribed_contracts.add(arg)
                        if new_cid in args:
                            mnq_sub_sent.set()
                            continue
                        if frame.get("invocationId") is not None:
                            ack = {"type": 3, "invocationId": frame["invocationId"]}
                            await self.incoming.put(json.dumps(ack) + SIGNALR_RECORD_SEPARATOR)
                        self.subscribed_event.set()
                    elif frame.get("target") in {
                        "UnsubscribeContractQuotes",
                        "UnsubscribeContractTrades",
                    }:
                        for arg in frame.get("arguments", []):
                            self.subscribed_contracts.discard(str(arg))
                        if frame.get("invocationId") is not None:
                            ack = {"type": 3, "invocationId": frame["invocationId"]}
                            await self.incoming.put(json.dumps(ack) + SIGNALR_RECORD_SEPARATOR)

    async def ws_factory(_url: str, _headers: dict[str, str]) -> _WithholdMnqAckTransport:
        return _WithholdMnqAckTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-cancel-hub"})
        if request.url.path == "/api/History/retrieveBars":
            req_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"success": True, "bars": _bars_for_history_payload(req_payload)},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data", signalr_enabled=True)
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(settings.data_dir, sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-cancel-hub")
            rest = TopstepRestClient(settings, auth, client)
            from app.topstep.signalr_hub import SignalRMarketHub

            hub = SignalRMarketHub(settings, auth_service=auth, ws_factory=ws_factory)
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
            try:
                await live_feed.set_active_contracts([shared_cid], live_mode=False)
                await live_feed.start()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if live_feed.feed_state == "LIVE":
                        break
                    await asyncio.sleep(0.01)
                assert live_feed.feed_state == "LIVE"
                assert live_feed.hub.contracts == (shared_cid,)

                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(
                        live_feed.subscribe_ws_contract(new_cid),
                        timeout=0.05,
                    )
                assert mnq_sub_sent.is_set()
                assert live_feed.active_contracts == (shared_cid,)
                assert new_cid not in live_feed._ws_contracts
                assert live_feed.hub.contracts == (shared_cid,)
            finally:
                await live_feed.stop()
    finally:
        sqlite_store.close()


