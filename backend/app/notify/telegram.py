"""Async Telegram alert outbox dispatcher and weekend key rotation reminder (F07, R03, SEC-002)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.engine.alert_engine import PreparedAlert, format_vietnam_time
from app.engine.session_calendar import NY_TZ
from app.security import redact_secrets
from app.storage.sqlite_store import AlertOutboxRecord, SqliteStore
from app.topstep.auth import TopstepAuthService, build_httpx_timeout

logger = logging.getLogger("topstep_chart.telegram")

DEFAULT_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (2, 5, 15)
_WEEKEND_REMINDER_TTL_SEC = 86400


class TelegramError(Exception):
    """Base error for Telegram notification failures."""


class TelegramConfigError(TelegramError):
    """Raised when Telegram bot token or chat ID is missing."""


class TelegramUpstreamError(TelegramError):
    """Raised when Telegram Bot API returns an error or times out."""


@dataclass(frozen=True)
class SendOutcome:
    """Result of a single Telegram Bot API sendMessage attempt."""

    ok: bool
    permanent: bool = False
    error: str | None = None


def is_cme_weekend_closed(now_utc: int) -> bool:
    """Return True during CME Globex weekend closure in America/New_York."""
    ny_dt = datetime.fromtimestamp(int(now_utc), tz=UTC).astimezone(NY_TZ)
    wd = ny_dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    ny_t = ny_dt.timetz().replace(tzinfo=None)
    if wd == 5:
        return True
    if wd == 4 and ny_t >= time(17, 0):
        return True
    return wd == 6 and ny_t < time(18, 0)


def weekend_bucket_utc(now_utc: int) -> int:
    """Compute a stable UTC timestamp identifying the current CME weekend window."""
    ny_dt = datetime.fromtimestamp(int(now_utc), tz=UTC).astimezone(NY_TZ)
    wd = ny_dt.weekday()
    if wd == 4:
        friday_date = ny_dt.date()
    elif wd == 5:
        friday_date = ny_dt.date() - timedelta(days=1)
    elif wd == 6:
        friday_date = ny_dt.date() - timedelta(days=2)
    else:
        # Fallback to most recent Friday if called outside weekend.
        days_since_fri = (wd - 4) % 7
        friday_date = ny_dt.date() - timedelta(days=days_since_fri)
    bucket_dt = datetime.combine(friday_date, time(17, 0), tzinfo=NY_TZ)
    return int(bucket_dt.timestamp())


class TelegramAlertDispatcher:
    """Background outbox processor delivering Telegram alerts with bounded TTL retries."""

    def __init__(
        self,
        settings: Settings,
        sqlite_store: SqliteStore,
        http_client: httpx.AsyncClient | None = None,
        *,
        auth_service: TopstepAuthService | None = None,
        retry_backoff_seconds: Sequence[int] = DEFAULT_RETRY_BACKOFF_SECONDS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._sqlite = sqlite_store
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=build_httpx_timeout(settings))
        self._auth = auth_service
        self._retry_backoff = tuple(max(1, int(sec)) for sec in retry_backoff_seconds) or (2,)
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp()))
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def is_configured(self) -> bool:
        """Return True iff both telegram_bot_token and telegram_chat_id are non-empty."""
        return bool(
            self._settings.telegram_bot_token.strip()
            and self._settings.telegram_chat_id.strip()
        )

    def _redact(self, text: str) -> str:
        secrets: list[str] = []
        if self._settings.telegram_bot_token:
            secrets.append(self._settings.telegram_bot_token)
        if self._settings.telegram_chat_id:
            secrets.append(self._settings.telegram_chat_id)
        if self._settings.topstepx_api_key:
            secrets.append(self._settings.topstepx_api_key)
        if self._auth is not None:
            secrets.extend(self._auth.active_redaction_secrets())
        return redact_secrets(text, secrets)

    def _send_url(self) -> str:
        base = self._settings.telegram_api_base_url.rstrip("/")
        token = self._settings.telegram_bot_token.strip()
        return f"{base}/bot{token}/sendMessage"

    def _backoff_for_attempt(self, attempts: int) -> int:
        idx = min(max(0, attempts), len(self._retry_backoff) - 1)
        return self._retry_backoff[idx]

    @staticmethod
    def _extract_message_text(record: AlertOutboxRecord) -> str:
        try:
            payload = json.loads(record.payload_json)
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                return payload["text"]
        except json.JSONDecodeError:
            pass
        return (
            f"Alert {record.strategy_id} {record.contract_id} "
            f"{record.timeframe} {record.direction}"
        )

    async def enqueue_prepared_alert(
        self,
        prepared: PreparedAlert,
        *,
        now_utc: int | None = None,
    ) -> tuple[AlertOutboxRecord, bool]:
        """Persist an evaluated alert in SQLite outbox without blocking on Telegram HTTP."""
        eff_now = now_utc if now_utc is not None else self._clock()
        sig = prepared.signal
        payload: dict[str, Any] = {
            "strategy_id": sig.strategy_id,
            "strategy_version": sig.strategy_version,
            "live_mode": sig.live_mode,
            "contract_id": sig.contract_id,
            "root_symbol": sig.root_symbol,
            "timeframe": sig.timeframe,
            "bar_time": sig.bar_time,
            "direction": sig.direction,
            "reason": sig.reason,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "rr_ratio": sig.rr_ratio,
            "signal_tag": sig.signal_tag,
            "text": prepared.message_text,
        }
        status_val = "SUPPRESSED" if prepared.suppress_delivery else "PENDING"
        last_err = (
            f"Suppressed non-live delivery ({sig.signal_tag})"
            if prepared.suppress_delivery
            else None
        )
        return await self._sqlite.enqueue_alert(
            strategy_id=sig.strategy_id,
            strategy_version=sig.strategy_version,
            live_mode=sig.live_mode,
            contract_id=sig.contract_id,
            timeframe=sig.timeframe,
            bar_time=sig.bar_time,
            direction=sig.direction,
            signal_tag=sig.signal_tag,
            status=status_val,
            payload_json=json.dumps(payload, separators=(",", ":")),
            created_at_utc=eff_now,
            expires_at_utc=eff_now + self._settings.alert_ttl_seconds,
            next_attempt_at_utc=eff_now,
            last_error=last_err,
        )

    async def maybe_enqueue_weekend_key_reminder(
        self,
        *,
        now_utc: int | None = None,
    ) -> tuple[AlertOutboxRecord, bool] | None:
        """Enqueue a once-per-weekend TopstepX API key rotation reminder when due."""
        if self._auth is None:
            return None
        eff_now = now_utc if now_utc is not None else self._clock()
        if not is_cme_weekend_closed(eff_now):
            return None

        auth_status = self._auth.get_status(now=datetime.fromtimestamp(eff_now, tz=UTC))
        if not auth_status.get("key_rotation_due"):
            return None

        age_days = auth_status.get("key_age_days")
        bucket = weekend_bucket_utc(eff_now)
        text = (
            f"[SYSTEM] TopstepX API Key Rotation Reminder\n"
            f"Current key age: {age_days} day(s) "
            f"(threshold: {self._settings.key_rotation_days}d).\n"
            f"CME Globex market is currently closed for the weekend — safe window to rotate your "
            f"key at topstepx.com/settings?tab=api.\n"
            f"Time (VN): {format_vietnam_time(eff_now)}"
        )
        payload = {
            "kind": "WEEKEND_KEY_ROTATION_REMINDER",
            "key_age_days": age_days,
            "key_rotation_days": self._settings.key_rotation_days,
            "text": text,
        }
        return await self._sqlite.enqueue_alert(
            strategy_id="system_key_rotation",
            strategy_version="1",
            live_mode=bool(auth_status.get("live", self._settings.topstepx_live)),
            contract_id="SYSTEM",
            timeframe="1w",
            bar_time=bucket,
            direction="REMINDER",
            signal_tag="SYSTEM",
            status="PENDING",
            payload_json=json.dumps(payload, separators=(",", ":")),
            created_at_utc=eff_now,
            expires_at_utc=eff_now + _WEEKEND_REMINDER_TTL_SEC,
            next_attempt_at_utc=eff_now,
        )

    async def _send_telegram_message(self, text: str) -> SendOutcome:
        if not self.is_configured:
            return SendOutcome(
                ok=False,
                permanent=True,
                error="Telegram bot token or chat_id is not configured",
            )

        url = self._send_url()
        payload = {
            "chat_id": self._settings.telegram_chat_id.strip(),
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            resp = await asyncio.wait_for(
                self._client.post(url, json=payload),
                timeout=self._settings.upstream_total_timeout_sec,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return SendOutcome(ok=False, permanent=False, error="Telegram sendMessage timed out")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return SendOutcome(
                ok=False,
                permanent=False,
                error=self._redact(f"Telegram transport error: {exc}"),
            )

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
            return SendOutcome(ok=True)

        desc = ""
        if isinstance(body, dict):
            desc = str(body.get("description") or body.get("error") or "")
        err_msg = self._redact(desc or f"Telegram API HTTP {resp.status_code}")

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            return SendOutcome(ok=False, permanent=True, error=err_msg)
        return SendOutcome(ok=False, permanent=False, error=err_msg)

    async def send_test_message(self, *, now_utc: int | None = None) -> dict[str, Any]:
        """Send an immediate test notification via Telegram Bot API."""
        if not self.is_configured:
            raise TelegramConfigError("Telegram bot token or chat ID is not configured")
        eff_now = now_utc if now_utc is not None else self._clock()
        text = (
            f"[TEST] TopstepX Futures Terminal Telegram Alert Check\n"
            f"Time (VN): {format_vietnam_time(eff_now)}"
        )
        outcome = await self._send_telegram_message(text)
        if not outcome.ok:
            raise TelegramUpstreamError(outcome.error or "Telegram test message failed")
        return {"ok": True, "sent_at_utc": eff_now}

    async def process_pending_once(
        self,
        *,
        now_utc: int | None = None,
        limit: int = 20,
    ) -> int:
        """Process one batch of weekend reminders and due PENDING outbox alerts."""
        eff_now = now_utc if now_utc is not None else self._clock()
        await self.maybe_enqueue_weekend_key_reminder(now_utc=eff_now)

        due_alerts = await self._sqlite.claim_due_pending_alerts(eff_now, limit=limit)
        processed = 0
        for record in due_alerts:
            attempt_now = now_utc if now_utc is not None else self._clock()
            if attempt_now >= record.expires_at_utc:
                await self._sqlite.mark_alert_expired(
                    record.id,
                    last_error="Alert TTL expired before delivery",
                    now_utc=attempt_now,
                )
                processed += 1
                continue

            if not self.is_configured:
                await self._sqlite.mark_alert_failed(
                    record.id,
                    last_error="Telegram bot token or chat_id is not configured",
                    now_utc=attempt_now,
                )
                processed += 1
                continue

            text = self._extract_message_text(record)
            outcome = await self._send_telegram_message(text)
            finish_now = now_utc if now_utc is not None else self._clock()

            if outcome.ok:
                await self._sqlite.mark_alert_delivered(record.id, finish_now)
            else:
                safe_error = self._redact(outcome.error or "Telegram delivery failed")
                if outcome.permanent:
                    await self._sqlite.mark_alert_failed(
                        record.id,
                        last_error=safe_error,
                        now_utc=finish_now,
                    )
                elif record.attempts + 1 > self._settings.alert_max_retries:
                    await self._sqlite.mark_alert_failed(
                        record.id,
                        last_error=safe_error,
                        now_utc=finish_now,
                    )
                else:
                    backoff = self._backoff_for_attempt(record.attempts)
                    next_attempt = finish_now + backoff
                    if next_attempt >= record.expires_at_utc:
                        await self._sqlite.mark_alert_expired(
                            record.id,
                            last_error=safe_error,
                            now_utc=finish_now,
                        )
                    else:
                        await self._sqlite.mark_alert_retry(
                            record.id,
                            next_attempt_at_utc=next_attempt,
                            last_error=safe_error,
                            now_utc=finish_now,
                        )
            processed += 1
        return processed

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                await self.process_pending_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Alert outbox worker error: %s", self._redact(str(exc)))
            await asyncio.sleep(self._settings.alert_poll_interval_sec)

    async def start(self) -> None:
        """Start background Telegram alert outbox polling worker."""
        if self._running:
            return
        self._running = True
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="telegram-alert-dispatcher",
            )

    async def stop(self) -> None:
        """Stop background dispatcher worker and close owned HTTP client cleanly."""
        self._running = False
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None
        if self._owns_client:
            await self._client.aclose()
