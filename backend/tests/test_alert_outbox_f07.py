"""Value-first tests for F07 durable alert outbox, AlertEngine, Telegram, and /api/alerts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from argon2 import PasswordHasher

from app.config import Settings
from app.engine.alert_engine import AlertEngine, AlertSignal
from app.engine.bar_aggregator import AggregatedBarEvent, BarAggregator
from app.engine.ohlcv import OHLCVBar
from app.engine.session_calendar import NY_TZ
from app.main import create_app
from app.notify.telegram import (
    TelegramAlertDispatcher,
    is_cme_weekend_closed,
    weekend_bucket_utc,
)
from app.services.live_feed_service import LiveFeedService
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import SIGNALR_RECORD_SEPARATOR

CID_MES = "CON.F.US.MES.Z26"
CID_MNQ = "CON.F.US.MNQ.Z26"
BASE_T = int(datetime(2026, 11, 10, 14, 0, 0, tzinfo=UTC).timestamp())


class _StubSignalRule:
    """Deterministic test scanner rule emitting one LONG signal per closed bar."""

    def __init__(self, *, strategy_id: str = "ict_fvg_sweep", version: str = "1.0.0") -> None:
        self.strategy_id = strategy_id
        self.version = version

    def evaluate(self, event: AggregatedBarEvent) -> list[AlertSignal]:
        return [
            AlertSignal(
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                live_mode=event.live_mode,
                contract_id=event.contract_id,
                root_symbol=event.root_symbol,
                timeframe=event.timeframe,
                bar_time=event.bar.time,
                direction="LONG",
                reason="Bullish FVG + Liquidity Sweep",
                entry_price=event.bar.close,
                stop_loss=event.bar.low - 1.0,
                take_profit=event.bar.close + 4.0,
                rr_ratio=2.0,
            )
        ]


class _MockSignalRTransport:
    """Minimal SignalR transport for end-to-end LiveFeedService recovery tests."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.handshake_done = asyncio.Event()

    async def send(self, data: str) -> None:
        for chunk in data.split(SIGNALR_RECORD_SEPARATOR):
            if chunk.strip():
                frame = json.loads(chunk)
                if frame.get("protocol") == "json":
                    await self.incoming.put("{}" + SIGNALR_RECORD_SEPARATOR)
                    self.handshake_done.set()

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        await self.incoming.put(ConnectionError("closed"))


def _make_closed_bar_event(
    *,
    contract_id: str = CID_MES,
    live_mode: bool = False,
    timeframe: str = "5m",
    bar_time: int = BASE_T,
    is_closed: bool = True,
    is_recovered_historical: bool = False,
) -> AggregatedBarEvent:
    return AggregatedBarEvent(
        contract_id=contract_id,
        live_mode=live_mode,
        timeframe=timeframe,
        bar=OHLCVBar(
            time=bar_time,
            open=5800.0,
            high=5805.0,
            low=5799.0,
            close=5804.0,
            volume=120.0,
            is_closed=is_closed,
        ),
        is_recovered_historical=is_recovered_historical,
        root_symbol="MES" if "MES" in contract_id else "MNQ",
    )


@asynccontextmanager
async def _authed_app_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    upstream = httpx.AsyncClient(
        transport=transport or httpx.MockTransport(lambda _req: httpx.Response(404))
    )
    app = create_app(settings=settings, http_client=upstream)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as client:
                yield client, app
    finally:
        await upstream.aclose()


