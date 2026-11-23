"""Value-first offline tests for F07 durable SQLite alert outbox, AlertEngine, and Telegram."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError

from app.config import Settings, load_settings
from app.engine.alert_engine import (
    AlertCandidate,
    AlertDirection,
    AlertEngine,
    cme_weekend_window_bounds,
    format_entry_alert_message,
    is_cme_weekend_window,
)
from app.engine.bar_aggregator import AggregatedBarEvent, BarAggregator
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import (
    NY_TZ,
    first_bucket_open_at_or_after,
    has_open_session_in_range,
)
from app.main import create_app
from app.notify.telegram import TelegramClient
from app.services.alert_service import AlertService, TelegramNotConfiguredError
from app.services.live_feed_service import LiveFeedService
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import SIGNALR_RECORD_SEPARATOR, SignalRMarketHub

CID_MES = "CON.F.US.MES.Z26"
CID_MNQ = "CON.F.US.MNQ.Z26"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)


class _MutableClock:
    def __init__(self, initial_utc: int) -> None:
        self.now = initial_utc

    def __call__(self) -> int:
        return self.now


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
                elif (
                    frame.get("target") in {"SubscribeContractQuotes", "SubscribeContractTrades"}
                    and frame.get("invocationId") is not None
                ):
                    comp = {"type": 3, "invocationId": frame["invocationId"]}
                    await self.incoming.put(json.dumps(comp) + SIGNALR_RECORD_SEPARATOR)

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


def _make_settings(
    tmp_path: Path,
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
    alert_ttl_seconds: int = 90,
    alert_max_retries: int = 3,
    backoff: list[float] | None = None,
    key_updated_at: str | None = None,
    key_rotation_days: int = 30,
    key_rotation_enforce: bool = False,
) -> Settings:
    eff_bot_token = "123456:tg-secret-token-abcdef" if bot_token is None else bot_token
    eff_chat_id = "-1001234567890" if chat_id is None else chat_id
    return Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token=eff_bot_token,
        telegram_chat_id=eff_chat_id,
        telegram_api_base_url="https://api.telegram.org",
        alert_ttl_seconds=alert_ttl_seconds,
        alert_max_retries=alert_max_retries,
        alert_retry_backoff_sec=backoff if backoff is not None else [2.0, 5.0, 15.0],
        topstepx_key_updated_at=key_updated_at,
        key_rotation_days=key_rotation_days,
        key_rotation_enforce=key_rotation_enforce,
    )


def _closed_bar_event(
    *,
    contract_id: str = CID_MES,
    live_mode: bool = True,
    timeframe: str = "5m",
    bar_time: int = 1_762_783_200,
    close_price: float = 5950.25,
    is_closed: bool = True,
    is_recovered_historical: bool = False,
) -> AggregatedBarEvent:
    return AggregatedBarEvent(
        contract_id=contract_id,
        live_mode=live_mode,
        timeframe=timeframe,
        bar=OHLCVBar(
            time=bar_time,
            open=close_price - 1.0,
            high=close_price + 0.5,
            low=close_price - 1.5,
            close=close_price,
            volume=42.0,
            is_closed=is_closed,
        ),
        is_recovered_historical=is_recovered_historical,
        root_symbol="MES" if "MES" in contract_id else "MNQ",
    )


def _sample_candidate(
    *,
    strategy_id: str = "fvg_sweep",
    strategy_version: str = "1.0.0",
    live_mode: bool = True,
    contract_id: str = CID_MES,
    timeframe: str = "5m",
    bar_time: int = 1_762_783_200,
    direction: AlertDirection = "LONG",
) -> AlertCandidate:
    return AlertCandidate(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        live_mode=live_mode,
        contract_id=contract_id,
        root_symbol="MES" if "MES" in contract_id else "MNQ",
        timeframe=timeframe,
        bar_time=bar_time,
        direction=direction,
        alert_kind="ENTRY",
        reason="Bullish FVG + Liquidity Sweep",
        entry_price=5950.25,
        stop_loss=5945.0,
        take_profit=5960.75,
        risk_reward=2.0,
    )


def _build_service_stack(
    tmp_path: Path,
    *,
    settings: Settings,
    tg_handler: Any,
    clock: _MutableClock,
) -> tuple[SqliteStore, httpx.AsyncClient, httpx.AsyncClient, AlertEngine, AlertService]:
    sqlite_store = SqliteStore(tmp_path / "metadata.db")
    sqlite_store.initialize()
    upstream_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"success": True, "token": "jwt-test-token"})
        )
    )
    tg_http = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    auth = TopstepAuthService(settings, upstream_http)
    engine = AlertEngine(scanner_enabled=settings.alert_scanner_enabled)
    tg_client = TelegramClient(settings, tg_http)
    service = AlertService(
        settings,
        engine,
        sqlite_store,
        tg_client,
        auth,
        clock=clock,
    )
    return sqlite_store, upstream_http, tg_http, engine, service


@asynccontextmanager
async def _async_app_client(
    settings: Settings,
    *,
    tg_transport: httpx.AsyncBaseTransport,
    clock: _MutableClock | None = None,
):
    upstream_http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"success": True, "token": "jwt-test-token"})
        )
    )
    tg_http = httpx.AsyncClient(transport=tg_transport)
    app = create_app(
        settings=settings,
        http_client=upstream_http,
        telegram_http_client=tg_http,
        clock=clock,
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
        await tg_http.aclose()
        await upstream_http.aclose()


async def test_f07_sqlite_outbox_deduplication_across_restart(tmp_path: Path) -> None:
    """Contract: UNIQUE constraint deduplicates identical alerts across SQLite restarts.

    Defect caught: Duplicate Telegram alerts fired on restart or version/contract collisions.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(tmp_path)
    sent_requests: list[dict[str, Any]] = []

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/bot{settings.telegram_bot_token}/sendMessage"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["chat_id"] == settings.telegram_chat_id
        assert isinstance(payload["text"], str) and payload["text"]
        assert payload["disable_web_page_preview"] is True
        sent_requests.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    store1, up1, tg1, _, svc1 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        base = _sample_candidate()
        res1 = await svc1.enqueue_candidate(base)
        assert res1.inserted is True
        assert res1.record.status == "PENDING"
        stats = await svc1.process_once()
        assert stats.delivered == 1
        assert len(sent_requests) == 1
    finally:
        store1.close()
        await tg1.aclose()
        await up1.aclose()

    # Simulated process restart: reopen SqliteStore on same DB file
    clock.now += 10
    store2, up2, tg2, _, svc2 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        dup_res = await svc2.enqueue_candidate(_sample_candidate())
        assert dup_res.inserted is False
        assert dup_res.record.id == res1.record.id
        assert dup_res.record.status == "DELIVERED"

        # Changing strategy_version, contract_id, or live_mode must be treated as distinct alerts.
        ver_res = await svc2.enqueue_candidate(_sample_candidate(strategy_version="2.0.0"))
        cid_res = await svc2.enqueue_candidate(_sample_candidate(contract_id=CID_MNQ))
        mode_res = await svc2.enqueue_candidate(_sample_candidate(live_mode=False))
        assert ver_res.inserted is True
        assert cid_res.inserted is True
        assert mode_res.inserted is True

        stats2 = await svc2.process_once()
        assert stats2.delivered == 3
        assert len(sent_requests) == 4
    finally:
        store2.close()
        await tg2.aclose()
        await up2.aclose()


