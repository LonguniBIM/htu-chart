"""Local WebSocket market broadcaster with R06 slow-consumer containment and open-bar coalescing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.engine.bar_aggregator import AggregatedBarEvent
from app.engine.session_calendar import normalize_timeframe

logger = logging.getLogger("topstep_chart.ws")

WS_CLOSE_SLOW_CONSUMER = 4008


class WsClientTransport(Protocol):
    """Minimal WebSocket interface satisfied by FastAPI WebSocket and test doubles."""

    async def send_json(self, data: Any) -> Any:
        """Serialize and send a JSON payload."""

    async def close(self, code: int = 1000, reason: str | None = None) -> Any:
        """Close the WebSocket connection."""


@dataclass
class _QueueSlot:
    payload: dict[str, Any]
    open_key: tuple[str, str] | None = None


class _CoalescingClientQueue:
    """Bounded per-client outbound queue with in-place provisional open-bar coalescing."""

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("ws_client_queue_maxsize must be >= 1")
        self._maxsize = maxsize
        self._items: deque[_QueueSlot] = deque()
        self._pending_open: dict[tuple[str, str], _QueueSlot] = {}
        self._has_items = asyncio.Event()
        self._closed = False

    def __len__(self) -> int:
        return len(self._items)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        self._items.clear()
        self._pending_open.clear()
        self._has_items.set()

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """Enqueue a payload, coalescing provisional open bars in place; False on overflow."""
        if self._closed:
            return False

        msg_type = payload.get("type")
        bar_data = payload.get("bar")
        if msg_type == "bar_update" and isinstance(bar_data, dict):
            contract_id = str(payload.get("contract_id", "")).strip().upper()
            timeframe = str(payload.get("timeframe", "")).strip().lower()
            key = (contract_id, timeframe)
            if bar_data.get("is_closed") is False:
                existing_slot = self._pending_open.get(key)
                if existing_slot is not None:
                    existing_slot.payload = payload
                    return True
                if len(self._items) >= self._maxsize:
                    return False
                slot = _QueueSlot(payload=payload, open_key=key)
                self._items.append(slot)
                self._pending_open[key] = slot
                self._has_items.set()
                return True

            # Closed bar: prevent any earlier provisional open bar from coalescing across it.
            self._pending_open.pop(key, None)

        if len(self._items) >= self._maxsize:
            return False
        self._items.append(_QueueSlot(payload=payload, open_key=None))
        self._has_items.set()
        return True

    async def get(self) -> dict[str, Any] | None:
        """Pop the next payload to send, or None once closed."""
        while True:
            if self._closed:
                return None
            if self._items:
                slot = self._items.popleft()
                if slot.open_key is not None and self._pending_open.get(slot.open_key) is slot:
                    self._pending_open.pop(slot.open_key, None)
                if not self._items and not self._closed:
                    self._has_items.clear()
                return slot.payload
            self._has_items.clear()
            await self._has_items.wait()


@dataclass
class WsClientSession:
    """Isolated per-client state, bounded queue, subscriptions, and sender task."""

    client_id: str
    transport: WsClientTransport
    queue: _CoalescingClientQueue
    subscriptions: set[tuple[str, str]] = field(default_factory=set)
    sender_task: asyncio.Task[None] | None = None
    closed_event: asyncio.Event = field(default_factory=asyncio.Event)
    disconnect_reason: str | None = None
    session_validator: Callable[[], bool] | None = None


async def _safe_close_ws(
    transport: WsClientTransport,
    *,
    code: int = WS_CLOSE_SLOW_CONSUMER,
    reason: str = "resync_required",
) -> None:
    try:
        await asyncio.wait_for(transport.close(code=code, reason=reason), timeout=0.5)
    except TypeError:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(transport.close(code), timeout=0.5)
    except Exception as exc:
        logger.debug("WebSocket close ignored error: %s", exc)


class WsBroadcaster:
    """Fan-out market broadcaster enforcing independent sender tasks and slow-client isolation."""

    def __init__(
        self,
        *,
        client_queue_maxsize: int = 64,
        send_timeout_sec: float = 5.0,
    ) -> None:
        if client_queue_maxsize < 1:
            raise ValueError("client_queue_maxsize must be >= 1")
        if send_timeout_sec <= 0:
            raise ValueError("send_timeout_sec must be > 0")
        self._queue_maxsize = client_queue_maxsize
        self._send_timeout_sec = send_timeout_sec
        self._clients: dict[str, WsClientSession] = {}
        self._teardown_tasks: set[asyncio.Task[None]] = set()
        self._slow_disconnect_count = 0

    @property
    def client_count(self) -> int:
        """Number of currently registered WebSocket clients."""
        return len(self._clients)

    @property
    def slow_disconnect_count(self) -> int:
        """Total clients disconnected due to queue overflow or send timeout."""
        return self._slow_disconnect_count

    def active_contract_ids(self) -> set[str]:
        """Return set of contract_ids currently subscribed by at least one client."""
        active: set[str] = set()
        for session in list(self._clients.values()):
            for cid, _tf in session.subscriptions:
                active.add(cid)
        return active

    def register(
        self,
        transport: WsClientTransport,
        *,
        client_id: str | None = None,
        session_validator: Callable[[], bool] | None = None,
    ) -> WsClientSession:
        """Register a connected WebSocket client and start its independent sender task."""
        cid = client_id or uuid.uuid4().hex
        session = WsClientSession(
            client_id=cid,
            transport=transport,
            queue=_CoalescingClientQueue(self._queue_maxsize),
            session_validator=session_validator,
        )
        session.sender_task = asyncio.create_task(
            self._sender_loop(session),
            name=f"ws-sender-{cid[:8]}",
        )
        self._clients[cid] = session
        return session

    def subscribe(self, client_id: str, contract_id: str, timeframe: str) -> tuple[str, str]:
        """Subscribe a registered client to (contract_id, timeframe)."""
        cid = contract_id.strip().upper()
        if not cid:
            raise ValueError("contract_id cannot be empty")
        tf = normalize_timeframe(timeframe)
        session = self._clients.get(client_id)
        if session is not None:
            session.subscriptions.add((cid, tf))
        return cid, tf

    def unsubscribe(self, client_id: str, contract_id: str, timeframe: str) -> None:
        """Unsubscribe a registered client from (contract_id, timeframe)."""
        cid = contract_id.strip().upper()
        tf = normalize_timeframe(timeframe)
        session = self._clients.get(client_id)
        if session is not None:
            session.subscriptions.discard((cid, tf))

    async def unregister(
        self,
        client_id: str,
        *,
        close_transport: bool = False,
        code: int = 1000,
        reason: str = "disconnected",
    ) -> None:
        """Unregister a client, close its queue, and stop its sender task."""
        session = self._clients.pop(client_id, None)
        if session is None:
            return
        session.queue.close()
        session.closed_event.set()
        task = session.sender_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if close_transport:
            await _safe_close_ws(session.transport, code=code, reason=reason)

    def _schedule_slow_disconnect(
        self,
        session: WsClientSession,
        reason: str,
        *,
        code: int = WS_CLOSE_SLOW_CONSUMER,
        count_as_slow: bool = True,
    ) -> None:
        if session.client_id not in self._clients:
            return
        self._clients.pop(session.client_id, None)
        if count_as_slow:
            self._slow_disconnect_count += 1
        session.disconnect_reason = reason
        session.queue.close()
        session.closed_event.set()

        async def _cleanup() -> None:
            task = session.sender_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await _safe_close_ws(
                session.transport,
                code=code,
                reason=reason,
            )

        cleanup_task = asyncio.create_task(_cleanup(), name=f"ws-drop-{session.client_id[:8]}")
        self._teardown_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._teardown_tasks.discard)

    def _is_session_valid(self, session: WsClientSession) -> bool:
        if session.session_validator is None:
            return True
        try:
            return bool(session.session_validator())
        except Exception:
            return False

    async def _sender_loop(self, session: WsClientSession) -> None:
        try:
            while True:
                payload = await session.queue.get()
                if payload is None:
                    break
                if not self._is_session_valid(session):
                    session.disconnect_reason = "Session expired or revoked"
                    self._clients.pop(session.client_id, None)
                    session.queue.close()
                    session.closed_event.set()
                    await _safe_close_ws(
                        session.transport,
                        code=1008,
                        reason=session.disconnect_reason,
                    )
                    break
                try:
                    await asyncio.wait_for(
                        session.transport.send_json(payload),
                        timeout=self._send_timeout_sec,
                    )
                except TimeoutError:
                    self._slow_disconnect_count += 1
                    session.disconnect_reason = "resync_required: send timeout"
                    self._clients.pop(session.client_id, None)
                    session.queue.close()
                    session.closed_event.set()
                    await _safe_close_ws(
                        session.transport,
                        code=WS_CLOSE_SLOW_CONSUMER,
                        reason=session.disconnect_reason,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._clients.pop(session.client_id, None)
                    session.queue.close()
                    session.closed_event.set()
                    break
        except asyncio.CancelledError:
            session.queue.close()
            session.closed_event.set()
            raise

    def send_to_client(self, client_id: str, payload: dict[str, Any]) -> bool:
        """Enqueue a payload to a specific client without awaiting socket I/O."""
        session = self._clients.get(client_id)
        if session is None:
            return False
        if not self._is_session_valid(session):
            self._schedule_slow_disconnect(
                session,
                "Session expired or revoked",
                code=1008,
                count_as_slow=False,
            )
            return False
        if not session.queue.enqueue(payload):
            self._schedule_slow_disconnect(session, "resync_required: queue overflow")
            return False
        return True

    def publish_bar(self, event: AggregatedBarEvent) -> None:
        """Broadcast a bar update non-blockingly to subscribed clients."""
        sub_key = (event.contract_id.upper(), event.timeframe.lower())
        payload: dict[str, Any] = {
            "type": "bar_update",
            "contract_id": event.contract_id,
            "live_mode": event.live_mode,
            "timeframe": event.timeframe,
            "is_recovered_historical": event.is_recovered_historical,
            "recovery_tag": "RECOVERED_HISTORICAL" if event.is_recovered_historical else None,
            "bar": {
                "time": event.bar.time,
                "open": event.bar.open,
                "high": event.bar.high,
                "low": event.bar.low,
                "close": event.bar.close,
                "volume": event.bar.volume,
                "is_closed": event.bar.is_closed,
            },
        }
        for session in list(self._clients.values()):
            if sub_key not in session.subscriptions:
                continue
            if not self._is_session_valid(session):
                self._schedule_slow_disconnect(
                    session,
                    "Session expired or revoked",
                    code=1008,
                    count_as_slow=False,
                )
                continue
            if not session.queue.enqueue(payload):
                self._schedule_slow_disconnect(session, "resync_required: queue overflow")

    def publish_feed_status(self, status_payload: dict[str, Any]) -> None:
        """Broadcast feed telemetry status non-blockingly to all connected clients."""
        payload = {"type": "feed_status", **status_payload}
        for session in list(self._clients.values()):
            if not self._is_session_valid(session):
                self._schedule_slow_disconnect(
                    session,
                    "Session expired or revoked",
                    code=1008,
                    count_as_slow=False,
                )
                continue
            if not session.queue.enqueue(payload):
                self._schedule_slow_disconnect(session, "resync_required: queue overflow")

    def publish_resync_required(
        self,
        reason: str,
        *,
        contract_id: str | None = None,
    ) -> None:
        """Notify connected clients that a resync/backfill refresh is required."""
        payload: dict[str, Any] = {
            "type": "resync_required",
            "reason": reason,
            "contract_id": contract_id,
        }
        for session in list(self._clients.values()):
            if not self._is_session_valid(session):
                self._schedule_slow_disconnect(
                    session,
                    "Session expired or revoked",
                    code=1008,
                    count_as_slow=False,
                )
                continue
            if not session.queue.enqueue(payload):
                self._schedule_slow_disconnect(session, "resync_required: queue overflow")

    async def close_all(self) -> None:
        """Cleanly disconnect all clients and await background teardown tasks."""
        for cid in list(self._clients):
            await self.unregister(cid, close_transport=True, code=1001, reason="server_shutdown")
        if self._teardown_tasks:
            await asyncio.gather(*tuple(self._teardown_tasks), return_exceptions=True)