def test_f07_sqlite_outbox_unique_dedup_across_restarts(tmp_path: Path) -> None:
    """Contract: SQLite alert_outbox deduplicates unique tuple across restarts.

    Defect caught: Duplicate alerts after restart or collapsed contract_id/strategy_version.
    """
    db_path = tmp_path / "metadata.db"
    store1 = SqliteStore(db_path)
    store1.initialize()

    first, created1 = store1.enqueue_alert_sync(
        strategy_id="ict_fvg_sweep",
        strategy_version="1.0.0",
        live_mode=False,
        contract_id=CID_MES,
        timeframe="5m",
        bar_time=BASE_T,
        direction="LONG",
        payload_json='{"text":"alert-1"}',
        created_at_utc=BASE_T,
        expires_at_utc=BASE_T + 90,
    )
    assert created1 is True
    assert first.status == "PENDING"

    dup_same_proc, created2 = store1.enqueue_alert_sync(
        strategy_id="ict_fvg_sweep",
        strategy_version="1.0.0",
        live_mode=False,
        contract_id=CID_MES,
        timeframe="5m",
        bar_time=BASE_T,
        direction="LONG",
        payload_json='{"text":"alert-1-dup"}',
        created_at_utc=BASE_T + 5,
        expires_at_utc=BASE_T + 95,
    )
    assert created2 is False
    assert dup_same_proc.id == first.id
    store1.close()

    # Reopen SqliteStore from disk to simulate restart
    store2 = SqliteStore(db_path)
    store2.initialize()
    try:
        dup_after_restart, created3 = store2.enqueue_alert_sync(
            strategy_id="ict_fvg_sweep",
            strategy_version="1.0.0",
            live_mode=False,
            contract_id=CID_MES,
            timeframe="5m",
            bar_time=BASE_T,
            direction="LONG",
            payload_json='{"text":"alert-1-restart"}',
            created_at_utc=BASE_T + 10,
            expires_at_utc=BASE_T + 100,
        )
        assert created3 is False
        assert dup_after_restart.id == first.id

        # Distinct contract_id inserts a new row
        diff_contract, created_contract = store2.enqueue_alert_sync(
            strategy_id="ict_fvg_sweep",
            strategy_version="1.0.0",
            live_mode=False,
            contract_id=CID_MNQ,
            timeframe="5m",
            bar_time=BASE_T,
            direction="LONG",
            payload_json='{"text":"alert-mnq"}',
            created_at_utc=BASE_T + 10,
            expires_at_utc=BASE_T + 100,
        )
        assert created_contract is True
        assert diff_contract.id != first.id

        # Distinct strategy_version inserts a new row
        diff_version, created_version = store2.enqueue_alert_sync(
            strategy_id="ict_fvg_sweep",
            strategy_version="2.0.0",
            live_mode=False,
            contract_id=CID_MES,
            timeframe="5m",
            bar_time=BASE_T,
            direction="LONG",
            payload_json='{"text":"alert-v2"}',
            created_at_utc=BASE_T + 10,
            expires_at_utc=BASE_T + 100,
        )
        assert created_version is True
        assert diff_version.id not in {first.id, diff_contract.id}

        rows = store2.list_alerts_sync(limit=20)
        assert len(rows) == 3
        assert store2.get_alert_summary_sync()["PENDING"] == 3
    finally:
        store2.close()


async def test_f07_dispatcher_expires_stale_alerts_without_sending(tmp_path: Path) -> None:
    """Contract: PENDING alerts with now >= expires_at become EXPIRED and never call Telegram.

    Defect caught: Stale intraday entry signals firing late after an outage or restart.
    """
    db_path = tmp_path / "metadata.db"
    store = SqliteStore(db_path)
    store.initialize()
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": request.content.decode("utf-8")})
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        data_dir=tmp_path,
        telegram_bot_token="123456:TEST-SECRET-TELEGRAM-TOKEN",
        telegram_chat_id="-100999888",
        alert_ttl_seconds=90,
    )
    engine = AlertEngine(rules=[_StubSignalRule()])
    prepared = engine.evaluate_bar_event(_make_closed_bar_event(bar_time=BASE_T))
    assert len(prepared) == 1
    assert "UTC+7" in prepared[0].message_text

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = TelegramAlertDispatcher(settings, store, http_client=client)
        rec, created = await dispatcher.enqueue_prepared_alert(prepared[0], now_utc=BASE_T)
        assert created is True
        assert rec.expires_at_utc == BASE_T + 90

        # Simulate outage until TTL has elapsed
        await dispatcher.process_pending_once(now_utc=BASE_T + 90)

    assert len(calls) == 0
    rows = await store.list_alerts()
    assert len(rows) == 1
    assert rows[0].status == "EXPIRED"
    assert (await store.get_alert_summary())["EXPIRED"] == 1
    store.close()


