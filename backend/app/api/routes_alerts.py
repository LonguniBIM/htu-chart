"""Authenticated alert scanner, outbox history, and Telegram test routes (F01, F07)."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.config import Settings
from app.engine.alert_engine import AlertEngine
from app.notify.telegram import (
    TelegramAlertDispatcher,
    TelegramConfigError,
    TelegramUpstreamError,
)
from app.security import SessionData, get_settings, redact_secrets, require_csrf, require_session
from app.storage.sqlite_store import AlertOutboxRecord, SqliteStore

router = APIRouter(prefix="/api", tags=["alerts"])


class ScannerToggleRequest(BaseModel):
    """Toggle payload for enabling or disabling the live alert scanner."""

    enabled: bool


def _get_alert_engine(request: Request) -> AlertEngine:
    return request.app.state.alert_engine


def _get_alert_dispatcher(request: Request) -> TelegramAlertDispatcher:
    return request.app.state.alert_dispatcher


def _get_sqlite_store(request: Request) -> SqliteStore:
    return request.app.state.sqlite_store


def _redact_alert_secrets(text: str | None, settings: Settings) -> str | None:
    if text is None:
        return None
    secrets = tuple(
        s
        for s in (
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.topstepx_api_key,
            settings.app_password_hash,
            settings.app_session_secret,
        )
        if s
    )
    return redact_secrets(text, secrets)


def _serialize_alert(record: AlertOutboxRecord, settings: Settings) -> dict[str, Any]:
    parsed_payload: dict[str, Any] | None = None
    try:
        raw_obj = json.loads(record.payload_json)
        if isinstance(raw_obj, dict):
            parsed_payload = raw_obj
    except json.JSONDecodeError:
        parsed_payload = None

    return {
        "id": record.id,
        "strategy_id": record.strategy_id,
        "strategy_version": record.strategy_version,
        "live_mode": record.live_mode,
        "contract_id": record.contract_id,
        "timeframe": record.timeframe,
        "bar_time": record.bar_time,
        "direction": record.direction,
        "signal_tag": record.signal_tag,
        "status": record.status,
        "payload": parsed_payload,
        "created_at_utc": record.created_at_utc,
        "expires_at_utc": record.expires_at_utc,
        "next_attempt_at_utc": record.next_attempt_at_utc,
        "attempts": record.attempts,
        "last_error": _redact_alert_secrets(record.last_error, settings),
        "updated_at_utc": record.updated_at_utc,
    }


@router.get("/alerts")
async def get_alerts(
    _session: Annotated[SessionData, Depends(require_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    alert_engine: Annotated[AlertEngine, Depends(_get_alert_engine)],
    dispatcher: Annotated[TelegramAlertDispatcher, Depends(_get_alert_dispatcher)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    """Return scanner state, redacted Telegram configuration flag, summary, and alert rows."""
    try:
        records = await sqlite_store.list_alerts(limit=limit, status_filter=status_filter)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    summary = await sqlite_store.get_alert_summary()
    return {
        "scanner_enabled": alert_engine.enabled,
        "telegram_configured": dispatcher.is_configured,
        "summary": summary,
        "alerts": [_serialize_alert(rec, settings) for rec in records],
    }


@router.post("/alerts/scanner")
async def set_scanner_enabled(
    body: ScannerToggleRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    alert_engine: Annotated[AlertEngine, Depends(_get_alert_engine)],
) -> dict[str, Any]:
    """Enable or disable live alert scanning."""
    updated = alert_engine.set_enabled(body.enabled)
    return {"scanner_enabled": updated}


@router.post("/alerts/test")
async def send_test_alert(
    _session: Annotated[SessionData, Depends(require_csrf)],
    settings: Annotated[Settings, Depends(get_settings)],
    dispatcher: Annotated[TelegramAlertDispatcher, Depends(_get_alert_dispatcher)],
) -> dict[str, Any]:
    """Send an immediate test notification to the configured Telegram chat."""
    try:
        return await dispatcher.send_test_message()
    except TelegramConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_redact_alert_secrets(str(exc), settings),
        ) from exc
    except TelegramUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_redact_alert_secrets(str(exc), settings),
        ) from exc