async def test_f07_crash_restart_delivery_recovery_within_ttl(tmp_path: Path) -> None:
    """Contract: PENDING alert persisted before crash is delivered once after restart within TTL.

    Defect caught: Lost alerts when process crashes between enqueue and Telegram send.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(tmp_path, alert_ttl_seconds=90)
    sent_payloads: list[str] = []

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8"))["text"])
        return httpx.Response(200, json={"ok": True})

    store1, up1, tg1, eng1, svc1 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        eng1.register_evaluator("fvg_sweep", lambda _evt: [_sample_candidate()])
        results = await svc1.handle_bar_signal(_closed_bar_event(), tag=None)
        assert len(results) == 1
        assert results[0].inserted is True
        assert results[0].record.status == "PENDING"
        assert len(sent_payloads) == 0
    finally:
        store1.close()
        await tg1.aclose()
        await up1.aclose()

    # Restart 30s later (still well inside 90s TTL)
    clock.now += 30
    store2, up2, tg2, _, svc2 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        stats = await svc2.process_once()
        assert stats.delivered == 1
        assert len(sent_payloads) == 1
        persisted = await store2.get_outbox_alert(results[0].record.id)
        assert persisted is not None
        assert persisted.status == "DELIVERED"
        assert persisted.attempt_count == 1
        assert persisted.delivered_at_utc == clock.now

        # Second pass does not re-send
        stats2 = await svc2.process_once()
        assert stats2.claimed == 0
        assert len(sent_payloads) == 1
    finally:
        store2.close()
        await tg2.aclose()
        await up2.aclose()


async def test_f07_staleness_ttl_expiry_across_restart(tmp_path: Path) -> None:
    """Contract: PENDING alert past ALERT_TTL_SECONDS expires without calling Telegram.

    Defect caught: Stale intraday entry signals delivered late after prolonged outage.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(tmp_path, alert_ttl_seconds=90)
    tg_calls = 0

    async def tg_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal tg_calls
        tg_calls += 1
        return httpx.Response(200, json={"ok": True})

    store1, up1, tg1, _, svc1 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        enq = await svc1.enqueue_candidate(_sample_candidate())
        assert enq.record.expires_at_utc == 1_762_783_260 + 90
    finally:
        store1.close()
        await tg1.aclose()
        await up1.aclose()

    # Advance exactly to expires_at_utc
    clock.now += 90
    store2, up2, tg2, _, svc2 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        stats = await svc2.process_once()
        assert stats.expired >= 1
        assert stats.claimed == 0
        assert tg_calls == 0
        row = await store2.get_outbox_alert(enq.record.id)
        assert row is not None
        assert row.status == "EXPIRED"
    finally:
        store2.close()
        await tg2.aclose()
        await up2.aclose()