async def test_f07_dispatcher_retries_transient_failure_then_delivers_before_ttl(
    tmp_path: Path,
) -> None:
    """Contract: Transient 503 retries with bounded backoff and delivers on HTTP 200 before TTL.

    Defect caught: Dropping valid signals on first transient error or failing to increment attempts.
    """
    store = SqliteStore(tmp_path / "metadata.db")
    store.initialize()
    attempt_num = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempt_num
        attempt_num += 1
        if attempt_num == 1:
            return httpx.Response(503, json={"ok": False, "description": "Service Unavailable"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    settings = Settings(
        data_dir=tmp_path,
        telegram_bot_token="123456:RETRY-BOT-SECRET",
        telegram_chat_id="-100111222",
        alert_ttl_seconds=90,
        alert_max_retries=3,
    )
    engine = AlertEngine(rules=[_StubSignalRule()])
    prepared = engine.evaluate_bar_event(_make_closed_bar_event(bar_time=BASE_T))[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dispatcher = TelegramAlertDispatcher(
            settings,
            store,
            http_client=client,
            retry_backoff_seconds=(2, 5, 15),
        )
        await dispatcher.enqueue_prepared_alert(prepared, now_utc=BASE_T)

        # Attempt 1 -> 503 -> stays PENDING with attempts=1 and next_attempt_at = BASE_T + 2
        await dispatcher.process_pending_once(now_utc=BASE_T)
        after_first = (await store.list_alerts())[0]
        assert after_first.status == "PENDING"
        assert after_first.attempts == 1
        assert after_first.next_attempt_at_utc == BASE_T + 2

        # Before backoff window elapses -> not claimed
        processed_early = await dispatcher.process_pending_once(now_utc=BASE_T + 1)
        assert processed_early == 0
        assert attempt_num == 1

        # At BASE_T + 2 -> attempt 2 returns 200 -> DELIVERED
        processed_due = await dispatcher.process_pending_once(now_utc=BASE_T + 2)
        assert processed_due == 1
        after_second = (await store.list_alerts())[0]
        assert after_second.status == "DELIVERED"
        assert after_second.attempts == 1
        assert after_second.last_error is None
        assert attempt_num == 2

    store.close()


async def test_f07_dispatcher_marks_failed_on_4xx_and_max_retries_with_redaction(
    tmp_path: Path,
) -> None:
    """Contract: Permanent 4xx or exhausted retries transition to FAILED with bot token redacted.

    Defect caught: Infinite retry on HTTP 400 or leaking telegram_bot_token into SQLite last_error.
    """
    secret_token = "987654321:SUPER-SECRET-TG-BOT-TOKEN"
    store = SqliteStore(tmp_path / "metadata.db")
    store.initialize()

    def handler_400(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "description": f"Bad Request token={secret_token}"},
        )

    settings = Settings(
        data_dir=tmp_path,
        telegram_bot_token=secret_token,
        telegram_chat_id="-100333444",
        alert_ttl_seconds=90,
        alert_max_retries=1,
    )
    engine = AlertEngine(rules=[_StubSignalRule()])
    prep1 = engine.evaluate_bar_event(_make_closed_bar_event(bar_time=BASE_T))[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_400)) as client:
        dispatcher = TelegramAlertDispatcher(settings, store, http_client=client)
        rec1, _ = await dispatcher.enqueue_prepared_alert(prep1, now_utc=BASE_T)
        await dispatcher.process_pending_once(now_utc=BASE_T)

    row1 = [r for r in await store.list_alerts() if r.id == rec1.id][0]
    assert row1.status == "FAILED"
    assert row1.last_error is not None
    assert secret_token not in row1.last_error

    # Now verify exhausting alert_max_retries=1 on transient transport error -> FAILED
    prep2 = engine.evaluate_bar_event(_make_closed_bar_event(bar_time=BASE_T + 300))[0]

    def handler_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Failed connecting to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_transport)) as client:
        dispatcher2 = TelegramAlertDispatcher(
            settings,
            store,
            http_client=client,
            retry_backoff_seconds=(2, 5, 15),
        )
        rec2, _ = await dispatcher2.enqueue_prepared_alert(prep2, now_utc=BASE_T)
        # First failure -> attempts becomes 1 (<= max_retries=1), scheduled for BASE_T + 2
        await dispatcher2.process_pending_once(now_utc=BASE_T)
        mid2 = [r for r in await store.list_alerts() if r.id == rec2.id][0]
        assert mid2.status == "PENDING"
        assert mid2.attempts == 1

        # Second failure -> attempts+1 = 2 > max_retries=1 -> FAILED
        await dispatcher2.process_pending_once(now_utc=BASE_T + 2)
        row2 = [r for r in await store.list_alerts() if r.id == rec2.id][0]
        assert row2.status == "FAILED"
        assert row2.attempts == 2
        assert row2.last_error is not None
        assert secret_token not in row2.last_error

    store.close()


