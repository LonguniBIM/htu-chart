"""Authenticated alert scanner, outbox history, and Telegram test routes (F01, F07)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.security import SessionData, require_csrf, require_session
from app.services.alert_service import AlertService, TelegramNotConfiguredError
from app.storage.sqlite_store import AlertOutboxRecord, AlertStatus

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class ScannerToggleRequest(BaseModel):
    """Toggle payload for live strategy alert scanner."""

    enabled: bool


def _get_alert_service(request: Request) -> AlertService:
    return request.app.state.alert_service


def _serialize_outbox_record(record: AlertOutboxRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "strategy_id": record.strategy_id,
        "strategy_version": record.strategy_version,
        "live_mode": record.live_mode,
        "contract_id": record.contract_id,
        "timeframe": record.timeframe,
        "bar_time": record.bar_time,
        "direction": record.direction,
        "alert_kind": record.alert_kind,
        "payload_text": record.payload_text,
        "status": record.status,
        "attempt_count": record.attempt_count,
        "max_attempts": record.max_attempts,
        "created_at_utc": record.created_at_utc,
        "expires_at_utc": record.expires_at_utc,
        "next_attempt_at_utc": record.next_attempt_at_utc,
        "delivered_at_utc": record.delivered_at_utc,
        "updated_at_utc": record.updated_at_utc,
        "last_error": record.last_error,
    }


@router.get("/status")
async def get_alerts_status(
    _session: Annotated[SessionData, Depends(require_session)],
    alert_service: Annotated[AlertService, Depends(_get_alert_service)],
) -> dict[str, Any]:
    """Return strictly redacted Telegram configuration, scanner, and outbox summary."""
    return await alert_service.get_status_snapshot()


@router.get("/history")
async def get_alerts_history(
    _session: Annotated[SessionData, Depends(require_session)],
    alert_service: Annotated[AlertService, Depends(_get_alert_service)],
    alert_status: Annotated[AlertStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List recent SQLite alert_outbox entries with optional status filtering."""
    records = await alert_service.list_history(status=alert_status, limit=limit)
    return {
        "items": [_serialize_outbox_record(record) for record in records],
        "count": len(records),
    }


@router.post("/scanner")
async def set_alerts_scanner(
    body: ScannerToggleRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    alert_service: Annotated[AlertService, Depends(_get_alert_service)],
) -> dict[str, Any]:
    """Enable or disable the live strategy alert scanner."""
    alert_service.set_scanner_enabled(body.enabled)
    return await alert_service.get_status_snapshot()


@router.post("/test")
async def trigger_test_alert(
    _session: Annotated[SessionData, Depends(require_csrf)],
    alert_service: Annotated[AlertService, Depends(_get_alert_service)],
) -> dict[str, Any]:
    """Enqueue and dispatch an immediate manual Telegram connectivity test alert."""
    try:
        record = await alert_service.send_test_alert()
    except TelegramNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return {
        "ok": record.status == "DELIVERED",
        "alert": _serialize_outbox_record(record),
    }
