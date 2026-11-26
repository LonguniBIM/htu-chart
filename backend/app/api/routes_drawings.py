"""Authenticated routes for domain-coordinate drawings and unified CommandService (F01)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.security import SessionData, require_csrf, require_session
from app.services.command_service import (
    CommandService,
    DrawingCommandRequest,
    UndoRedoRequest,
)
from app.storage.sqlite_store import (
    DrawingConflictError,
    DrawingNotFoundError,
    DrawingStoreError,
    IdempotencyConflictError,
    NothingToRedoError,
    NothingToUndoError,
    RevisionConflictError,
    ScopeLimitError,
)

router = APIRouter(prefix="/api/drawings", tags=["drawings"])


def _get_command_service(request: Request) -> CommandService:
    return request.app.state.command_service


def _translate_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RevisionConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "current_revision": exc.current_revision,
                "expected_revision": exc.expected_revision,
            },
        )
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_conflict",
                "message": str(exc),
            },
        )
    if isinstance(exc, NothingToUndoError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "nothing_to_undo",
                "message": str(exc),
            },
        )
    if isinstance(exc, NothingToRedoError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "nothing_to_redo",
                "message": str(exc),
            },
        )
    if isinstance(exc, DrawingConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "drawing_conflict",
                "message": str(exc),
                "drawing_id": exc.drawing_id,
            },
        )
    if isinstance(exc, ScopeLimitError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "scope_limit_exceeded",
                "message": str(exc),
                "limit": exc.limit,
            },
        )
    if isinstance(exc, DrawingNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "drawing_not_found",
                "message": str(exc),
                "drawing_id": exc.drawing_id,
            },
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected drawing command failure",
    )


@router.get("/{symbol}")
async def list_drawings(
    symbol: str,
    _session: Annotated[SessionData, Depends(require_session)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
    contract_id: Annotated[str | None, Query()] = None,
    pane_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """List persisted domain-coordinate drawings and scope revision metadata."""
    try:
        return await command_service.list_scope_drawings(
            symbol,
            contract_id=contract_id,
            pane_id=pane_id,
        )
    except ValueError as exc:
        raise _translate_domain_error(exc) from exc


@router.post("/{symbol}")
async def execute_drawing_command(
    symbol: str,
    body: DrawingCommandRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
) -> dict[str, Any]:
    """Execute a create/update/delete/clear/batch drawing command atomically."""
    try:
        return await command_service.execute_command(symbol, body)
    except (ValueError, DrawingStoreError) as exc:
        raise _translate_domain_error(exc) from exc


@router.post("/{symbol}/undo")
async def undo_drawing_command(
    symbol: str,
    body: UndoRedoRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
) -> dict[str, Any]:
    """Undo the most recent drawing command in the target scope."""
    try:
        return await command_service.undo(
            symbol,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            contract_id=body.contract_id,
            pane_id=body.pane_id,
            actor=body.actor,
        )
    except (ValueError, DrawingStoreError) as exc:
        raise _translate_domain_error(exc) from exc


@router.post("/{symbol}/redo")
async def redo_drawing_command(
    symbol: str,
    body: UndoRedoRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
) -> dict[str, Any]:
    """Redo the most recently undone drawing command in the target scope."""
    try:
        return await command_service.redo(
            symbol,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            contract_id=body.contract_id,
            pane_id=body.pane_id,
            actor=body.actor,
        )
    except (ValueError, DrawingStoreError) as exc:
        raise _translate_domain_error(exc) from exc


@router.delete("/{symbol}")
async def clear_drawings_scope(
    symbol: str,
    _session: Annotated[SessionData, Depends(require_csrf)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
    expected_revision: Annotated[int, Query(ge=0)],
    idempotency_key: Annotated[str | None, Query()] = None,
    contract_id: Annotated[str | None, Query()] = None,
    pane_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Clear all drawings in the target scope through CommandService."""
    try:
        req = DrawingCommandRequest(
            action="clear",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            contract_id=contract_id,
            pane_id=pane_id,
        )
        return await command_service.execute_command(symbol, req)
    except (ValueError, DrawingStoreError) as exc:
        raise _translate_domain_error(exc) from exc


@router.delete("/{symbol}/{drawing_id}")
async def delete_single_drawing(
    symbol: str,
    drawing_id: str,
    _session: Annotated[SessionData, Depends(require_csrf)],
    command_service: Annotated[CommandService, Depends(_get_command_service)],
    expected_revision: Annotated[int, Query(ge=0)],
    idempotency_key: Annotated[str | None, Query()] = None,
    contract_id: Annotated[str | None, Query()] = None,
    pane_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Delete a single drawing by ID through CommandService."""
    try:
        req = DrawingCommandRequest(
            action="delete",
            expected_revision=expected_revision,
            drawing_id=drawing_id,
            idempotency_key=idempotency_key,
            contract_id=contract_id,
            pane_id=pane_id,
        )
        return await command_service.execute_command(symbol, req)
    except (ValueError, DrawingStoreError) as exc:
        raise _translate_domain_error(exc) from exc