async def test_f07_recovery_historical_signals_are_suppressed_and_never_sent(
    tmp_path: Path,
) -> None:
    """Contract: Recovery RECOVERED_HISTORICAL signals become SUPPRESSED and never call Telegram.

    Defect caught: Reconnect backfill replaying old closed bars as live Telegram entry alerts.
    """
    store = SqliteStore(tmp_path / "metadata.db")
    store.initialize()
    parquet_store = ParquetStore(tmp_path / "data", sqlite_store=store)
    telegram_calls: list[str] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        telegram_calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    def topstep_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-f07-test"})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        {
                            "t": BASE_T - 120,
                            "o": 5800.0,
                            "h": 5804.0,
                            "l": 5799.0,
                            "c": 5803.0,
                            "v": 100.0,
                        },
                        {
                            "t": BASE_T - 60,
                            "o": 5803.0,
                            "h": 5806.0,
                            "l": 5802.0,
                            "c": 5805.0,
                            "v": 120.0,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    settings = Settings(
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:RECOVERY-BOT-TOKEN",
        telegram_chat_id="-100555666",
        signalr_enabled=False,
    )
    engine = AlertEngine(rules=[_StubSignalRule()])

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(topstep_handler)) as ts_client,
        httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as tg_client,
    ):
        auth = TopstepAuthService(settings, ts_client)
        await auth.login("alice", "api-key-f07")
        rest = TopstepRestClient(settings, auth, ts_client)
        dispatcher = TelegramAlertDispatcher(settings, store, http_client=tg_client)

        async def _on_bar_signal(event: AggregatedBarEvent, tag: str | None) -> None:
            for prep in engine.evaluate_bar_event(event, signal_tag=tag):
                await dispatcher.enqueue_prepared_alert(prep, now_utc=BASE_T)

        feed = LiveFeedService(
            settings,
            auth,
            rest,
            parquet_store,
            store,
            aggregator=BarAggregator(),
            on_bar_signal=_on_bar_signal,
            clock=lambda: BASE_T,
        )
        await feed.set_active_contracts([CID_MES], live_mode=False)
        await feed.trigger_recovery(now_utc=BASE_T)
        await dispatcher.process_pending_once(now_utc=BASE_T)

    assert len(telegram_calls) == 0
    rows = await store.list_alerts()
    assert len(rows) >= 1
    assert all(r.status == "SUPPRESSED" for r in rows)
    assert all(r.signal_tag == "RECOVERED_HISTORICAL" for r in rows)
    assert (await store.get_alert_summary())["PENDING"] == 0
    store.close()


