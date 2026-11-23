"""Async Telegram Bot API client with strict timeout bounds and SEC-002 redaction (F07, R03)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.security import redact_secrets

logger = logging.getLogger("topstep_chart.notify.telegram")

_MAX_ERROR_CHARS = 512
_MAX_RETRY_AFTER_SEC = 300.0


@dataclass(frozen=True)
class TelegramSendResult:
    """Outcome of a single Telegram Bot API sendMessage attempt."""

    ok: bool
    permanent_failure: bool
    status_code: int | None
    retry_after_sec: float | None = None
    error: str | None = None


def build_telegram_httpx_timeout(settings: Settings) -> httpx.Timeout:
    """Construct explicit connect/read/write/pool timeouts for Telegram HTTP calls (R03)."""
    return httpx.Timeout(
        connect=settings.telegram_connect_timeout_sec,
        read=settings.telegram_read_timeout_sec,
        write=settings.telegram_write_timeout_sec,
        pool=settings.telegram_pool_timeout_sec,
    )


def _clamp_retry_after(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float, str)):
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return max(0.0, min(val, _MAX_RETRY_AFTER_SEC))


def _extract_retry_after(data: Any, headers: httpx.Headers) -> float | None:
    if isinstance(data, dict):
        params = data.get("parameters")
        if isinstance(params, dict):
            parsed = _clamp_retry_after(params.get("retry_after"))
            if parsed is not None:
                return parsed
    return _clamp_retry_after(headers.get("Retry-After"))


class TelegramClient:
    """Bounded, strictly redacted async Telegram Bot API dispatcher."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._semaphore = semaphore or asyncio.Semaphore(settings.telegram_max_concurrency)

    @property
    def is_configured(self) -> bool:
        """Return True iff both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are non-empty."""
        return bool(
            self._settings.telegram_bot_token.strip()
            and self._settings.telegram_chat_id.strip()
        )

    def redact_text(self, text: str) -> str:
        """Redact bot token, chat ID (even short <6 chars), and generic secrets from text."""
        if not text:
            return text
        bot_token = self._settings.telegram_bot_token.strip()
        chat_id = self._settings.telegram_chat_id.strip()
        extra = tuple(s for s in (bot_token, chat_id) if s)
        redacted = redact_secrets(text, extra)
        for secret in sorted(extra, key=len, reverse=True):
            redacted = redacted.replace(secret, "***REDACTED***")
        return redacted

    async def _post_send_message(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("Telegram HTTP client is not initialized")
        async with self._semaphore:
            return await self._client.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

    async def send_message(self, text: str) -> TelegramSendResult:
        """Send a single Telegram message within bounded concurrency and total deadline."""
        bot_token = self._settings.telegram_bot_token.strip()
        chat_id = self._settings.telegram_chat_id.strip()
        if not bot_token or not chat_id:
            return TelegramSendResult(
                ok=False,
                permanent_failure=True,
                status_code=None,
                error="Telegram bot token or chat ID is not configured",
            )

        base_url = self._settings.telegram_api_base_url.rstrip("/")
        url = f"{base_url}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            resp = await asyncio.wait_for(
                self._post_send_message(url, payload),
                timeout=self._settings.telegram_total_timeout_sec,
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            err = self.redact_text(f"Telegram request timed out: {exc}")[:_MAX_ERROR_CHARS]
            logger.warning("Telegram delivery timeout: %s", err)
            return TelegramSendResult(
                ok=False,
                permanent_failure=False,
                status_code=None,
                error=err,
            )
        except httpx.TransportError as exc:
            err = self.redact_text(f"Telegram transport error: {exc}")[:_MAX_ERROR_CHARS]
            logger.warning("Telegram transport error: %s", err)
            return TelegramSendResult(
                ok=False,
                permanent_failure=False,
                status_code=None,
                error=err,
            )
        except Exception as exc:
            err = self.redact_text(f"Telegram unexpected error: {exc}")[:_MAX_ERROR_CHARS]
            logger.warning("Telegram unexpected error: %s", err)
            return TelegramSendResult(
                ok=False,
                permanent_failure=False,
                status_code=None,
                error=err,
            )

        status_code = resp.status_code
        data: Any = None
        if resp.content:
            try:
                data = json.loads(resp.text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                data = None

        retry_after = _extract_retry_after(data, resp.headers)
        desc = ""
        if isinstance(data, dict):
            raw_desc = data.get("description") or data.get("error") or ""
            desc = str(raw_desc)
        if not desc:
            desc = resp.text if resp.text else f"Telegram HTTP {status_code}"
        safe_desc = self.redact_text(desc)[:_MAX_ERROR_CHARS]

        if status_code == 200:
            if isinstance(data, dict) and data.get("ok") is True:
                return TelegramSendResult(
                    ok=True,
                    permanent_failure=False,
                    status_code=200,
                )
            if isinstance(data, dict) and data.get("ok") is False:
                err_code = data.get("error_code")
                if err_code == 429:
                    logger.warning("Telegram rate limited (ok=false, 429): %s", safe_desc)
                    return TelegramSendResult(
                        ok=False,
                        permanent_failure=False,
                        status_code=429,
                        retry_after_sec=retry_after,
                        error=safe_desc,
                    )
                if isinstance(err_code, int) and 400 <= err_code < 500:
                    logger.warning("Telegram permanent API error (%s): %s", err_code, safe_desc)
                    return TelegramSendResult(
                        ok=False,
                        permanent_failure=True,
                        status_code=err_code,
                        error=safe_desc,
                    )
                eff_code = err_code if isinstance(err_code, int) else 200
                logger.warning("Telegram transient API error (%s): %s", eff_code, safe_desc)
                return TelegramSendResult(
                    ok=False,
                    permanent_failure=False,
                    status_code=eff_code,
                    retry_after_sec=retry_after,
                    error=safe_desc,
                )
            logger.warning("Telegram returned invalid JSON on HTTP 200")
            return TelegramSendResult(
                ok=False,
                permanent_failure=False,
                status_code=200,
                error=self.redact_text("Telegram returned unexpected HTTP 200 response"),
            )

        if status_code == 429:
            logger.warning("Telegram HTTP 429 rate limited: %s", safe_desc)
            return TelegramSendResult(
                ok=False,
                permanent_failure=False,
                status_code=429,
                retry_after_sec=retry_after,
                error=safe_desc,
            )

        if 400 <= status_code < 500:
            logger.warning("Telegram permanent HTTP %s error: %s", status_code, safe_desc)
            return TelegramSendResult(
                ok=False,
                permanent_failure=True,
                status_code=status_code,
                error=safe_desc,
            )

        logger.warning("Telegram transient HTTP %s error: %s", status_code, safe_desc)
        return TelegramSendResult(
            ok=False,
            permanent_failure=False,
            status_code=status_code,
            retry_after_sec=retry_after,
            error=safe_desc,
        )