async def test_f07_bounded_transient_retry_and_backoff_recovery(tmp_path: Path) -> None:
    """Contract: Transient 503 and 429 retry with backoff/Retry-After and deliver within TTL.

    Defect caught: Dropping alerts on transient errors or ignoring Telegram Retry-After.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(
        tmp_path,
        alert_ttl_seconds=90,
        alert_max_retries=3,
        backoff=[2.0, 5.0, 15.0],
    )
    attempt_idx = 0

    async def tg_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempt_idx
        attempt_idx += 1
        if attempt_idx == 1:
            return httpx.Response(503, json={"ok": False, "description": "Service Unavailable"})
        if attempt_idx == 2:
            return httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}},
            )
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        enq = await svc.enqueue_candidate(_sample_candidate())
        alert_id = enq.record.id

        # Attempt 1 -> 503 -> PENDING with +2s backoff
        s1 = await svc.process_once()
        assert s1.retried == 1
        r1 = await store.get_outbox_alert(alert_id)
        assert r1 is not None
        assert r1.status == "PENDING"
        assert r1.attempt_count == 1
        assert r1.next_attempt_at_utc == clock.now + 2

        # Before next_attempt_at_utc -> not claimed
        clock.now += 1
        s_early = await svc.process_once()
        assert s_early.claimed == 0

        # Attempt 2 -> 429 retry_after=7 (> backoff[1]=5.0) -> PENDING with +7s
        clock.now += 1
        s2 = await svc.process_once()
        assert s2.retried == 1
        r2 = await store.get_outbox_alert(alert_id)
        assert r2 is not None
        assert r2.status == "PENDING"
        assert r2.attempt_count == 2
        assert r2.next_attempt_at_utc == clock.now + 7

        # Attempt 3 -> 200 -> DELIVERED
        clock.now += 7
        s3 = await svc.process_once()
        assert s3.delivered == 1
        r3 = await store.get_outbox_alert(alert_id)
        assert r3 is not None
        assert r3.status == "DELIVERED"
        assert r3.attempt_count == 3
        assert r3.last_error is None
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_exhausted_retries_and_permanent_4xx_transition_to_failed(
    tmp_path: Path,
) -> None:
    """Contract: Exhausted max_attempts or permanent 4xx transitions cleanly to FAILED.

    Defect caught: Infinite retry loops on permanent 400/401/403 or exhausted transient errors.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(
        tmp_path,
        alert_ttl_seconds=300,
        alert_max_retries=2,
        backoff=[1.0, 1.0],
    )
    mode = {"kind": "timeout"}
    calls = 0

    async def tg_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if mode["kind"] == "timeout":
            raise httpx.ReadTimeout("simulated timeout")
        if mode["kind"] == "perm_403":
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot blocked"})
        return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "Bad Chat"})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        # Case A: max_attempts = 1 + 2 = 3 transient timeouts -> FAILED on 3rd attempt
        enq_a = await svc.enqueue_candidate(_sample_candidate(bar_time=1_762_783_200))
        for _ in range(3):
            await svc.process_once()
            clock.now += 2
        row_a = await store.get_outbox_alert(enq_a.record.id)
        assert row_a is not None
        assert row_a.status == "FAILED"
        assert row_a.attempt_count == 3
        assert calls == 3

        await svc.process_once()
        assert calls == 3

        # Case B: permanent HTTP 403 -> FAILED immediately on attempt 1
        mode["kind"] = "perm_403"
        enq_b = await svc.enqueue_candidate(_sample_candidate(bar_time=1_762_783_500))
        stats_b = await svc.process_once()
        assert stats_b.failed == 1
        row_b = await store.get_outbox_alert(enq_b.record.id)
        assert row_b is not None
        assert row_b.status == "FAILED"
        assert row_b.attempt_count == 1
        assert calls == 4

        # Case C: HTTP 200 ok=false error_code=400 -> permanent FAILED on attempt 1
        mode["kind"] = "ok_false_400"
        enq_c = await svc.enqueue_candidate(_sample_candidate(bar_time=1_762_783_800))
        stats_c = await svc.process_once()
        assert stats_c.failed == 1
        row_c = await store.get_outbox_alert(enq_c.record.id)
        assert row_c is not None
        assert row_c.status == "FAILED"
        assert row_c.attempt_count == 1
        assert calls == 5
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_f03_live_feed_signal_suppression_integration(tmp_path: Path) -> None:
    """Contract: ONLY LIVE (tag=None) closed bars enqueue; RECOVERED_HISTORICAL/SUPPRESSED do not.

    Defect caught: False Telegram alerts fired on reconnect backfill or DEGRADED ticks.
    """
    base_t = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    clock = _MutableClock(base_t + 120)
    settings = Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token="123456:tg-secret-token-abcdef",
        telegram_chat_id="-1001234567890",
        upstream_max_retries=0,
        signalr_recovery_timeout_sec=2.0,
    )
    sent_texts: list[str] = []
    rest_state = {"fail_history": True}

    def rest_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-f03-test"})
        if request.url.path == "/api/History/retrieveBars":
            if rest_state["fail_history"]:
                return httpx.Response(500, json={"errorMessage": "Temporary upstream outage"})
            payload = json.loads(request.content.decode("utf-8"))
            raw_start = str(payload["startTime"]).replace("Z", "+00:00")
            raw_end = str(payload["endTime"]).replace("Z", "+00:00")
            req_start = int(datetime.fromisoformat(raw_start).timestamp())
            req_end = int(datetime.fromisoformat(raw_end).timestamp())
            cursor = first_bucket_open_at_or_after(req_start, "1m")
            bars: list[dict[str, Any]] = []
            while cursor + 60 <= req_end:
                if has_open_session_in_range(cursor, cursor + 60):
                    bars.append(
                        {"t": cursor, "o": 5950.0, "h": 5951.0, "l": 5949.5, "c": 5950.5, "v": 10.0}
                    )
                cursor += 60
            return httpx.Response(200, json={"success": True, "bars": bars})
        return httpx.Response(404, json={"errorMessage": "Not found"})

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        return httpx.Response(200, json={"ok": True})

    sqlite_store = SqliteStore(tmp_path / "metadata.db")
    sqlite_store.initialize()
    parquet_store = ParquetStore(tmp_path, sqlite_store=sqlite_store)
    up_http = httpx.AsyncClient(transport=httpx.MockTransport(rest_handler))
    tg_http = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    auth = TopstepAuthService(settings, up_http)
    rest = TopstepRestClient(settings, auth, up_http)
    aggregator = BarAggregator(timeframes=("1m",))
    engine = AlertEngine(scanner_enabled=True)
    engine.register_evaluator(
        "live_breakout",
        lambda evt: [
            AlertCandidate(
                strategy_id="live_breakout",
                strategy_version="1",
                live_mode=evt.live_mode,
                contract_id=evt.contract_id,
                root_symbol=evt.root_symbol,
                timeframe=evt.timeframe,
                bar_time=evt.bar.time,
                direction="LONG",
                alert_kind="ENTRY",
                reason="Live close breakout",
                entry_price=evt.bar.close,
            )
        ],
    )
    tg_client = TelegramClient(settings, tg_http)
    alert_service = AlertService(settings, engine, sqlite_store, tg_client, auth, clock=clock)

    observed_signals: list[tuple[int, str | None]] = []

    async def spy_on_bar_signal(event: AggregatedBarEvent, tag: str | None) -> None:
        observed_signals.append((event.bar.time, tag))
        await alert_service.handle_bar_signal(event, tag)

    transports: list[_MockSignalRTransport] = []

    async def ws_factory(*_args: Any, **_kwargs: Any) -> _MockSignalRTransport:
        tr = _MockSignalRTransport()
        transports.append(tr)
        return tr

    hub = SignalRMarketHub(settings, auth_service=auth, ws_factory=ws_factory)
    live_feed = LiveFeedService(
        settings,
        auth,
        rest,
        parquet_store,
        sqlite_store,
        aggregator=aggregator,
        signalr_hub=hub,
        recovery_timeframes=("1m",),
        initial_backfill_bars=3,
        degraded_retry_interval_sec=60.0,
        on_bar_signal=spy_on_bar_signal,
        clock=clock,
    )

    def _trade_frame(ts: int, price: float, volume: float) -> dict[str, Any]:
        return {
            "type": 1,
            "target": "GatewayTrade",
            "arguments": [
                CID_MES,
                {
                    "symbolId": "F.US.MES",
                    "price": price,
                    "volume": volume,
                    "timestamp": datetime.fromtimestamp(ts, tz=UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            ],
        }

    try:
        await auth.login("user1", "api-key-123456", live=False)
        await live_feed.set_active_contracts([CID_MES], live_mode=False)
        await live_feed.start()

        # 1) Failed REST backfill drives LiveFeedService into DEGRADED state
        for _ in range(100):
            if live_feed.feed_state == "DEGRADED":
                break
            await asyncio.sleep(0.02)
        assert live_feed.feed_state == "DEGRADED"

        tr = transports[-1]
        await tr.handshake_done.wait()

        # Ingest live ticks while DEGRADED -> closed bar must emit tag="SUPPRESSED" & enqueue 0
        m1_open = base_t + 180
        m2_open = base_t + 240
        clock.now = m1_open + 15
        await tr.push_frame(_trade_frame(m1_open + 15, 5951.0, 5))
        await asyncio.sleep(0.03)
        clock.now = m2_open + 10
        await tr.push_frame(_trade_frame(m2_open + 10, 5951.5, 4))

        for _ in range(100):
            if any(tag == "SUPPRESSED" for _, tag in observed_signals):
                break
            await asyncio.sleep(0.02)
        assert (m1_open, "SUPPRESSED") in observed_signals
        assert engine.suppressed_signals >= 1
        assert (await sqlite_store.get_outbox_summary())["total"] == 0

        # 2) Recover to LIVE at m3_open + 2 -> REST confirms prior bars & emits RECOVERED_HISTORICAL
        rest_state["fail_history"] = False
        clock.now = base_t + 302
        await live_feed.trigger_recovery()
        for _ in range(100):
            if live_feed.feed_state == "LIVE":
                break
            await asyncio.sleep(0.02)
        assert live_feed.feed_state == "LIVE"
        assert any(tag == "RECOVERED_HISTORICAL" for _, tag in observed_signals)
        assert engine.recovered_historical_skipped >= 1
        assert (await sqlite_store.get_outbox_summary())["total"] == 0

        # 3) Feed real live ticks while LIVE across complete boundary (m4_open -> m5_open)
        m4_open = base_t + 360
        m5_open = base_t + 420
        clock.now = m4_open + 15
        await tr.push_frame(_trade_frame(m4_open + 15, 5952.0, 6))
        await asyncio.sleep(0.03)
        clock.now = m5_open + 5
        await tr.push_frame(_trade_frame(m5_open + 5, 5952.5, 7))

        for _ in range(100):
            summary = await sqlite_store.get_outbox_summary()
            if summary["total"] >= 1:
                break
            await asyncio.sleep(0.02)

        assert (m4_open, None) in observed_signals
        summary = await sqlite_store.get_outbox_summary()
        assert summary["total"] == 1
        assert summary["pending"] == 1

        stats = await alert_service.process_once()
        assert stats.delivered == 1
        assert len(sent_texts) == 1
        assert "MES" in sent_texts[0]
        assert "UTC+7" in sent_texts[0]

        # Scanner kill-switch suppresses LIVE bars too
        alert_service.set_scanner_enabled(False)
        killed_evt = _closed_bar_event(live_mode=False, timeframe="1m", bar_time=m5_open)
        assert await alert_service.handle_bar_signal(killed_evt, tag=None) == []
        assert (await sqlite_store.get_outbox_summary())["total"] == 1
    finally:
        await live_feed.stop()
        await alert_service.stop()
        sqlite_store.close()
        await tg_http.aclose()
        await up_http.aclose()


async def test_f07_zero_secret_redaction_and_alert_api_routes(tmp_path: Path) -> None:
    """Contract: Bot token & chat_id never leak in errors, logs, or /api/alerts/* responses.

    Defect caught: Leaking TELEGRAM_BOT_TOKEN in /bot<token>/sendMessage URLs or chat_id in JSON.
    """
    short_token = "99:ab"
    short_chat = "1234"
    clock = _MutableClock(1_762_783_260)

    # First verify unconfigured 503 on POST /api/alerts/test
    unconfigured = _make_settings(tmp_path / "unconf", bot_token="", chat_id="")
    async with _async_app_client(
        unconfigured,
        tg_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True})),
        clock=clock,
    ) as (client, _):
        login_resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        csrf = login_resp.json()["csrf_token"]
        test_503 = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
        assert test_503.status_code == 503

    # Now configure short secrets and simulate TransportError leaking full URL + chat_id
    settings = _make_settings(tmp_path / "conf", bot_token=short_token, chat_id=short_chat)
    fail_mode = {"fail": True}

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        if fail_mode["fail"]:
            raise httpx.ConnectError(
                f"Failed POST {request.url} for chat_id={short_chat} token={short_token}",
                request=request,
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    tg_logger = logging.getLogger("topstep_chart")
    prev_lvl = tg_logger.level
    tg_logger.setLevel(logging.DEBUG)
    tg_logger.addHandler(handler)
    try:
        async with _async_app_client(
            settings,
            tg_transport=httpx.MockTransport(tg_handler),
            clock=clock,
        ) as (client, app):
            login_resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
            csrf = login_resp.json()["csrf_token"]

            # Trigger failing test alert -> transient error stored redacted in outbox & last_error
            test_fail_resp = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
            assert test_fail_resp.status_code == 200
            body_fail = test_fail_resp.json()
            assert body_fail["ok"] is False
            assert body_fail["alert"]["status"] == "PENDING"

            # Verify /api/status, /api/alerts/status, /api/alerts/history redact all secrets
            status_resp = await client.get("/api/status")
            alerts_status = await client.get("/api/alerts/status")
            alerts_history = await client.get("/api/alerts/history?status=PENDING&limit=10")
            assert status_resp.status_code == 200
            assert alerts_status.status_code == 200
            assert alerts_history.status_code == 200
            assert status_resp.json()["alerts"]["telegram_configured"] is True

            # Toggle scanner via POST /api/alerts/scanner
            toggle_resp = await client.post(
                "/api/alerts/scanner",
                json={"enabled": False},
                headers={"X-CSRF-Token": csrf},
            )
            assert toggle_resp.status_code == 200
            assert toggle_resp.json()["scanner_enabled"] is False

            # Now succeed on a fresh test alert
            fail_mode["fail"] = False
            test_ok_resp = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
            assert test_ok_resp.status_code == 200
            assert test_ok_resp.json()["ok"] is True
            assert test_ok_resp.json()["alert"]["status"] == "DELIVERED"

            delivered_hist = await client.get("/api/alerts/history?status=DELIVERED&limit=5")
            assert delivered_hist.status_code == 200
            assert delivered_hist.json()["count"] >= 1

            combined_text = "\n".join(
                [
                    test_fail_resp.text,
                    status_resp.text,
                    alerts_status.text,
                    alerts_history.text,
                    test_ok_resp.text,
                    delivered_hist.text,
                    app.state.alert_service.last_error or "",
                ]
            )
            assert short_token not in combined_text
            assert short_chat not in combined_text
            assert "/bot***REDACTED***" in combined_text
    finally:
        tg_logger.removeHandler(handler)
        tg_logger.setLevel(prev_lvl)

    log_text = log_buf.getvalue()
    assert short_token not in log_text
    assert short_chat not in log_text


async def test_f07_weekend_key_rotation_reminder_and_weekday_suppression(tmp_path: Path) -> None:
    """Contract: Key rotation reminder fires once per CME weekend and never on weekdays.

    Defect caught: Reminders firing during active Globex hours or repeating on restart.
    """
    # 2026-11-11 is Wednesday, 2026-11-13 is Friday, 2026-11-14 is Saturday, 2026-11-15 is Sunday
    wed_open = int(datetime(2026, 11, 11, 10, 0, tzinfo=NY_TZ).timestamp())
    fri_before_close = int(datetime(2026, 11, 13, 16, 59, tzinfo=NY_TZ).timestamp())
    fri_closed = int(datetime(2026, 11, 13, 17, 0, tzinfo=NY_TZ).timestamp())
    sat_noon = int(datetime(2026, 11, 14, 12, 0, tzinfo=NY_TZ).timestamp())
    sun_before_open = int(datetime(2026, 11, 15, 17, 59, tzinfo=NY_TZ).timestamp())
    sun_open = int(datetime(2026, 11, 15, 18, 0, tzinfo=NY_TZ).timestamp())

    assert is_cme_weekend_window(wed_open) is False
    assert is_cme_weekend_window(fri_before_close) is False
    assert is_cme_weekend_window(fri_closed) is True
    assert is_cme_weekend_window(sat_noon) is True
    assert is_cme_weekend_window(sun_before_open) is True
    assert is_cme_weekend_window(sun_open) is False

    bounds = cme_weekend_window_bounds(sat_noon)
    assert bounds == (fri_closed, sun_open)

    clock = _MutableClock(wed_open)
    settings = _make_settings(
        tmp_path,
        key_updated_at="2026-10-01",
        key_rotation_days=30,
        key_rotation_enforce=False,
    )
    sent_texts: list[str] = []

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        return httpx.Response(200, json={"ok": True})

    store1, up1, tg1, _, svc1 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        # Weekday -> no reminder
        assert await svc1.check_key_rotation_reminder() is None
        clock.now = fri_before_close
        assert await svc1.check_key_rotation_reminder() is None

        # Saturday -> enqueues 1 reminder expiring at Sunday 18:00 ET
        clock.now = sat_noon
        res1 = await svc1.check_key_rotation_reminder()
        assert res1 is not None
        assert res1.inserted is True
        assert res1.record.alert_kind == "KEY_ROTATION"
        assert res1.record.expires_at_utc == sun_open
        assert "topstepx.com/settings?tab=api" in res1.record.payload_text

        stats1 = await svc1.process_once()
        assert stats1.delivered == 1
        assert len(sent_texts) == 1
    finally:
        store1.close()
        await tg1.aclose()
        await up1.aclose()

    # Simulated restart on Sunday afternoon -> deduplicates same weekend
    clock.now = sun_before_open
    store2, up2, tg2, _, svc2 = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        res2 = await svc2.check_key_rotation_reminder()
        assert res2 is not None
        assert res2.inserted is False
        assert res2.record.status == "DELIVERED"
        stats2 = await svc2.process_once()
        assert stats2.claimed == 0
        assert len(sent_texts) == 1

        # Sunday 18:00 ET market reopen -> outside weekend window
        clock.now = sun_open
        assert await svc2.check_key_rotation_reminder() is None
    finally:
        store2.close()
        await tg2.aclose()
        await up2.aclose()


async def test_f07_terminal_state_invariants_and_pure_alert_engine(tmp_path: Path) -> None:
    """Contract: Terminal rows are immutable; AlertEngine formats UTC+7 and validates config.

    Defect caught: Terminal DELIVERED/EXPIRED/FAILED rows reverting to PENDING or bad URL config.
    """
    store = SqliteStore(tmp_path / "metadata.db")
    store.initialize()
    try:
        now = 1_762_783_260
        for idx, terminal in enumerate(("DELIVERED", "EXPIRED", "FAILED")):
            enq = await store.enqueue_alert_outbox(
                strategy_id="term_check",
                strategy_version="1",
                live_mode=True,
                contract_id=CID_MES,
                timeframe="5m",
                bar_time=now + idx * 300,
                direction="LONG",
                payload_text="msg",
                created_at_utc=now,
                expires_at_utc=now + 90,
                max_attempts=3,
            )
            aid = enq.record.id
            if terminal == "DELIVERED":
                assert await store.mark_outbox_delivered(aid, now + 1) is True
            elif terminal == "EXPIRED":
                assert await store.mark_outbox_expired(aid, now + 1, "expired") is True
            else:
                assert await store.mark_outbox_failed(aid, now + 1, "perm") is True

            assert await store.mark_outbox_delivered(aid, now + 5) is False
            assert await store.mark_outbox_expired(aid, now + 5, "again") is False
            assert await store.mark_outbox_failed(aid, now + 5, "again") is False
            assert await store.mark_outbox_retry(aid, now + 5, now + 10, "retry") is None

            row = await store.get_outbox_alert(aid)
            assert row is not None
            assert row.status == terminal
    finally:
        store.close()

    # Pure Vietnam UTC+7 message formatting check (independent UTC+7 wall-clock strings)
    candidate = _sample_candidate()
    created_utc = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())
    formatted = format_entry_alert_message(candidate, created_utc)
    assert "Bar: 2025-11-10 21:00:00 ICT (UTC+7)" in formatted
    assert "Time: 2026-11-10 21:00:00 ICT (UTC+7)" in formatted
    assert "Bullish FVG + Liquidity Sweep" in formatted
    assert "Entry: 5950.25" in formatted
    assert "SL: 5945" in formatted
    assert "TP: 5960.75" in formatted
    assert "R:R: 2" in formatted

    # Provisional open bars are ignored by default unless allow_provisional=True
    engine = AlertEngine()
    engine.register_evaluator("default_closed_only", lambda _e: [_sample_candidate()])
    open_evt = _closed_bar_event(is_closed=False)
    assert engine.evaluate_bar_signal(open_evt, tag=None) == ()
    assert engine.unregister_evaluator("default_closed_only") is True

    # Config validation checks
    with pytest.raises(ValidationError):
        Settings(telegram_api_base_url="http://api.telegram.org")
    with pytest.raises(ValidationError):
        Settings(telegram_api_base_url="https://user:pass@api.telegram.org")
    with pytest.raises(ValidationError):
        Settings(telegram_api_base_url="https://api.telegram.org?foo=bar")
    with pytest.raises(ValidationError):
        Settings(alert_retry_backoff_sec=[0.0, 2.0])
    with pytest.raises(ValidationError):
        Settings(alert_retry_backoff_sec="2, -1")

    loopback_cfg = Settings(
        telegram_api_base_url="http://127.0.0.1:9090/",
        alert_retry_backoff_sec="1.5, 3, 6.5",
    )
    assert loopback_cfg.telegram_api_base_url == "http://127.0.0.1:9090"
    assert loopback_cfg.alert_retry_backoff_sec == [1.5, 3.0, 6.5]

    # Unconfigured TelegramClient check + TelegramNotConfiguredError export check
    assert issubclass(TelegramNotConfiguredError, RuntimeError)