async def test_f07_weekend_key_rotation_reminder_dedup_and_market_hours_gate(
    tmp_path: Path,
) -> None:
    """Contract: Weekend key rotation reminder enqueues once per weekend, never when market is open.

    Defect caught: Spamming key rotation alerts every poll or interrupting weekday sessions.
    """
    store = SqliteStore(tmp_path / "metadata.db")
    store.initialize()

    # Saturday 14:00 America/New_York (CME closed)
    sat_ny = int(datetime(2026, 11, 14, 14, 0, 0, tzinfo=NY_TZ).timestamp())
    # Sunday 10:00 America/New_York (same weekend, still closed before 18:00 ET)
    sun_ny = int(datetime(2026, 11, 15, 10, 0, 0, tzinfo=NY_TZ).timestamp())
    # Wednesday 10:00 America/New_York (CME open)
    wed_ny = int(datetime(2026, 11, 11, 10, 0, 0, tzinfo=NY_TZ).timestamp())

    assert is_cme_weekend_closed(sat_ny) is True
    assert is_cme_weekend_closed(sun_ny) is True
    assert is_cme_weekend_closed(wed_ny) is False
    assert weekend_bucket_utc(sat_ny) == weekend_bucket_utc(sun_ny)

    settings = Settings(
        data_dir=tmp_path,
        topstepx_key_updated_at="2026-10-01",
        key_rotation_days=30,
        telegram_bot_token="123456:WEEKEND-BOT-TOKEN",
        telegram_chat_id="-100777888",
    )
    tg_calls: list[dict[str, Any]] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        tg_calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as client:
        auth = TopstepAuthService(settings, client)
        dispatcher = TelegramAlertDispatcher(settings, store, http_client=client, auth_service=auth)

        # Market open Wednesday -> does NOT enqueue
        wed_res = await dispatcher.maybe_enqueue_weekend_key_reminder(now_utc=wed_ny)
        assert wed_res is None
        assert len(await store.list_alerts()) == 0

        # Saturday -> enqueues once and delivers
        await dispatcher.process_pending_once(now_utc=sat_ny)
        assert len(tg_calls) == 1
        assert "Rotation Reminder" in tg_calls[0]["text"]

        # Repeated poll on Saturday & Sunday -> deduplicated by weekend bucket
        await dispatcher.process_pending_once(now_utc=sat_ny + 60)
        await dispatcher.process_pending_once(now_utc=sun_ny)
        assert len(tg_calls) == 1

        rows = await store.list_alerts()
        assert len(rows) == 1
        assert rows[0].strategy_id == "system_key_rotation"
        assert rows[0].signal_tag == "SYSTEM"
        assert rows[0].status == "DELIVERED"

    store.close()


async def test_f07_alert_routes_auth_csrf_and_secret_redaction(tmp_path: Path) -> None:
    """Contract: /api/alerts* enforces session + CSRF and never exposes Telegram secrets.

    Defect caught: Unauthenticated alert inspection, CSRF bypass on scanner/test, or token leakage.
    """
    pw = "alerts-api-password"
    pw_hash = PasswordHasher().hash(pw)
    bot_token = "555666777:ALERT-ROUTE-SECRET-TOKEN"
    chat_id = "-100999111222"
    tg_test_calls: list[dict[str, Any]] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            tg_test_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "data",
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        alert_poll_interval_sec=60.0,
    )

    async with _authed_app_client(
        settings,
        transport=httpx.MockTransport(tg_handler),
    ) as (client, app):
        # Unauthenticated requests rejected
        assert (await client.get("/api/alerts")).status_code == 401
        scanner_unauth = await client.post("/api/alerts/scanner", json={"enabled": False})
        assert scanner_unauth.status_code == 401
        assert (await client.post("/api/alerts/test")).status_code == 401

        login_resp = await client.post("/api/auth/login", json={"password": pw})
        assert login_resp.status_code == 200
        csrf = login_resp.json()["csrf_token"]

        # Mutating endpoints without CSRF rejected with 403
        scanner_nocsrf = await client.post("/api/alerts/scanner", json={"enabled": False})
        assert scanner_nocsrf.status_code == 403
        assert (await client.post("/api/alerts/test")).status_code == 403

        # Seed a row whose error had an inline bot token-like value to verify response redaction
        await app.state.sqlite_store.enqueue_alert(
            strategy_id="ict_fvg_sweep",
            strategy_version="1.0.0",
            live_mode=False,
            contract_id=CID_MES,
            timeframe="5m",
            bar_time=BASE_T,
            direction="LONG",
            payload_json='{"text":"Signal ready"}',
            created_at_utc=BASE_T,
            expires_at_utc=BASE_T + 90,
            status="FAILED",
            last_error=f"Upstream error at https://api.telegram.org/bot{bot_token}/sendMessage",
        )

        # Toggle scanner off and back on with CSRF
        toggle_off = await client.post(
            "/api/alerts/scanner",
            json={"enabled": False},
            headers={"X-CSRF-Token": csrf},
        )
        assert toggle_off.status_code == 200
        assert toggle_off.json()["scanner_enabled"] is False
        assert app.state.alert_engine.enabled is False

        # Send test Telegram message with CSRF
        test_resp = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
        assert test_resp.status_code == 200
        assert test_resp.json()["ok"] is True
        assert len(tg_test_calls) == 1
        assert tg_test_calls[0]["chat_id"] == chat_id

        # GET /api/alerts and GET /api/status must not leak bot_token or chat_id
        alerts_resp = await client.get("/api/alerts")
        assert alerts_resp.status_code == 200
        alerts_body = alerts_resp.json()
        assert alerts_body["scanner_enabled"] is False
        assert alerts_body["telegram_configured"] is True
        assert alerts_body["summary"]["FAILED"] == 1
        assert len(alerts_body["alerts"]) == 1

        status_resp = await client.get("/api/status")
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["alerts_summary"]["FAILED"] == 1

        serialized_all = json.dumps({"alerts": alerts_body, "status": status_body})
        assert bot_token not in serialized_all
        assert chat_id not in serialized_all
        assert pw_hash not in serialized_all


