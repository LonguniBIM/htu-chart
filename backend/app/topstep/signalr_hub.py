"""Read-only TopstepX SignalR Market Hub transport client (F03, R03, ARCH-003, SEC-001)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websockets

from app.config import Settings
from app.security import redact_secrets
from app.topstep.auth import TopstepAuthService

logger = logging.getLogger("topstep_chart.signalr")

SIGNALR_RECORD_SEPARATOR = "\x1e"
SIGNALR_HANDSHAKE_FRAME = json.dumps({"protocol": "json", "version": 1}, separators=(",", ":")) + (
    SIGNALR_RECORD_SEPARATOR
)
SIGNALR_PING_FRAME = json.dumps({"type": 6}, separators=(",", ":")) + SIGNALR_RECORD_SEPARATOR


class SignalRWebSocketTransport(Protocol):
    """Minimal async WebSocket transport interface for injectable SignalR testing."""

    async def send(self, data: str) -> Any:
        """Send a text frame to the SignalR server."""

    async def recv(self) -> str | bytes:
        """Receive the next frame from the SignalR server."""

    async def close(self) -> Any:
        """Close the underlying WebSocket transport."""


WsConnectFactory = Callable[[str, dict[str, str]], Awaitable[SignalRWebSocketTransport]]
TokenProvider = Callable[[], Awaitable[str]]
SecretsProvider = Callable[[], Iterable[str]]


@dataclass(frozen=True)
class SignalRConnectedEvent:
    """Lifecycle event emitted after SignalR handshake and initial subscriptions."""

    contracts: tuple[str, ...]
    live_mode: bool
    connected_monotonic: float


@dataclass(frozen=True)
class SignalRDisconnectedEvent:
    """Lifecycle event emitted when the SignalR connection drops or shuts down."""

    reason: str
    will_reconnect: bool
    disconnected_monotonic: float


@dataclass(frozen=True)
class SignalRTickEvent:
    """Normalized market tick/quote event parsed from SignalR Market Hub."""

    contract_id: str
    live_mode: bool
    timestamp_utc_sec: float
    price: float
    volume: float
    source_target: str
    received_monotonic: float


@dataclass(frozen=True)
class SignalRErrorEvent:
    """Redacted transport or protocol error observed by SignalRMarketHub."""

    message: str
    occurred_monotonic: float


SignalREvent = (
    SignalRConnectedEvent | SignalRDisconnectedEvent | SignalRTickEvent | SignalRErrorEvent
)
EventCallback = Callable[[SignalREvent], Awaitable[None] | None]


def _validate_market_hub_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("SignalR Market Hub URL must use ws:// or wss:// with a valid host")
    if not parsed.path.lower().rstrip("/").endswith("/hubs/market"):
        raise ValueError("SignalR URL must target the read-only Market Hub (/hubs/market)")
    return cleaned


def _build_authenticated_ws_url(base_url: str, token: str) -> str:
    parsed = urlparse(base_url)
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "access_token"
    ]
    query_pairs.append(("access_token", token))
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _parse_signalr_timestamp(raw_ts: Any) -> float | None:
    if raw_ts is None:
        return datetime.now(UTC).timestamp()
    if isinstance(raw_ts, bool):
        return None
    if isinstance(raw_ts, (int, float)):
        fval = float(raw_ts)
        if not math.isfinite(fval):
            return None
        # Convert millisecond epochs if upstream sends JS epoch ms.
        if fval > 10_000_000_000:
            return fval / 1000.0
        return fval
    if isinstance(raw_ts, str):
        cleaned = raw_ts.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if dt.tzinfo is None or dt.utcoffset() is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def _parse_finite_number(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    try:
        fval = float(val)
    except OverflowError:
        return None
    if not math.isfinite(fval):
        return None
    return fval


def parse_market_hub_frames(raw_message: str | bytes) -> list[dict[str, Any]]:
    """Decode one or more SignalR JSON frames delimited by record separator 0x1e."""
    if isinstance(raw_message, bytes):
        text = raw_message.decode("utf-8", errors="replace")
    else:
        text = raw_message

    frames: list[dict[str, Any]] = []
    for chunk in text.split(SIGNALR_RECORD_SEPARATOR):
        stripped = chunk.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            frames.append(parsed)
    return frames


def extract_ticks_from_invocation(
    frame: Mapping[str, Any],
    *,
    live_mode: bool,
    received_monotonic: float,
) -> list[SignalRTickEvent]:
    """Extract normalized market ticks from a SignalR GatewayQuote/GatewayTrade frame."""
    if frame.get("type") != 1:
        return []
    target = str(frame.get("target") or "")
    target_lower = target.lower()
    if target_lower not in {"gatewayquote", "gatewaytrade"}:
        return []

    args = frame.get("arguments")
    if not isinstance(args, list) or not args:
        return []

    default_contract: str | None = None
    payload_items: list[Mapping[str, Any]] = []
    if len(args) >= 2 and isinstance(args[0], str):
        default_contract = args[0].strip().upper() or None
        second = args[1]
        if isinstance(second, Mapping):
            payload_items.append(second)
        elif isinstance(second, list):
            payload_items.extend(item for item in second if isinstance(item, Mapping))
    elif isinstance(args[0], Mapping):
        payload_items.append(args[0])
    elif isinstance(args[0], list):
        payload_items.extend(item for item in args[0] if isinstance(item, Mapping))

    ticks: list[SignalRTickEvent] = []
    for item in payload_items:
        raw_cid = item.get("contractId") or item.get("symbolId") or default_contract
        if not isinstance(raw_cid, str) or not raw_cid.strip():
            continue
        cid = raw_cid.strip().upper()

        raw_price: Any = None
        for price_key in ("lastPrice", "price", "tradePrice", "p", "c"):
            if price_key in item and item[price_key] is not None:
                raw_price = item[price_key]
                break
        if raw_price is None:
            continue
        price = _parse_finite_number(raw_price)
        if price is None or price <= 0.0:
            continue

        raw_vol: Any = 0.0
        for vol_key in ("volume", "size", "qty", "v"):
            if vol_key in item and item[vol_key] is not None:
                raw_vol = item[vol_key]
                break
        volume = _parse_finite_number(raw_vol)
        if volume is None or volume < 0.0:
            continue

        raw_ts: Any = None
        for ts_key in ("timestamp", "lastUpdated", "t", "ts"):
            if ts_key in item and item[ts_key] is not None:
                raw_ts = item[ts_key]
                break
        ts_sec = _parse_signalr_timestamp(raw_ts)
        if ts_sec is None:
            continue

        ticks.append(
            SignalRTickEvent(
                contract_id=cid,
                live_mode=live_mode,
                timestamp_utc_sec=ts_sec,
                price=price,
                volume=volume,
                source_target=target,
                received_monotonic=received_monotonic,
            )
        )
    return ticks


class SignalRMarketHub:
    """Single read-only SignalR Market Hub connection with backoff reconnect and heartbeat."""

    def __init__(
        self,
        settings: Settings,
        *,
        auth_service: TopstepAuthService | None = None,
        token_provider: TokenProvider | None = None,
        extra_secrets_provider: SecretsProvider | None = None,
        ws_factory: WsConnectFactory | None = None,
        on_event: EventCallback | None = None,
        event_queue: asyncio.Queue[SignalREvent] | None = None,
        contracts: Sequence[str] = (),
        live_mode: bool | None = None,
        reconnect_initial_delay_sec: float | None = None,
        reconnect_max_delay_sec: float | None = None,
        ping_interval_sec: float | None = None,
        heartbeat_timeout_sec: float | None = None,
        connect_timeout_sec: float | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = _validate_market_hub_url(settings.topstepx_rtc_url)
        self._auth_service = auth_service
        self._token_provider = token_provider
        self._extra_secrets_provider = extra_secrets_provider
        self._ws_factory = ws_factory or self._default_ws_connect
        self._on_event = on_event
        self._event_queue = event_queue

        self._contracts: list[str] = self._normalize_contracts(contracts)
        if live_mode is not None:
            self._live_mode = bool(live_mode)
        elif auth_service is not None:
            self._live_mode = auth_service.live
        else:
            self._live_mode = settings.topstepx_live

        self._initial_delay = (
            reconnect_initial_delay_sec
            if reconnect_initial_delay_sec is not None
            else settings.signalr_reconnect_initial_delay_sec
        )
        self._max_delay = (
            reconnect_max_delay_sec
            if reconnect_max_delay_sec is not None
            else settings.signalr_reconnect_max_delay_sec
        )
        self._ping_interval = (
            ping_interval_sec
            if ping_interval_sec is not None
            else settings.signalr_ping_interval_sec
        )
        self._heartbeat_timeout = (
            heartbeat_timeout_sec
            if heartbeat_timeout_sec is not None
            else settings.signalr_heartbeat_timeout_sec
        )
        self._connect_timeout = (
            connect_timeout_sec
            if connect_timeout_sec is not None
            else settings.upstream_connect_timeout_sec
        )

        self._ws: SignalRWebSocketTransport | None = None
        self._connected = False
        self._stopping = False
        self._invocation_seq = 0
        self._last_event_monotonic: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._reconnect_now_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    @staticmethod
    def _normalize_contracts(contracts: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in contracts:
            cleaned = str(raw).strip().upper()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return out

    @property
    def redacted_url(self) -> str:
        """Return Market Hub URL without query secrets."""
        return self._base_url

    @property
    def is_connected(self) -> bool:
        """Return True iff SignalR handshake succeeded and socket is active."""
        return self._connected

    @property
    def last_event_monotonic(self) -> float | None:
        """Return monotonic timestamp of the latest received SignalR frame."""
        return self._last_event_monotonic

    @property
    def contracts(self) -> tuple[str, ...]:
        """Currently registered contract IDs."""
        return tuple(self._contracts)

    @property
    def live_mode(self) -> bool:
        """Active live/sim feed mode."""
        return self._live_mode

    def _collect_secrets(self, *extra: str | None) -> tuple[str, ...]:
        secrets_set: set[str] = {
            s
            for s in (
                *extra,
                self._settings.topstepx_api_key,
                self._settings.app_password_hash,
                self._settings.app_session_secret,
            )
            if s
        }
        if self._auth_service is not None:
            secrets_set.update(self._auth_service.active_redaction_secrets())
        if self._extra_secrets_provider is not None:
            secrets_set.update(s for s in self._extra_secrets_provider() if s)
        return tuple(sorted(secrets_set, key=len, reverse=True))

    def _redact(self, text: str, *extra: str | None) -> str:
        return redact_secrets(text, self._collect_secrets(*extra))

    async def _default_ws_connect(
        self,
        url: str,
        headers: dict[str, str],
    ) -> SignalRWebSocketTransport:
        return await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=self._connect_timeout,
            close_timeout=1.0,
        )

    async def _get_token(self) -> str:
        if self._token_provider is not None:
            return await self._token_provider()
        if self._auth_service is not None:
            return await self._auth_service.ensure_token()
        raise RuntimeError("SignalRMarketHub requires token_provider or auth_service")

    async def _emit(self, event: SignalREvent) -> None:
        if self._event_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._event_queue.put_nowait(event)
        if self._on_event is not None:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res

    def _next_invocation_id(self) -> str:
        self._invocation_seq += 1
        return str(self._invocation_seq)

    async def _send_raw(self, ws: SignalRWebSocketTransport, frame: str) -> None:
        async with self._send_lock:
            await ws.send(frame)

    async def _send_invocation(
        self,
        ws: SignalRWebSocketTransport,
        target: str,
        arguments: list[Any],
    ) -> None:
        payload = {
            "type": 1,
            "invocationId": self._next_invocation_id(),
            "target": target,
            "arguments": arguments,
        }
        frame = json.dumps(payload, separators=(",", ":")) + SIGNALR_RECORD_SEPARATOR
        await self._send_raw(ws, frame)

    async def _subscribe_contract(self, ws: SignalRWebSocketTransport, contract_id: str) -> None:
        await self._send_invocation(ws, "SubscribeContractQuotes", [contract_id])
        await self._send_invocation(ws, "SubscribeContractTrades", [contract_id])

    async def _unsubscribe_contract(self, ws: SignalRWebSocketTransport, contract_id: str) -> None:
        await self._send_invocation(ws, "UnsubscribeContractQuotes", [contract_id])
        await self._send_invocation(ws, "UnsubscribeContractTrades", [contract_id])

    async def update_contracts(
        self,
        contracts: Sequence[str],
        *,
        live_mode: bool | None = None,
    ) -> None:
        """Update active contract subscriptions (unsubscribe removed, subscribe added)."""
        new_contracts = self._normalize_contracts(contracts)
        old_set = set(self._contracts)
        new_set = set(new_contracts)
        self._contracts = new_contracts

        if live_mode is not None and bool(live_mode) != self._live_mode:
            self._live_mode = bool(live_mode)
            await self.trigger_reconnect()
            return

        ws = self._ws
        if ws is not None and self._connected:
            for removed in sorted(old_set - new_set):
                with contextlib.suppress(Exception):
                    await self._unsubscribe_contract(ws, removed)
            for added in sorted(new_set - old_set):
                with contextlib.suppress(Exception):
                    await self._subscribe_contract(ws, added)

    async def trigger_reconnect(self) -> None:
        """Force an immediate reconnect cycle (e.g. on JWT refresh)."""
        self._reconnect_now_event.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=1.0)

    def start(self) -> asyncio.Task[None]:
        """Start the background SignalR Market Hub loop if not already running."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self.run(), name="signalr-market-hub")
        return self._task

    async def stop(self) -> None:
        """Stop the background loop and close the active WebSocket cleanly."""
        self._stopping = True
        self._reconnect_now_event.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=1.0)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._ws = None
        self._connected = False

    async def _close_ws(self, ws: SignalRWebSocketTransport | None) -> None:
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=1.0)

    async def _heartbeat_loop(self, ws: SignalRWebSocketTransport) -> None:
        check_interval = max(0.05, min(self._ping_interval, self._heartbeat_timeout / 2.0))
        while not self._stopping:
            await asyncio.sleep(check_interval)
            now_mono = time.monotonic()
            last_mono = self._last_event_monotonic
            if last_mono is not None and (now_mono - last_mono) > self._heartbeat_timeout:
                raise TimeoutError(
                    f"SignalR Market Hub heartbeat timeout after {self._heartbeat_timeout}s"
                )
            await self._send_raw(ws, SIGNALR_PING_FRAME)

    async def _run_single_session(self) -> None:
        token: str | None = None
        ws: SignalRWebSocketTransport | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        disconnect_reason = "Connection closed"

        try:
            token = await asyncio.wait_for(self._get_token(), timeout=self._connect_timeout)
            if self._auth_service is not None:
                self._live_mode = self._auth_service.live
            auth_url = _build_authenticated_ws_url(self._base_url, token)
            headers = {"Authorization": f"Bearer {token}"}

            ws = await asyncio.wait_for(
                self._ws_factory(auth_url, headers),
                timeout=self._connect_timeout,
            )
            self._ws = ws

            await asyncio.wait_for(ws.send(SIGNALR_HANDSHAKE_FRAME), timeout=self._connect_timeout)
            handshake_raw = await asyncio.wait_for(ws.recv(), timeout=self._connect_timeout)
            handshake_frames = parse_market_hub_frames(handshake_raw)
            if not handshake_frames:
                raise ConnectionError("SignalR handshake returned empty response")
            if handshake_frames[0].get("error"):
                raise ConnectionError(f"SignalR handshake error: {handshake_frames[0]['error']}")

            now_mono = time.monotonic()
            self._last_event_monotonic = now_mono
            self._connected = True

            for cid in tuple(self._contracts):
                await self._subscribe_contract(ws, cid)

            await self._emit(
                SignalRConnectedEvent(
                    contracts=tuple(self._contracts),
                    live_mode=self._live_mode,
                    connected_monotonic=now_mono,
                )
            )

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            while not self._stopping:
                recv_task = asyncio.create_task(ws.recv())
                try:
                    done, _ = await asyncio.wait(
                        {recv_task, heartbeat_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    if not recv_task.done():
                        recv_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await recv_task
                    raise

                if heartbeat_task in done:
                    if not recv_task.done():
                        recv_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await recv_task
                    exc = heartbeat_task.exception()
                    if exc is not None:
                        raise exc
                    break

                raw_msg = recv_task.result()
                recv_mono = time.monotonic()
                self._last_event_monotonic = recv_mono
                frames = parse_market_hub_frames(raw_msg)
                for frame in frames:
                    msg_type = frame.get("type")
                    if msg_type == 6:
                        continue
                    if msg_type == 7:
                        err_detail = frame.get("error") or "Server sent SignalR close frame"
                        disconnect_reason = self._redact(str(err_detail), token)
                        return
                    for tick in extract_ticks_from_invocation(
                        frame,
                        live_mode=self._live_mode,
                        received_monotonic=recv_mono,
                    ):
                        await self._emit(tick)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            disconnect_reason = self._redact(str(exc) or type(exc).__name__, token)
            await self._emit(
                SignalRErrorEvent(
                    message=disconnect_reason,
                    occurred_monotonic=time.monotonic(),
                )
            )
            raise
        finally:
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
            was_connected = self._connected
            self._connected = False
            self._ws = None
            await self._close_ws(ws)
            if was_connected:
                await self._emit(
                    SignalRDisconnectedEvent(
                        reason=disconnect_reason,
                        will_reconnect=not self._stopping,
                        disconnected_monotonic=time.monotonic(),
                    )
                )

    async def run(self) -> None:
        """Connect and maintain the SignalR Market Hub session with exponential backoff."""
        delay = self._initial_delay
        try:
            while not self._stopping:
                self._reconnect_now_event.clear()
                session_connected = False
                try:
                    await self._run_single_session()
                    session_connected = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    safe_err = self._redact(str(exc) or type(exc).__name__)
                    logger.warning("SignalR Market Hub disconnected: %s", safe_err)

                if self._stopping:
                    break

                if self._reconnect_now_event.is_set() or session_connected:
                    delay = self._initial_delay

                wait_delay = 0.0 if self._reconnect_now_event.is_set() else delay
                if wait_delay > 0:
                    try:
                        await asyncio.wait_for(self._reconnect_now_event.wait(), timeout=wait_delay)
                    except TimeoutError:
                        pass
                delay = min(self._max_delay, delay * 2.0)
        except asyncio.CancelledError:
            self._connected = False
            raise
