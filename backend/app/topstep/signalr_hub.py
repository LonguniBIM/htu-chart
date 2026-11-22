"""Read-only TopstepX SignalR Market Hub transport client (F03, R03, ARCH-003, SEC-001)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websockets

from app.config import Settings, validate_market_hub_url
from app.security import SecretRedactionFilter, redact_secrets
from app.topstep.auth import TopstepAuthService

logger = logging.getLogger("topstep_chart.signalr")

SIGNALR_RECORD_SEPARATOR = "\x1e"
SIGNALR_HANDSHAKE_FRAME = json.dumps({"protocol": "json", "version": 1}, separators=(",", ":")) + (
    SIGNALR_RECORD_SEPARATOR
)
SIGNALR_PING_FRAME = json.dumps({"type": 6}, separators=(",", ":")) + SIGNALR_RECORD_SEPARATOR


class _NoRedirectWsConnect(websockets.connect):
    """Reject all HTTP 3xx redirects so Market Hub connections never leave /hubs/market."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


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
    pre_connect_overflow_count: int = 0
    pre_connect_overflow_buckets: tuple[tuple[str, int], ...] = ()


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
class SignalRMalformedTickEvent:
    """Malformed market tick/quote payload rejected during SignalR parsing (R04)."""

    contract_id: str | None
    live_mode: bool
    reason: str
    issue_code: str
    raw_summary: str
    bar_time: int | None
    received_monotonic: float


@dataclass(frozen=True)
class SignalRErrorEvent:
    """Redacted transport or protocol error observed by SignalRMarketHub."""

    message: str
    occurred_monotonic: float


SignalREvent = (
    SignalRConnectedEvent
    | SignalRDisconnectedEvent
    | SignalRTickEvent
    | SignalRMalformedTickEvent
    | SignalRErrorEvent
)
EventCallback = Callable[[SignalREvent], Awaitable[None] | None]
QueueOverflowCallback = Callable[[SignalREvent, int], Awaitable[None] | None]


def _validate_market_hub_url(url: str) -> str:
    return validate_market_hub_url(url)


def _build_authenticated_ws_url(base_url: str, token: str) -> str:
    parsed = urlparse(base_url)
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "access_token"
    ]
    query_pairs.append(("access_token", token))
    return urlunparse(parsed._replace(query=urlencode(query_pairs)))


def _parse_signalr_timestamp(raw_ts: Any) -> float | None:
    max_epoch_sec = 4_102_444_800.0  # 2100-01-01T00:00:00Z
    if raw_ts is None or isinstance(raw_ts, bool):
        return None
    if isinstance(raw_ts, (int, float)):
        try:
            fval = float(raw_ts)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(fval):
            return None
        # Convert millisecond epochs if upstream sends JS epoch ms.
        if fval > 10_000_000_000:
            fval = fval / 1000.0
        if fval < 0.0 or fval > max_epoch_sec:
            return None
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
        ts_val = dt.timestamp()
        if ts_val < 0.0 or ts_val > max_epoch_sec:
            return None
        return ts_val
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


def _extract_item_timestamp(item: Mapping[str, Any]) -> int | None:
    raw_ts: Any = None
    for ts_key in ("timestamp", "lastUpdated", "t", "ts"):
        if ts_key in item and item[ts_key] is not None:
            raw_ts = item[ts_key]
            break
    ts_val = _parse_signalr_timestamp(raw_ts)
    return int(math.floor(ts_val)) if ts_val is not None else None