async def test_f07_alert_engine_open_bar_retry_past_ttl_and_test_route_errors(
    tmp_path: Path,
) -> None:
    """Contract: Open bars/disabled scanner emit nothing; retry past TTL expires; 400/502 redacted.

    Defect caught: Intrabar alert spam, retry scheduling past TTL, or secret leak in 502 detail.
    """
    engine = AlertEngine(rules=[_StubSignalRule()], enabled=True)
    assert engine.evaluate_bar_event(_make_closed_bar_event(is_closed=False)) == []
    engine.set_enabled(False)
    assert engine.evaluate_bar_event(_make_closed_bar_event(is_closed=True)) == []
    engine.set_enabled(True)
    prepared = engine.evaluate_bar_event(_make_closed_bar_event(is_closed=True))[0]

    # Retry whose backoff (5s) exceeds remaining TTL (3s) -> EXPIRED
    store = SqliteStore(tmp_path / "retry_ttl.db")
    store.initialize()
    ttl_settings = Settings(
        data_dir=tmp_path,
        telegram_bot_token="111:RETRY-TTL-SECRET",
        telegram_chat_id="-100444",
        alert_ttl_seconds=3,
        alert_max_retries=3,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(503, json={"ok": False}))
    ) as tg_client:
        dispatcher = TelegramAlertDispatcher(
            ttl_settings,
            store,
            http_client=tg_client,
            retry_backoff_seconds=(5, 10, 15),
        )
        await dispatcher.enqueue_prepared_alert(prepared, now_utc=BASE_T)
        await dispatcher.process_pending_once(now_utc=BASE_T)
    expired_row = (await store.list_alerts())[0]
    assert expired_row.status == "EXPIRED"
    store.close()

    # /api/alerts/test returns 400 when unconfigured
    pw = "alerts-err-pw"
    pw_hash = PasswordHasher().hash(pw)
    unconf_settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "unconf",
    )
    async with _authed_app_client(unconf_settings) as (client, _app):
        login_resp = await client.post("/api/auth/login", json={"password": pw})
        csrf = login_resp.json()["csrf_token"]
        resp_400 = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
        assert resp_400.status_code == 400

    # /api/alerts/test returns 502 with redacted detail on Telegram upstream failure
    secret_tok = "999888777:UPSTREAM-FAIL-SECRET"
    fail_settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "fail",
        telegram_bot_token=secret_tok,
        telegram_chat_id="-100888",
    )

    def fail_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "description": f"Bad gateway token={secret_tok}"},
        )

    async with _authed_app_client(
        fail_settings,
        transport=httpx.MockTransport(fail_handler),
    ) as (client, _app):
        login_resp = await client.post("/api/auth/login", json={"password": pw})
        csrf = login_resp.json()["csrf_token"]
        resp_502 = await client.post("/api/alerts/test", headers={"X-CSRF-Token": csrf})
        assert resp_502.status_code == 502
        assert secret_tok not in resp_502.text
