"""Value-first tests for /ws/market auth/origin enforcement and R06 slow-consumer containment."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.engine.bar_aggregator import AggregatedBarEvent
from app.engine.ohlcv import OHLCVBar
from app.main import create_app
from app.services.ws_broadcaster import WS_CLOSE_SLOW_CONSUMER, WsBroadcaster

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
            assert ws.receive_json() == {"type": "pong"}

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

    # Release gate and verify closed bars and latest open bar are all delivered cleanly.
    gate.set()
    await asyncio.sleep(0.03)
    closed_times = [m["bar"]["time"] for m in blocked_ws.sent if m["bar"]["is_closed"]]
    assert closed_times == [BASE_T, BASE_T + 60]
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