def parse_market_invocation_events(
    frame: Mapping[str, Any],
    *,
    live_mode: bool,
    received_monotonic: float,
) -> list[SignalRTickEvent | SignalRMalformedTickEvent]:
    """Extract ticks and malformed-tick quarantine events from a SignalR invocation."""
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
    payload_items: list[Any] = []
    if len(args) >= 2 and isinstance(args[0], str):
        default_contract = args[0].strip().upper() or None
        second = args[1]
        if isinstance(second, list):
            payload_items.extend(second)
        else:
            payload_items.append(second)
    elif isinstance(args[0], list):
        payload_items.extend(args[0])
    else:
        payload_items.append(args[0])

    events: list[SignalRTickEvent | SignalRMalformedTickEvent] = []
    for item in payload_items:
        raw_summary = json.dumps(item, default=str)[:512]
        if not isinstance(item, Mapping):
            events.append(
                SignalRMalformedTickEvent(
                    contract_id=default_contract,
                    live_mode=live_mode,
                    reason=(
                        f"Malformed {target or 'market'} payload element type: "
                        f"{type(item).__name__}"
                    ),
                    issue_code="malformed_tick_payload",
                    raw_summary=raw_summary,
                    bar_time=None,
                    received_monotonic=received_monotonic,
                )
            )
            continue
        # Require explicit contractId or callback contract; never fall back to root symbolId.
        raw_cid = item.get("contractId") or default_contract
        if not isinstance(raw_cid, str) or not raw_cid.strip():
            events.append(
                SignalRMalformedTickEvent(
                    contract_id=default_contract,
                    live_mode=live_mode,
                    reason="Missing or empty SignalR contractId",
                    issue_code="invalid_contract",
                    raw_summary=raw_summary,
                    bar_time=_extract_item_timestamp(item),
                    received_monotonic=received_monotonic,
                )
            )
            continue
        cid = raw_cid.strip().upper()
        best_bar_time = _extract_item_timestamp(item)

        raw_price: Any = None
        for price_key in ("lastPrice", "price", "tradePrice", "p", "c"):
            if price_key in item and item[price_key] is not None:
                raw_price = item[price_key]
                break
        if raw_price is None:
            events.append(
                SignalRMalformedTickEvent(
                    contract_id=cid,
                    live_mode=live_mode,
                    reason=f"Missing price in {target} tick",
                    issue_code="missing_price",
                    raw_summary=raw_summary,
                    bar_time=best_bar_time,
                    received_monotonic=received_monotonic,
                )
            )
            continue
        price = _parse_finite_number(raw_price)
        if price is None or price <= 0.0:
            events.append(
                SignalRMalformedTickEvent(
                    contract_id=cid,
                    live_mode=live_mode,
                    reason=f"Invalid price {raw_price!r} in {target} tick",
                    issue_code="invalid_price",
                    raw_summary=raw_summary,
                    bar_time=best_bar_time,
                    received_monotonic=received_monotonic,
                )
            )
            continue

        # GatewayQuote carries cumulative session volume; only GatewayTrade volume is incremental.
        if target_lower == "gatewayquote":
            volume = 0.0
        else:
            raw_vol: Any = None
            for vol_key in ("volume", "size", "qty", "v"):
                if vol_key in item and item[vol_key] is not None:
                    raw_vol = item[vol_key]
                    break
            if raw_vol is None:
                events.append(
                    SignalRMalformedTickEvent(
                        contract_id=cid,
                        live_mode=live_mode,
                        reason=f"Missing trade volume in {target} tick",
                        issue_code="missing_volume",
                        raw_summary=raw_summary,
                        bar_time=best_bar_time,
                        received_monotonic=received_monotonic,
                    )
                )
                continue
            parsed_vol = _parse_finite_number(raw_vol)
            if parsed_vol is None or parsed_vol < 0.0:
                events.append(
                    SignalRMalformedTickEvent(
                        contract_id=cid,
                        live_mode=live_mode,
                        reason=f"Invalid trade volume {raw_vol!r} in {target} tick",
                        issue_code="negative_volume"
                        if parsed_vol is not None and parsed_vol < 0.0
                        else "invalid_volume",
                        raw_summary=raw_summary,
                        bar_time=best_bar_time,
                        received_monotonic=received_monotonic,
                    )
                )
                continue
            volume = parsed_vol

        raw_ts: Any = None
        for ts_key in ("timestamp", "lastUpdated", "t", "ts"):
            if ts_key in item and item[ts_key] is not None:
                raw_ts = item[ts_key]
                break
        ts_sec = _parse_signalr_timestamp(raw_ts)
        if ts_sec is None:
            events.append(
                SignalRMalformedTickEvent(
                    contract_id=cid,
                    live_mode=live_mode,
                    reason=f"Invalid timestamp {raw_ts!r} in {target} tick",
                    issue_code="invalid_timestamp",
                    raw_summary=raw_summary,
                    bar_time=None,
                    received_monotonic=received_monotonic,
                )
            )
            continue

        events.append(
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
    return events


def extract_ticks_from_invocation(
    frame: Mapping[str, Any],
    *,
    live_mode: bool,
    received_monotonic: float,
) -> list[SignalRTickEvent]:
    """Extract normalized market ticks from a SignalR GatewayQuote/GatewayTrade frame."""
    return [
        evt
        for evt in parse_market_invocation_events(
            frame,
            live_mode=live_mode,
            received_monotonic=received_monotonic,
        )
        if isinstance(evt, SignalRTickEvent)
    ]


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
        on_queue_overflow: QueueOverflowCallback | None = None,
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
        self._on_queue_overflow = on_queue_overflow
        self._queue_overflow_count = 0

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
        self._session_connected_and_subscribed = False
        self._connected_token_digest: str | None = None
        self._stopping = False
        self._invocation_seq = 0
        self._last_event_monotonic: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._active_session_task: asyncio.Task[None] | None = None
        self._session_generation = 0
        self._reconnect_now_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._recent_tokens: deque[str] = deque(maxlen=4)
        self._pending_subscriptions: dict[str, tuple[str, str, float]] = {}
        self._pending_ack_futures: dict[str, asyncio.Future[None]] = {}
        self._subscribed_contracts: set[str] = set()
        self._sub_sync_lock = asyncio.Lock()
        self._ws_logger = logging.getLogger(f"topstep_chart.signalr.ws.{id(self)}")
        self._ws_logger.filters = [
            flt for flt in self._ws_logger.filters if not isinstance(flt, SecretRedactionFilter)
        ]
        self._ws_logger.addFilter(SecretRedactionFilter(self._collect_secrets))

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
    def queue_overflow_count(self) -> int:
        """Return total number of SignalR hub events that hit a full event_queue."""
        return self._queue_overflow_count

    @property
    def connected_token_digest(self) -> str | None:
        """One-way SHA-256 hex digest of the JWT used for the active SignalR session."""
        return self._connected_token_digest

    @property
    def has_pending_subscriptions(self) -> bool:
        """Return True iff any subscription invocation is still awaiting type=3 completion."""
        return bool(self._pending_subscriptions)

    def has_pending_subscription(self, contract_id: str) -> bool:
        """Return True iff contract_id still has a pending subscribe type=3 completion."""
        norm = contract_id.strip().upper()
        return any(cid == norm for _tgt, cid, _ts in self._pending_subscriptions.values())

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
        secrets_set.update(t for t in self._recent_tokens if t)
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
        return await _NoRedirectWsConnect(
            url,
            additional_headers=headers,
            open_timeout=self._connect_timeout,
            close_timeout=1.0,
            logger=self._ws_logger,
        )

    async def _get_token(self) -> str:
        if self._token_provider is not None:
            return await self._token_provider()
        if self._auth_service is not None:
            return await self._auth_service.ensure_token()
        raise RuntimeError("SignalRMarketHub requires token_provider or auth_service")

    def _surface_queue_overflow(self, dropped_event: SignalREvent) -> None:
        if self._event_queue is None:
            return
        try:
            oldest = self._event_queue.get_nowait()
        except asyncio.QueueEmpty:
            with contextlib.suppress(asyncio.QueueFull):
                self._event_queue.put_nowait(dropped_event)
            return
        self._event_queue.task_done()

        if isinstance(dropped_event, (SignalRDisconnectedEvent, SignalRConnectedEvent)):
            # Lifecycle transitions supersede stale queued ticks/errors so consumers resync.
            replacement: SignalREvent = dropped_event
        elif isinstance(
            oldest,
            (SignalRDisconnectedEvent, SignalRConnectedEvent, SignalRErrorEvent),
        ):
            # Keep an already-queued lifecycle or overflow/error marker intact.
            replacement = oldest
        elif isinstance(dropped_event, SignalRErrorEvent):
            replacement = dropped_event
        else:
            replacement = SignalRErrorEvent(
                message=(
                    "SignalR event_queue overflowed "
                    f"(count={self._queue_overflow_count}); resync required"
                ),
                occurred_monotonic=time.monotonic(),
            )
        with contextlib.suppress(asyncio.QueueFull):
            self._event_queue.put_nowait(replacement)

    async def _emit(self, event: SignalREvent) -> None:
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                self._queue_overflow_count += 1
                logger.warning(
                    "SignalR event_queue overflow (count=%d, dropped=%s)",
                    self._queue_overflow_count,
                    type(event).__name__,
                )
                self._surface_queue_overflow(event)
                if self._on_queue_overflow is not None:
                    cb_res = self._on_queue_overflow(event, self._queue_overflow_count)
                    if inspect.isawaitable(cb_res):
                        await cb_res
        if self._on_event is not None:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res

    def _next_invocation_id(self) -> str:
        self._invocation_seq += 1
        return str(self._invocation_seq)

    async def _send_raw(self, ws: SignalRWebSocketTransport, frame: str) -> None:
        send_deadline = min(self._connect_timeout, self._heartbeat_timeout)
        try:
            async with asyncio.timeout(send_deadline):
                async with self._send_lock:
                    await ws.send(frame)
        except TimeoutError as exc:
            raise TimeoutError(
                f"SignalR frame send exceeded {send_deadline}s deadline"
            ) from exc

    async def _send_invocation(
        self,
        ws: SignalRWebSocketTransport,
        target: str,
        arguments: list[Any],
        *,
        invocation_id: str | None = None,
    ) -> str:
        inv_id = invocation_id or self._next_invocation_id()
        payload = {
            "type": 1,
            "invocationId": inv_id,
            "target": target,
            "arguments": arguments,
        }
        frame = json.dumps(payload, separators=(",", ":")) + SIGNALR_RECORD_SEPARATOR
        await self._send_raw(ws, frame)
        return inv_id

    async def _subscribe_contract(
        self, ws: SignalRWebSocketTransport, contract_id: str
    ) -> list[asyncio.Future[None]]:
        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future[None]] = []
        for target in ("SubscribeContractQuotes", "SubscribeContractTrades"):
            inv_id = self._next_invocation_id()
            fut: asyncio.Future[None] = loop.create_future()
            # Consume unawaited future errors when teardown occurs before gather.
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self._pending_subscriptions[inv_id] = (target, contract_id, time.monotonic())
            self._pending_ack_futures[inv_id] = fut
            try:
                await self._send_invocation(ws, target, [contract_id], invocation_id=inv_id)
            except Exception:
                self._pending_subscriptions.pop(inv_id, None)
                self._pending_ack_futures.pop(inv_id, None)
                raise
            futures.append(fut)
        return futures

    async def _unsubscribe_contract(self, ws: SignalRWebSocketTransport, contract_id: str) -> None:
        await self._send_invocation(ws, "UnsubscribeContractQuotes", [contract_id])
        await self._send_invocation(ws, "UnsubscribeContractTrades", [contract_id])

    async def _sync_session_subscriptions(
        self, ws: SignalRWebSocketTransport
    ) -> list[asyncio.Future[None]]:
        ack_futures: list[asyncio.Future[None]] = []
        async with self._sub_sync_lock:
            while self._ws is ws:
                desired = set(self._contracts)
                to_unsub = sorted(self._subscribed_contracts - desired)
                to_sub = sorted(desired - self._subscribed_contracts)
                if not to_unsub and not to_sub:
                    return ack_futures
                for removed in to_unsub:
                    self._subscribed_contracts.discard(removed)
                    with contextlib.suppress(Exception):
                        await self._unsubscribe_contract(ws, removed)
                for added in to_sub:
                    self._subscribed_contracts.add(added)
                    try:
                        ack_futures.extend(await self._subscribe_contract(ws, added))
                    except Exception:
                        self._subscribed_contracts.discard(added)
                        raise
        return ack_futures

    async def update_contracts(
        self,
        contracts: Sequence[str],
        *,
        live_mode: bool | None = None,
    ) -> None:
        """Update active contract subscriptions and await dynamic subscribe ACK with timeout."""
        new_contracts = self._normalize_contracts(contracts)
        self._contracts = new_contracts

        if live_mode is not None and bool(live_mode) != self._live_mode:
            self._live_mode = bool(live_mode)
            await self.trigger_reconnect()
            return

        ws = self._ws
        if ws is not None and self._connected:
            try:
                ack_futures = await self._sync_session_subscriptions(ws)
                if ack_futures:
                    ack_timeout = min(self._connect_timeout, self._heartbeat_timeout)
                    await asyncio.wait_for(
                        asyncio.gather(*ack_futures),
                        timeout=ack_timeout,
                    )
            except Exception as exc:
                safe_err = self._redact(str(exc) or type(exc).__name__)
                logger.warning("SignalR dynamic contract sync failed: %s", safe_err)
                await self.trigger_reconnect()

    async def trigger_reconnect(self) -> None:
        """Force an immediate reconnect cycle (e.g. on JWT refresh)."""
        self._session_generation += 1
        self._reconnect_now_event.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=1.0)
        session_task = self._active_session_task
        if session_task is not None and not session_task.done():
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session_task

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
        self._session_generation += 1
        self._reconnect_now_event.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=1.0)
        session_task = self._active_session_task
        if session_task is not None and not session_task.done():
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_task
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._active_session_task = None
        self._task = None
        self._ws = None
        self._connected = False
        self._stopping = False

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

    def _clear_pending_subscriptions(self, exc: BaseException | None = None) -> None:
        self._pending_subscriptions.clear()
        for fut in self._pending_ack_futures.values():
            if not fut.done():
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.cancel()
        self._pending_ack_futures.clear()

    def _handle_completion_frame(self, frame: Mapping[str, Any], token: str | None) -> None:
        inv_id = frame.get("invocationId")
        inv_key = str(inv_id) if inv_id is not None else None
        sub_info = self._pending_subscriptions.pop(inv_key, None) if inv_key else None
        fut = self._pending_ack_futures.pop(inv_key, None) if inv_key else None
        err_msg = frame.get("error")
        if err_msg:
            safe_err = self._redact(str(err_msg), token)
            target_desc = (
                f"{sub_info[0]}({sub_info[1]})"
                if sub_info is not None
                else f"invocation={inv_id}"
            )
            rej_exc = ConnectionError(f"SignalR invocation rejected ({target_desc}): {safe_err}")
            if fut is not None and not fut.done():
                fut.set_exception(rej_exc)
            raise rej_exc
        if fut is not None and not fut.done():
            fut.set_result(None)

    def _build_frame_events(
        self,
        frame: Mapping[str, Any],
        *,
        recv_mono: float,
        token: str | None,
        live_mode: bool | None = None,
    ) -> list[SignalRTickEvent | SignalRMalformedTickEvent]:
        out: list[SignalRTickEvent | SignalRMalformedTickEvent] = []
        eff_live = self._live_mode if live_mode is None else live_mode
        for evt in parse_market_invocation_events(
            frame,
            live_mode=eff_live,
            received_monotonic=recv_mono,
        ):
            if isinstance(evt, SignalRMalformedTickEvent):
                evt = SignalRMalformedTickEvent(
                    contract_id=evt.contract_id,
                    live_mode=evt.live_mode,
                    reason=self._redact(evt.reason, token),
                    issue_code=evt.issue_code,
                    raw_summary=self._redact(evt.raw_summary, token),
                    bar_time=evt.bar_time,
                    received_monotonic=evt.received_monotonic,
                )
            out.append(evt)
        return out

    async def _emit_frame_events(
        self,
        frame: Mapping[str, Any],
        *,
        recv_mono: float,
        token: str | None,
        live_mode: bool | None = None,
    ) -> None:
        for evt in self._build_frame_events(
            frame, recv_mono=recv_mono, token=token, live_mode=live_mode
        ):
            await self._emit(evt)

    async def _run_single_session(self, session_gen: int | None = None) -> None:
        token: str | None = None
        ws: SignalRWebSocketTransport | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        disconnect_reason = "Connection closed"
        eff_gen = self._session_generation if session_gen is None else session_gen

        try:
            session_live = (
                self._auth_service.live if self._auth_service is not None else self._live_mode
            )
            token = await asyncio.wait_for(self._get_token(), timeout=self._connect_timeout)
            if eff_gen != self._session_generation or self._stopping:
                raise asyncio.CancelledError
            if token:
                self._recent_tokens.append(token)
                self._connected_token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            else:
                self._connected_token_digest = None
            if self._auth_service is not None:
                self._live_mode = session_live
            auth_url = _build_authenticated_ws_url(self._base_url, token)
            headers = {"Authorization": f"Bearer {token}"}

            ws = await asyncio.wait_for(
                self._ws_factory(auth_url, headers),
                timeout=self._connect_timeout,
            )
            if eff_gen != self._session_generation or self._stopping:
                raise asyncio.CancelledError
            self._ws = ws

            await asyncio.wait_for(ws.send(SIGNALR_HANDSHAKE_FRAME), timeout=self._connect_timeout)
            handshake_raw = await asyncio.wait_for(ws.recv(), timeout=self._connect_timeout)
            handshake_frames = parse_market_hub_frames(handshake_raw)
            if not handshake_frames:
                raise ConnectionError("SignalR handshake returned empty response")
            if handshake_frames[0].get("error"):
                raise ConnectionError(f"SignalR handshake error: {handshake_frames[0]['error']}")

            self._clear_pending_subscriptions()
            self._subscribed_contracts.clear()
            now_mono = time.monotonic()
            self._last_event_monotonic = now_mono

            await self._sync_session_subscriptions(ws)

            ack_timeout = min(self._connect_timeout, self._heartbeat_timeout)
            deadline_mono = time.monotonic() + ack_timeout
            pre_connect_cap = max(1, self._settings.signalr_recovery_queue_maxsize)
            pre_connect_events: deque[SignalREvent] = deque(maxlen=pre_connect_cap)
            pre_connect_overflow = 0
            overflow_buckets: set[tuple[str, int]] = set()
            while self._pending_subscriptions or set(self._contracts) != self._subscribed_contracts:
                if set(self._contracts) != self._subscribed_contracts:
                    await self._sync_session_subscriptions(ws)
                    continue
                remaining = deadline_mono - time.monotonic()
                if remaining <= 0:
                    pending_desc = ", ".join(
                        f"{tgt}({cid})" for tgt, cid, _ts in self._pending_subscriptions.values()
                    )
                    raise TimeoutError(
                        f"SignalR subscribe completion timed out after {ack_timeout}s "
                        f"waiting for: {pending_desc}"
                    )
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                recv_mono = time.monotonic()
                self._last_event_monotonic = recv_mono
                for frame in parse_market_hub_frames(raw_msg):
                    msg_type = frame.get("type")
                    if msg_type == 6:
                        continue
                    if msg_type == 3:
                        self._handle_completion_frame(frame, token)
                        continue
                    if msg_type == 7:
                        err_detail = frame.get("error") or "Server sent SignalR close frame"
                        disconnect_reason = self._redact(str(err_detail), token)
                        return
                    frame_events = self._build_frame_events(
                        frame, recv_mono=recv_mono, token=token, live_mode=session_live
                    )
                    for pre_evt in frame_events:
                        if len(pre_connect_events) >= pre_connect_cap:
                            pre_connect_overflow += 1
                            if isinstance(pre_evt, SignalRTickEvent):
                                overflow_buckets.add(
                                    (pre_evt.contract_id, int(pre_evt.timestamp_utc_sec))
                                )
                            elif (
                                isinstance(pre_evt, SignalRMalformedTickEvent)
                                and pre_evt.contract_id
                            ):
                                ts_val = (
                                    pre_evt.bar_time
                                    if pre_evt.bar_time is not None
                                    else int(time.time())
                                )
                                overflow_buckets.add((pre_evt.contract_id, ts_val))
                            continue
                        pre_connect_events.append(pre_evt)

            self._connected = True
            self._session_connected_and_subscribed = True
            await self._emit(
                SignalRConnectedEvent(
                    contracts=tuple(self._contracts),
                    live_mode=session_live,
                    connected_monotonic=time.monotonic(),
                    pre_connect_overflow_count=pre_connect_overflow,
                    pre_connect_overflow_buckets=tuple(sorted(overflow_buckets)),
                )
            )
            for pre_evt in pre_connect_events:
                await self._emit(pre_evt)

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            while not self._stopping:
                ack_wait_timeout: float | None = None
                if self._pending_subscriptions:
                    oldest_sent = min(
                        sent_ts for _t, _c, sent_ts in self._pending_subscriptions.values()
                    )
                    ack_wait_timeout = max(0.0, ack_timeout - (time.monotonic() - oldest_sent))
                    if ack_wait_timeout <= 0.0:
                        pending_desc = ", ".join(
                            f"{tgt}({cid})"
                            for tgt, cid, _ts in self._pending_subscriptions.values()
                        )
                        raise TimeoutError(
                            f"SignalR dynamic subscribe completion timed out after {ack_timeout}s "
                            f"waiting for: {pending_desc}"
                        )

                recv_task = asyncio.create_task(ws.recv())
                try:
                    done, _ = await asyncio.wait(
                        {recv_task, heartbeat_task},
                        timeout=ack_wait_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    if not recv_task.done():
                        recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await recv_task
                    raise

                if not done:
                    if not recv_task.done():
                        recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await recv_task
                    pending_desc = ", ".join(
                        f"{tgt}({cid})" for tgt, cid, _ts in self._pending_subscriptions.values()
                    )
                    raise TimeoutError(
                        f"SignalR dynamic subscribe completion timed out after {ack_timeout}s "
                        f"waiting for: {pending_desc}"
                    )

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
                    if msg_type == 3:
                        self._handle_completion_frame(frame, token)
                        continue
                    if msg_type == 7:
                        err_detail = frame.get("error") or "Server sent SignalR close frame"
                        disconnect_reason = self._redact(str(err_detail), token)
                        return
                    await self._emit_frame_events(
                        frame, recv_mono=recv_mono, token=token, live_mode=session_live
                    )

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
            self._connected_token_digest = None
            self._ws = None
            self._clear_pending_subscriptions(ConnectionError(disconnect_reason))
            self._subscribed_contracts.clear()
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
                self._session_connected_and_subscribed = False
                self._session_generation += 1
                session_gen = self._session_generation
                session_task = asyncio.create_task(
                    self._run_single_session(session_gen),
                    name="signalr-single-session",
                )
                self._active_session_task = session_task
                try:
                    await session_task
                except asyncio.CancelledError:
                    cur_task = asyncio.current_task()
                    if self._stopping or (cur_task is not None and cur_task.cancelling() > 0):
                        raise
                except Exception as exc:
                    safe_err = self._redact(str(exc) or type(exc).__name__)
                    logger.warning("SignalR Market Hub disconnected: %s", safe_err)
                finally:
                    if self._active_session_task is session_task:
                        self._active_session_task = None

                if self._stopping:
                    break

                if self._reconnect_now_event.is_set():
                    delay = self._initial_delay
                    wait_delay = 0.0
                elif self._session_connected_and_subscribed:
                    delay = self._initial_delay
                    wait_delay = delay
                    delay = min(self._max_delay, delay * 2.0)
                else:
                    wait_delay = delay
                    delay = min(self._max_delay, delay * 2.0)

                if wait_delay > 0:
                    try:
                        await asyncio.wait_for(self._reconnect_now_event.wait(), timeout=wait_delay)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            self._connected = False
            raise
