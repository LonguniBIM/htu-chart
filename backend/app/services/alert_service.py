"""Durable alert outbox orchestration, F03 suppression guard, and Telegram worker (F07, R03)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.engine.alert_engine import (
    AlertCandidate,
    AlertEngine,
    build_key_rotation_reminder_candidate,
    format_entry_alert_message,
    format_vn_timestamp,
    is_cme_weekend_window,
    normalize_candidate,
)
from app.engine.bar_aggregator import AggregatedBarEvent
from app.notify.telegram import TelegramClient
from app.security import redact_secrets
from app.storage.sqlite_store import (
    AlertOutboxRecord,
    AlertStatus,
    EnqueueAlertResult,
    SqliteStore,
)
from app.topstep.auth import TopstepAuthService

logger = logging.getLogger("topstep_chart.alert_service")


class TelegramNotConfiguredError(RuntimeError):
    """Raised when a manual Telegram test alert is requested without bot configuration."""


@dataclass(frozen=True)
class DispatchCycleStats:
    """Counters summarizing one outbox dispatch pass."""

    expired: int = 0
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0


class AlertService:
    """Coordinates AlertEngine, SQLite transactional outbox, TelegramClient, and key reminders."""

    def __init__(
        self,
        settings: Settings,
        engine: AlertEngine,
        sqlite_store: SqliteStore,
        telegram_client: TelegramClient,
        topstep_auth: TopstepAuthService,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._sqlite = sqlite_store
        self._telegram = telegram_client
        self._auth = topstep_auth
        self._clock = clock or (lambda: int(datetime.now(UTC).timestamp()))
        self._dispatch_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._last_error: str | None = None

    @property
    def engine(self) -> AlertEngine:
        """Underlying pure AlertEngine."""
        return self._engine

    @property
    def last_error(self) -> str | None:
        """Latest redacted alert processing/delivery error, if any."""
        return self._last_error

    def set_scanner_enabled(self, enabled: bool) -> None:
        """Toggle live strategy scanner state."""
        self._engine.set_scanner_enabled(enabled)

    def _redact(self, text: str) -> str:
        extra = (
            *self._auth.active_redaction_secrets(),
            self._settings.topstepx_api_key,
            self._settings.app_password_hash,
            self._settings.app_session_secret,
            self._settings.telegram_bot_token,
            self._settings.telegram_chat_id,
        )
        return self._telegram.redact_text(redact_secrets(text, extra))

    async def handle_bar_signal(
        self,
        event: AggregatedBarEvent,
        tag: str | None,
    ) -> list[EnqueueAlertResult]:
        """Evaluate a LiveFeedService bar signal with defense-in-depth F03 gating."""
        results: list[EnqueueAlertResult] = []
        try:
            candidates = self._engine.evaluate_bar_signal(event, tag)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = self._redact(str(exc))
            logger.warning("Alert signal evaluation failed: %s", self._last_error)
            return results

        # Defense-in-depth F03 guard: never enqueue non-LIVE or recovered historical signals.
        if tag is not None or event.is_recovered_historical:
            return []

        for candidate in candidates:
            try:
                res = await self.enqueue_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = self._redact(str(exc))
                logger.warning("Alert signal enqueue failed: %s", self._last_error)
                continue
            results.append(res)
            if res.inserted:
                self._wakeup.set()
        return results

    async def enqueue_candidate(
        self,
        candidate: AlertCandidate,
        *,
        created_at_utc: int | None = None,
    ) -> EnqueueAlertResult:
        """Normalize and persist an AlertCandidate transactionally into SQLite alert_outbox."""
        normalized = normalize_candidate(candidate)
        eff_created = self._clock() if created_at_utc is None else int(created_at_utc)
        ttl = (
            normalized.ttl_seconds
            if normalized.ttl_seconds is not None
            else self._settings.alert_ttl_seconds
        )
        effective_ttl = max(1, int(ttl))
        expires_at_utc = eff_created + effective_ttl
        max_attempts = 1 + self._settings.alert_max_retries
        payload_text = (
            normalized.message_text
            if normalized.message_text is not None
            else format_entry_alert_message(normalized, eff_created)
        )
        return await self._sqlite.enqueue_alert_outbox(
            strategy_id=normalized.strategy_id,
            strategy_version=normalized.strategy_version,
            live_mode=normalized.live_mode,
            contract_id=normalized.contract_id,
            timeframe=normalized.timeframe,
            bar_time=normalized.bar_time,
            direction=normalized.direction,
            payload_text=payload_text,
            created_at_utc=eff_created,
            expires_at_utc=expires_at_utc,
            alert_kind=normalized.alert_kind,
            max_attempts=max_attempts,
        )

    def _compute_retry_delay_sec(
        self,
        attempt_count: int,
        retry_after_sec: float | None,
    ) -> int:
        schedule = self._settings.alert_retry_backoff_sec
        if schedule:
            idx = min(max(0, attempt_count), len(schedule) - 1)
            backoff = float(schedule[idx])
        else:
            backoff = 1.0
        if retry_after_sec is not None and math.isfinite(retry_after_sec):
            backoff = max(backoff, float(retry_after_sec))
        return max(1, math.ceil(backoff))

    async def _dispatch_record_locked(self, record: AlertOutboxRecord) -> AlertStatus:
        """Dispatch a single PENDING outbox record under self._dispatch_lock."""
        send_now = self._clock()
        if send_now >= record.expires_at_utc:
            if await self._sqlite.mark_outbox_expired(record.id, send_now):
                return "EXPIRED"
            current = await self._sqlite.get_outbox_alert(record.id)
            return current.status if current is not None else "EXPIRED"

        result = await self._telegram.send_message(record.payload_text)
        done_now = self._clock()

        if result.ok:
            await self._sqlite.mark_outbox_delivered(record.id, done_now)
            self._last_error = None
            return "DELIVERED"

        if result.permanent_failure:
            safe_err = self._redact(
                result.error or f"Telegram permanent failure ({result.status_code})"
            )
            await self._sqlite.mark_outbox_failed(
                record.id,
                done_now,
                safe_err,
                increment_attempt=True,
            )
            self._last_error = safe_err
            return "FAILED"

        safe_err = self._redact(
            result.error or f"Telegram transient failure ({result.status_code})"
        )
        self._last_error = safe_err
        delay_sec = self._compute_retry_delay_sec(
            record.attempt_count,
            result.retry_after_sec,
        )
        remaining = max(0, record.expires_at_utc - done_now)
        next_attempt_at_utc = done_now + min(delay_sec, remaining)
        new_status = await self._sqlite.mark_outbox_retry(
            record.id,
            done_now,
            next_attempt_at_utc,
            safe_err,
        )
        if new_status is not None:
            return new_status
        current = await self._sqlite.get_outbox_alert(record.id)
        return current.status if current is not None else "FAILED"

    async def process_once(self, limit: int = 20) -> DispatchCycleStats:
        """Expire stale rows and deliver due PENDING outbox alerts sequentially under lock."""
        async with self._dispatch_lock:
            now_utc = self._clock()
            expired_count = await self._sqlite.expire_stale_outbox_alerts(now_utc)
            due_alerts = await self._sqlite.claim_due_outbox_alerts(now_utc, limit=limit)
            delivered = 0
            retried = 0
            failed = 0

            for record in due_alerts:
                outcome = await self._dispatch_record_locked(record)
                if outcome == "DELIVERED":
                    delivered += 1
                elif outcome == "EXPIRED":
                    expired_count += 1
                elif outcome == "FAILED":
                    failed += 1
                elif outcome == "PENDING":
                    retried += 1

            return DispatchCycleStats(
                expired=expired_count,
                claimed=len(due_alerts),
                delivered=delivered,
                retried=retried,
                failed=failed,
            )

    async def check_key_rotation_reminder(self) -> EnqueueAlertResult | None:
        """Enqueue a weekend-only TopstepX API key rotation reminder when due."""
        now = self._clock()
        rotation = self._auth.compute_key_rotation(now=datetime.fromtimestamp(now, tz=UTC))
        if (
            not rotation.key_rotation_due
            or rotation.key_age_days is None
            or not is_cme_weekend_window(now)
            or not self._telegram.is_configured
        ):
            return None

        candidate = build_key_rotation_reminder_candidate(
            key_age_days=rotation.key_age_days,
            rotation_days=self._settings.key_rotation_days,
            enforced=rotation.key_rotation_enforced,
            now_utc=now,
        )
        if candidate is None:
            return None
        res = await self.enqueue_candidate(candidate, created_at_utc=now)
        if res.inserted:
            self._wakeup.set()
        return res

    async def send_test_alert(self) -> AlertOutboxRecord:
        """Enqueue and immediately dispatch a unique manual Telegram connectivity test alert."""
        if not self._telegram.is_configured:
            raise TelegramNotConfiguredError("Telegram bot token or chat ID is not configured")

        now_utc = self._clock()
        candidate = AlertCandidate(
            strategy_id=f"__manual_test__:{secrets.token_hex(8)}",
            strategy_version="1",
            live_mode=self._auth.live,
            contract_id="__SYSTEM__",
            root_symbol=None,
            timeframe="1m",
            bar_time=now_utc,
            direction="INFO",
            alert_kind="TEST",
            reason="Manual Telegram connectivity test",
            message_text=(
                "TopstepX Chart Terminal: Telegram test alert OK "
                f"({format_vn_timestamp(now_utc)})"
            ),
            ttl_seconds=self._settings.alert_ttl_seconds,
        )
        async with self._dispatch_lock:
            enqueued = await self.enqueue_candidate(candidate, created_at_utc=now_utc)
            await self._dispatch_record_locked(enqueued.record)
        updated = await self._sqlite.get_outbox_alert(enqueued.record.id)
        return updated or enqueued.record

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                await self.check_key_rotation_reminder()
                if self._settings.alert_dispatcher_enabled:
                    await self.process_once()
                else:
                    await self._sqlite.expire_stale_outbox_alerts(self._clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = self._redact(str(exc))
                logger.warning("Alert outbox worker error: %s", self._last_error)

            if not self._running:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=self._settings.alert_dispatch_poll_interval_sec,
                )
            self._wakeup.clear()

    async def start(self) -> None:
        """Start the background outbox dispatcher non-blockingly (R03)."""
        if self._running:
            return
        self._running = True
        self._wakeup.clear()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="alert-outbox-worker",
            )

    async def stop(self) -> None:
        """Stop the background outbox dispatcher cleanly on shutdown."""
        self._running = False
        self._wakeup.set()
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None

    async def get_status_snapshot(self) -> dict[str, Any]:
        """Return strictly redacted alert engine and outbox telemetry."""
        outbox = await self._sqlite.get_outbox_summary()
        return {
            "telegram_configured": self._telegram.is_configured,
            "scanner_enabled": self._engine.scanner_enabled,
            "alert_ttl_seconds": self._settings.alert_ttl_seconds,
            "outbox": outbox,
            "suppressed_signals": self._engine.suppressed_signals,
            "recovered_historical_skipped": self._engine.recovered_historical_skipped,
            "last_error": self._last_error,
        }

    async def list_history(
        self,
        *,
        status: AlertStatus | None = None,
        limit: int = 50,
    ) -> list[AlertOutboxRecord]:
        """List recent alert_outbox entries with optional status filter."""
        return await self._sqlite.list_outbox_alerts(status=status, limit=limit)