async def test_f07_lifespan_creates_and_closes_dedicated_telegram_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Lifespan creates and closes a dedicated Telegram AsyncClient when configured.

    Defect caught: Leaked Telegram httpx connection pool or missing client initialization.
    """
    created_clients: list[httpx.AsyncClient] = []
    captured_timeouts: list[object] = []
    orig_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured_timeouts.append(kwargs.get("timeout"))
        client = orig_async_client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr("app.main.httpx.AsyncClient", fake_async_client)

    settings = _make_settings(tmp_path)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        assert len(created_clients) == 2
        tg_timeout = captured_timeouts[1]
        assert isinstance(tg_timeout, httpx.Timeout)
        assert tg_timeout.connect == 5.0
        assert tg_timeout.read == 10.0
        assert tg_timeout.write == 10.0
        assert tg_timeout.pool == 5.0
        assert created_clients[0].is_closed is False
        assert created_clients[1].is_closed is False
        assert app.state.telegram_http_client is created_clients[1]
    assert created_clients[0].is_closed is True
    assert created_clients[1].is_closed is True


async def test_f07_redacts_chat_id_straddling_truncation_boundary_in_502_body(
    tmp_path: Path,
) -> None:
    """Contract: Non-JSON proxy error bodies are redacted before 512-char truncation.

    Defect caught: Truncating before redaction leaves a raw prefix of telegram_chat_id.
    """
    chat_id = "-1009876543210"
    settings = _make_settings(tmp_path, chat_id=chat_id)
    filler = "X" * 508
    body_text = f"{filler}{chat_id}-tail"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(502, text=body_text))
    ) as tg_http:
        client = TelegramClient(settings, tg_http)
        result = await client.send_message("hello")

    assert result.ok is False
    assert result.permanent_failure is False
    assert result.status_code == 502
    assert result.error is not None
    assert len(result.error) == 512
    assert result.error == f"{filler}***R"
    # Zero prefix (first 4+ chars or full chat_id) may leak across the 512-byte boundary
    for prefix_len in range(4, len(chat_id) + 1):
        assert chat_id[:prefix_len] not in result.error


async def test_f07_send_test_alert_bypasses_backlog_and_paused_dispatcher(
    tmp_path: Path,
) -> None:
    """Contract: send_test_alert dispatches only TEST even with backlog & paused worker.

    Defect caught: Test button starving behind backlog or flushing paused strategy alerts.
    """
    clock = _MutableClock(1_762_783_260)
    settings = Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token="123456:tg-secret-token-abcdef",
        telegram_chat_id="-1001234567890",
        alert_dispatcher_enabled=False,
    )
    sent_texts: list[str] = []

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        backlog_ids: list[int] = []
        for idx in range(20):
            enq = await svc.enqueue_candidate(
                _sample_candidate(bar_time=1_762_783_200 + idx * 300)
            )
            backlog_ids.append(enq.record.id)

        test_record = await svc.send_test_alert()
        assert test_record.status == "DELIVERED"
        assert test_record.alert_kind == "TEST"
        assert len(sent_texts) == 1
        assert "Telegram test alert OK" in sent_texts[0]

        for bid in backlog_ids:
            row = await store.get_outbox_alert(bid)
            assert row is not None
            assert row.status == "PENDING"
            assert row.attempt_count == 0
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_direct_enqueue_candidate_infers_root_symbol_and_validates(
    tmp_path: Path,
) -> None:
    """Contract: Direct enqueue_candidate normalizes/validates inputs and infers root_symbol.

    Defect caught: Direct callers bypassing AlertEngine validation or losing root_symbol label.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(tmp_path)
    store, up, tg, _, svc = _build_service_stack(
        tmp_path,
        settings=settings,
        tg_handler=lambda _r: httpx.Response(200, json={"ok": True}),
        clock=clock,
    )
    try:
        direct = AlertCandidate(
            strategy_id="direct_caller",
            strategy_version="1",
            live_mode=True,
            contract_id="con.f.us.mes.z26",
            root_symbol=None,
            timeframe="5M",
            bar_time=1_762_783_200,
            direction="LONG",
            reason="Direct test",
        )
        enq = await svc.enqueue_candidate(direct)
        assert enq.inserted is True
        assert enq.record.contract_id == CID_MES
        assert enq.record.timeframe == "5m"
        assert "MES (CON.F.US.MES.Z26)" in enq.record.payload_text

        bad_tf = AlertCandidate(
            strategy_id="direct_caller",
            strategy_version="1",
            live_mode=True,
            contract_id=CID_MES,
            root_symbol=None,
            timeframe="99m",
            bar_time=1_762_783_200,
            direction="LONG",
        )
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            await svc.enqueue_candidate(bad_tf)

        invalid_dir: Any = "SIDEWAYS"
        bad_dir = AlertCandidate(
            strategy_id="direct_caller",
            strategy_version="1",
            live_mode=True,
            contract_id=CID_MES,
            root_symbol=None,
            timeframe="5m",
            bar_time=1_762_783_200,
            direction=invalid_dir,
        )
        with pytest.raises(ValueError, match="Unsupported AlertDirection"):
            await svc.enqueue_candidate(bad_dir)
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


def test_f07_env_alert_retry_backoff_sec_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: ALERT_RETRY_BACKOFF_SEC parses comma-separated and JSON env strings cleanly.

    Defect caught: pydantic-settings EnvSettingsSource crashing with JSONDecodeError on '2,5,15'.
    """
    monkeypatch.setenv("ALERT_RETRY_BACKOFF_SEC", "2,5,15")
    csv_settings = Settings()
    assert csv_settings.alert_retry_backoff_sec == [2.0, 5.0, 15.0]

    monkeypatch.setenv("ALERT_RETRY_BACKOFF_SEC", "[1, 3, 9]")
    json_settings = Settings()
    assert json_settings.alert_retry_backoff_sec == [1.0, 3.0, 9.0]

    monkeypatch.delenv("ALERT_RETRY_BACKOFF_SEC", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ALERT_RETRY_BACKOFF_SEC=2,5,15\n", encoding="utf-8")
    loaded = load_settings(env_file=env_file)
    assert loaded.alert_retry_backoff_sec == [2.0, 5.0, 15.0]


async def test_f07_redacts_full_error_before_truncation_with_4080_token_body(
    tmp_path: Path,
) -> None:
    """Contract: Full response body is redacted BEFORE slicing to _MAX_ERROR_CHARS (512).

    Defect caught: Pre-redaction 4096-byte slice cutting chat_id into '-1009' inside final error.
    """
    chat_id = "-1009876543210"
    settings = _make_settings(tmp_path, chat_id=chat_id)
    long_body = "token=" + ("X" * 4080) + " end " + chat_id

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    tg_logger = logging.getLogger("topstep_chart.notify.telegram")
    prev_lvl = tg_logger.level
    tg_logger.setLevel(logging.DEBUG)
    tg_logger.addHandler(handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(502, text=long_body))
        ) as tg_http:
            client = TelegramClient(settings, tg_http)
            result = await client.send_message("hello")
    finally:
        tg_logger.removeHandler(handler)
        tg_logger.setLevel(prev_lvl)

    assert result.ok is False
    assert result.status_code == 502
    assert result.error is not None
    assert len(result.error) <= 512
    assert chat_id not in result.error
    assert "-1009" not in result.error
    emitted_logs = log_buf.getvalue()
    assert chat_id not in emitted_logs
    assert "-1009" not in emitted_logs


async def test_f07_telegram_semaphore_concurrency_and_cancellation(tmp_path: Path) -> None:
    """Contract: TelegramClient enforces max_concurrency semaphore and propagates CancelledError.

    Defect caught: Unbounded concurrent Telegram POSTs or swallowed task cancellation.
    """
    settings = Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token="123456:tg-secret-token-abcdef",
        telegram_chat_id="-1001234567890",
        telegram_max_concurrency=2,
    )
    in_flight = 0
    max_observed = 0
    started_two = asyncio.Event()
    release_send = asyncio.Event()

    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_observed
        in_flight += 1
        max_observed = max(max_observed, in_flight)
        if max_observed >= 2:
            started_two.set()
        try:
            await release_send.wait()
            return httpx.Response(200, json={"ok": True})
        finally:
            in_flight -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as tg_http:
        client = TelegramClient(settings, tg_http)
        tasks = [asyncio.create_task(client.send_message(f"msg-{i}")) for i in range(5)]
        await asyncio.wait_for(started_two.wait(), timeout=2.0)
        await asyncio.sleep(0.02)
        assert max_observed == 2
        release_send.set()
        results = await asyncio.gather(*tasks)
        assert all(res.ok is True for res in results)

        # Cancellation propagation while blocked inside send_message
        release_send.clear()
        started_cancel_target = asyncio.Event()

        async def blocking_handler(_request: httpx.Request) -> httpx.Response:
            started_cancel_target.set()
            await asyncio.Future()
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(blocking_handler)
        ) as blocking_http:
            cancel_client = TelegramClient(settings, blocking_http)
            cancel_task = asyncio.create_task(cancel_client.send_message("cancel-me"))
            await asyncio.wait_for(started_cancel_target.wait(), timeout=2.0)
            cancel_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancel_task


async def test_f07_huge_backoff_clamped_to_expires_at_enforces_max_attempts(
    tmp_path: Path,
) -> None:
    """Contract: Huge alert_retry_backoff_sec clamps to expires_at_utc without int64 overflow.

    Defect caught: done_now + 1e20 overflowing SQLite int64 and leaving PENDING at attempt 0.
    """
    clock = _MutableClock(1_762_783_260)
    settings = _make_settings(
        tmp_path,
        alert_ttl_seconds=90,
        alert_max_retries=0,
        backoff=[1e20],
    )
    tg_calls = 0

    async def tg_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal tg_calls
        tg_calls += 1
        return httpx.Response(503, json={"ok": False, "description": "Unavailable"})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        enq = await svc.enqueue_candidate(_sample_candidate())
        stats1 = await svc.process_once()
        assert stats1.failed == 1
        row = await store.get_outbox_alert(enq.record.id)
        assert row is not None
        assert row.status == "FAILED"
        assert row.attempt_count == 1
        assert tg_calls == 1

        stats2 = await svc.process_once()
        assert stats2.claimed == 0
        assert tg_calls == 1
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_key_rotation_reminder_pins_created_at_across_boundary_tick(
    tmp_path: Path,
) -> None:
    """Contract: Weekend key rotation reminder pins created_at_utc so expires_at_utc == sun_utc.

    Defect caught: Second clock read ticking past sun_utc-1 allowing delivery at market open.
    """
    sun_open = int(datetime(2026, 11, 15, 18, 0, tzinfo=NY_TZ).timestamp())
    tick_values = iter([sun_open - 1, sun_open, sun_open])
    last_val = sun_open

    def stepping_clock() -> int:
        nonlocal last_val
        try:
            last_val = next(tick_values)
        except StopIteration:
            pass
        return last_val

    settings = _make_settings(
        tmp_path,
        key_updated_at="2026-10-01",
        key_rotation_days=30,
    )
    tg_calls = 0

    async def tg_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal tg_calls
        tg_calls += 1
        return httpx.Response(200, json={"ok": True})

    sqlite_store = SqliteStore(tmp_path / "metadata.db")
    sqlite_store.initialize()
    up_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
    )
    tg_http = httpx.AsyncClient(transport=httpx.MockTransport(tg_handler))
    auth = TopstepAuthService(settings, up_http)
    engine = AlertEngine()
    svc = AlertService(
        settings,
        engine,
        sqlite_store,
        TelegramClient(settings, tg_http),
        auth,
        clock=stepping_clock,
    )
    try:
        res = await svc.check_key_rotation_reminder()
        assert res is not None
        assert res.inserted is True
        assert res.record.created_at_utc == sun_open - 1
        assert res.record.expires_at_utc == sun_open

        stats = await svc.process_once()
        assert stats.expired >= 1
        assert tg_calls == 0
        persisted = await sqlite_store.get_outbox_alert(res.record.id)
        assert persisted is not None
        assert persisted.status == "EXPIRED"
    finally:
        sqlite_store.close()
        await tg_http.aclose()
        await up_http.aclose()


async def test_f07_mid_batch_ttl_expiry_skips_second_send(tmp_path: Path) -> None:
    """Contract: Pre-send TTL check expires later alerts in a batch if clock advances mid-batch.

    Defect caught: Sending a stale alert when earlier sends in the same batch exhausted its TTL.
    """
    t0 = 1_762_783_260
    clock = _MutableClock(t0)
    settings = _make_settings(tmp_path, alert_ttl_seconds=30)
    sent_texts: list[str] = []

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        clock.now = t0 + 35
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        enq_a = await svc.enqueue_candidate(_sample_candidate(bar_time=t0 - 300, direction="LONG"))
        enq_b = await svc.enqueue_candidate(_sample_candidate(bar_time=t0 - 600, direction="SHORT"))

        stats = await svc.process_once()
        assert stats.claimed == 2
        assert stats.delivered == 1
        assert stats.expired == 1
        assert len(sent_texts) == 1

        row_a = await store.get_outbox_alert(enq_a.record.id)
        row_b = await store.get_outbox_alert(enq_b.record.id)
        assert row_a is not None and row_a.status == "DELIVERED"
        assert row_b is not None and row_b.status == "EXPIRED"
    finally:
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_hanging_transport_triggers_total_timeout_deadline(tmp_path: Path) -> None:
    """Contract: TelegramClient enforces telegram_total_timeout_sec against a hanging transport.

    Defect caught: Hung TCP/TLS read starving the outbox worker indefinitely.
    """
    settings = Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token="123456:tg-secret-token-abcdef",
        telegram_chat_id="-1001234567890",
        telegram_total_timeout_sec=0.05,
    )
    never = asyncio.Event()

    async def hanging_handler(_request: httpx.Request) -> httpx.Response:
        await never.wait()
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging_handler)) as tg_http:
        client = TelegramClient(settings, tg_http)
        result = await asyncio.wait_for(client.send_message("deadline-test"), timeout=0.5)

    assert result.ok is False
    assert result.permanent_failure is False
    assert result.status_code is None
    assert result.error is not None
    assert "timed out" in result.error.lower()


async def test_f07_httpx_logger_redacts_bot_token_after_create_app(tmp_path: Path) -> None:
    """Contract: Global log redaction installed by create_app masks /bot<token> on httpx logger.

    Defect caught: httpx INFO request logs leaking TELEGRAM_BOT_TOKEN in URLs.
    """
    bot_token = "555666777:AAH_httpx_secret_token_xyz"
    settings = _make_settings(tmp_path, bot_token=bot_token)
    create_app(settings=settings)

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    httpx_logger = logging.getLogger("httpx")
    prev_lvl = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.addHandler(handler)
    try:
        httpx_logger.info(
            "HTTP Request: POST https://api.telegram.org/bot%s/sendMessage",
            bot_token,
        )
    finally:
        httpx_logger.removeHandler(handler)
        httpx_logger.setLevel(prev_lvl)

    emitted = log_buf.getvalue()
    assert bot_token not in emitted
    assert "/bot***REDACTED***/sendMessage" in emitted


async def test_f07_handle_bar_signal_wakes_worker_per_insert_and_isolates_bad_candidate(
    tmp_path: Path,
) -> None:
    """Contract: Valid inserts wake sleeping worker immediately even if a sibling candidate fails.

    Defect caught: A failing candidate skipping _wakeup.set() for earlier inserted rows.
    """
    clock = _MutableClock(1_762_783_260)
    settings = Settings(
        data_dir=tmp_path,
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token="123456:tg-secret-token-abcdef",
        telegram_chat_id="-1001234567890",
        alert_ttl_seconds=5,
        alert_dispatch_poll_interval_sec=60.0,
    )
    sent_texts: list[str] = []
    two_delivered = asyncio.Event()

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        if len(sent_texts) >= 2:
            two_delivered.set()
        return httpx.Response(200, json={"ok": True})

    store, up, tg, eng, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    valid_1 = _sample_candidate(strategy_id="strat_1", direction="LONG")
    bad_whitespace = AlertCandidate(
        strategy_id="strat_bad",
        strategy_version="1",
        live_mode=True,
        contract_id=CID_MES,
        root_symbol="MES",
        timeframe="5m",
        bar_time=1_762_783_200,
        direction="INFO",
        alert_kind="ENTRY",
        message_text="   ",
    )
    valid_2 = _sample_candidate(strategy_id="strat_2", direction="SHORT")
    eng.register_evaluator("mixed", lambda _evt: [valid_1, bad_whitespace, valid_2])

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    svc_logger = logging.getLogger("topstep_chart.alert_service")
    prev_lvl = svc_logger.level
    svc_logger.setLevel(logging.DEBUG)
    svc_logger.addHandler(handler)
    try:
        # First verify synchronous wakeup + last_error recording when worker is stopped
        eng.unregister_evaluator("mixed")
        eng.register_evaluator("pre_check", lambda _evt: [valid_1, bad_whitespace])
        pre_res = await svc.handle_bar_signal(
            _closed_bar_event(bar_time=1_762_783_200),
            tag=None,
        )
        assert len(pre_res) == 1 and pre_res[0].inserted is True
        assert svc._wakeup.is_set() is True
        assert svc.last_error is not None
        assert "payload_text" in svc.last_error
        await svc.process_once()
        assert len(sent_texts) == 1
        sent_texts.clear()
        two_delivered.clear()

        # Now start background worker with 60s poll interval and let it sleep on _wakeup.wait()
        eng.unregister_evaluator("pre_check")
        valid_3 = _sample_candidate(strategy_id="strat_3", bar_time=1_762_783_500, direction="LONG")
        valid_4 = _sample_candidate(
            strategy_id="strat_4", bar_time=1_762_783_500, direction="SHORT"
        )
        eng.register_evaluator("mixed", lambda _evt: [valid_3, bad_whitespace, valid_4])

        await svc.start()
        await asyncio.sleep(0.05)

        results = await svc.handle_bar_signal(
            _closed_bar_event(bar_time=1_762_783_500),
            tag=None,
        )
        assert len(results) == 2
        assert all(r.inserted for r in results)

        # Awakened worker must deliver both valid alerts promptly (< 1.0s, not 60s)
        await asyncio.wait_for(two_delivered.wait(), timeout=1.0)
        for _ in range(50):
            row_3 = await store.get_outbox_alert(results[0].record.id)
            row_4 = await store.get_outbox_alert(results[1].record.id)
            if (
                row_3 is not None
                and row_4 is not None
                and row_3.status == "DELIVERED"
                and row_4.status == "DELIVERED"
            ):
                break
            await asyncio.sleep(0.02)

        assert row_3 is not None and row_3.status == "DELIVERED"
        assert row_4 is not None and row_4.status == "DELIVERED"
        assert "payload_text must be a non-empty string" in log_buf.getvalue()
    finally:
        svc_logger.removeHandler(handler)
        svc_logger.setLevel(prev_lvl)
        await svc.stop()
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_send_test_alert_cuts_in_and_uses_post_lock_clock(tmp_path: Path) -> None:
    """Contract: send_test_alert cuts in after current single send and reads clock inside lock.

    Defect caught: Stale pre-lock clock expiring TEST alert and batch lock stalling behind 8 rows.
    """
    t0 = 1_762_783_260
    clock = _MutableClock(t0)
    settings = _make_settings(tmp_path, alert_ttl_seconds=90)
    send_order: list[str] = []
    first_in_flight = asyncio.Event()
    release_first = asyncio.Event()

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content.decode("utf-8"))["text"]
        if "Telegram test alert OK" in text:
            send_order.append("TEST")
        else:
            send_order.append("BATCH")
            if len(send_order) == 1:
                first_in_flight.set()
                await release_first.wait()
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        batch_ids: list[int] = []
        for idx in range(8):
            enq = await svc.enqueue_candidate(
                _sample_candidate(bar_time=t0 + idx * 300)
            )
            batch_ids.append(enq.record.id)

        batch_task = asyncio.create_task(svc.process_once())
        await asyncio.wait_for(first_in_flight.wait(), timeout=2.0)

        # Start send_test_alert() while batch alert #1 is in flight, then advance clock past 90s
        test_task = asyncio.create_task(svc.send_test_alert())
        await asyncio.sleep(0.02)
        clock.now = t0 + 98
        release_first.set()

        test_record = await asyncio.wait_for(test_task, timeout=2.0)
        stats = await asyncio.wait_for(batch_task, timeout=2.0)

        assert test_record.status == "DELIVERED"
        assert test_record.attempt_count == 1
        assert test_record.created_at_utc == t0 + 98
        assert send_order[:2] == ["BATCH", "TEST"]
        assert stats.delivered == 1
        assert stats.expired == 7

        row0 = await store.get_outbox_alert(batch_ids[0])
        assert row0 is not None and row0.status == "DELIVERED"
        for bid in batch_ids[1:]:
            row = await store.get_outbox_alert(bid)
            assert row is not None and row.status == "EXPIRED"
    finally:
        release_first.set()
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_concurrent_process_once_claims_no_duplicates(tmp_path: Path) -> None:
    """Contract: Concurrent process_once() calls never double-claim or double-send any row.

    Defect caught: Releasing batch lock between rows causing concurrent claimer to re-send.
    """
    t0 = 1_762_783_260
    clock = _MutableClock(t0)
    settings = _make_settings(tmp_path, alert_ttl_seconds=300)
    sent_texts: list[str] = []
    first_in_flight = asyncio.Event()
    release_first = asyncio.Event()

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content.decode("utf-8"))["text"]
        sent_texts.append(text)
        if len(sent_texts) == 1:
            first_in_flight.set()
            await release_first.wait()
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        batch_ids: list[int] = []
        for idx in range(8):
            enq = await svc.enqueue_candidate(
                _sample_candidate(strategy_id=f"s{idx}", bar_time=t0 + idx * 60)
            )
            batch_ids.append(enq.record.id)

        t1 = asyncio.create_task(svc.process_once(limit=20))
        await asyncio.wait_for(first_in_flight.wait(), timeout=2.0)

        t2 = asyncio.create_task(svc.process_once(limit=20))
        await asyncio.sleep(0.02)
        release_first.set()

        s1, s2 = await asyncio.gather(t1, t2)
        assert s1.delivered + s2.delivered == 8
        assert len(sent_texts) == 8
        assert len(set(sent_texts)) == 8
        assert svc._in_flight_owners == {}

        for bid in batch_ids:
            row = await store.get_outbox_alert(bid)
            assert row is not None
            assert row.status == "DELIVERED"
            assert row.attempt_count == 1
    finally:
        release_first.set()
        store.close()
        await tg.aclose()
        await up.aclose()


async def test_f07_three_way_aba_in_flight_ownership_prevents_double_delivery(
    tmp_path: Path,
) -> None:
    """Contract: Batch-token ownership prevents 3-way overlapping ABA re-claim/double-send.

    Defect caught: Batch 1 finally wiping A after Batch 2 re-claimed A, letting Batch 3 re-send.
    """
    t0 = 1_762_783_260
    clock = _MutableClock(t0)
    settings = _make_settings(
        tmp_path,
        alert_ttl_seconds=300,
        alert_max_retries=2,
        backoff=[2.0],
    )

    call_log: list[tuple[str, int]] = []
    b_paused = asyncio.Event()
    release_b = asyncio.Event()
    x_paused = asyncio.Event()
    release_x = asyncio.Event()
    a_count = 0

    async def tg_handler(request: httpx.Request) -> httpx.Response:
        nonlocal a_count
        text = json.loads(request.content.decode("utf-8"))["text"]
        if "strat_A" in text:
            a_count += 1
            if a_count == 1:
                call_log.append(("A", 503))
                return httpx.Response(503, json={"ok": False, "description": "Retry"})
            call_log.append(("A", 200))
            return httpx.Response(200, json={"ok": True})
        if "strat_B" in text:
            call_log.append(("B", 200))
            b_paused.set()
            await release_b.wait()
            return httpx.Response(200, json={"ok": True})
        if "strat_C" in text:
            call_log.append(("C", 200))
            return httpx.Response(200, json={"ok": True})
        call_log.append(("X", 200))
        x_paused.set()
        await release_x.wait()
        return httpx.Response(200, json={"ok": True})

    store, up, tg, _, svc = _build_service_stack(
        tmp_path, settings=settings, tg_handler=tg_handler, clock=clock
    )
    try:
        enq_a = await svc.enqueue_candidate(_sample_candidate(strategy_id="strat_A", bar_time=t0))
        enq_b = await svc.enqueue_candidate(
            _sample_candidate(strategy_id="strat_B", bar_time=t0 + 60)
        )
        enq_c = await svc.enqueue_candidate(
            _sample_candidate(strategy_id="strat_C", bar_time=t0 + 120)
        )

        # Batch 1 claims [A, B, C], gets 503 on A (schedules retry at t0 + 2), pauses on B
        batch1 = asyncio.create_task(svc.process_once(limit=20))
        await asyncio.wait_for(b_paused.wait(), timeout=2.0)

        # Enqueue X at t0 + 1 (due before A's t0 + 2), then advance clock to t0 + 2
        clock.now = t0 + 1
        enq_x = await svc.enqueue_candidate(
            _sample_candidate(strategy_id="strat_X", bar_time=t0 + 180)
        )
        clock.now = t0 + 2

        # Queue Batch 2 behind B, then release B -> Batch 2 claims [X, A],
        # Batch 1 finishes C and runs its finally: while Batch 2 still owns A!
        batch2 = asyncio.create_task(svc.process_once(limit=20))
        await asyncio.sleep(0.02)
        release_b.set()

        await asyncio.wait_for(x_paused.wait(), timeout=2.0)
        await asyncio.wait_for(batch1, timeout=2.0)

        # Queue Batch 3 while Batch 2 is paused on X (and still holds A in _in_flight_owners)
        batch3 = asyncio.create_task(svc.process_once(limit=20))
        await asyncio.sleep(0.02)
        release_x.set()

        s1, s2, s3 = await asyncio.gather(batch1, batch2, batch3)
        assert s1.retried == 1 and s1.delivered == 2
        assert s2.delivered == 2
        assert s3.claimed == 0
        assert call_log == [
            ("A", 503),
            ("B", 200),
            ("C", 200),
            ("X", 200),
            ("A", 200),
        ]

        row_a = await store.get_outbox_alert(enq_a.record.id)
        row_b = await store.get_outbox_alert(enq_b.record.id)
        row_c = await store.get_outbox_alert(enq_c.record.id)
        row_x = await store.get_outbox_alert(enq_x.record.id)
        assert row_a is not None and row_a.status == "DELIVERED" and row_a.attempt_count == 2
        assert row_b is not None and row_b.status == "DELIVERED" and row_b.attempt_count == 1
        assert row_c is not None and row_c.status == "DELIVERED" and row_c.attempt_count == 1
        assert row_x is not None and row_x.status == "DELIVERED" and row_x.attempt_count == 1
        assert svc._in_flight_owners == {}
    finally:
        release_b.set()
        release_x.set()
        store.close()
        await tg.aclose()
        await up.aclose()
