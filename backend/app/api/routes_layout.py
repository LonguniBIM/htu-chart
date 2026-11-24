"""Authenticated CRUD routes for named multi-pane chart layouts (F01, F06)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.engine.instruments import INSTRUMENTS
from app.engine.session_calendar import normalize_timeframe
from app.security import SessionData, require_csrf, require_session
from app.storage.sqlite_store import LayoutRecord, SqliteStore

router = APIRouter(prefix="/api/layouts", tags=["layouts"])

LAYOUT_TEMPLATE_PANE_COUNTS: dict[str, int] = {
    "1-single": 1,
    "2-cols": 2,
    "2-rows": 2,
    "3-cols": 3,
    "3-rows": 3,
    "3-left-main": 3,
    "3-right-main": 3,
    "3-top-main": 3,
    "3-bottom-main": 3,
    "4-grid-2x2": 4,
    "4-cols": 4,
    "4-rows": 4,
    "4-left-main": 4,
    "4-top-main": 4,
    "5-top2-bot3": 5,
    "5-left-main-4": 5,
    "6-grid-3x2": 6,
    "6-grid-2x3": 6,
    "7-top3-bot4": 7,
    "8-grid-4x2": 8,
    "8-grid-2x4": 8,
}

SUPPORTED_ROOT_SYMBOLS = frozenset(INSTRUMENTS.keys())


class SyncConfigPayload(BaseModel):
    """5-toggle layout synchronization settings."""

    model_config = ConfigDict(extra="forbid")

    symbol: StrictBool = False
    interval: StrictBool = True
    crosshair: StrictBool = True
    time: StrictBool = False
    dateRange: StrictBool = False


class PaneConfigPayload(BaseModel):
    """Single pane configuration within a named layout."""

    model_config = ConfigDict(extra="forbid")

    pane_id: str | None = None
    root_symbol: str
    timeframe: str = "5m"
    live_mode: StrictBool = False
    playback_mode: Literal["live", "replay"] = "live"

    @field_validator("pane_id")
    @classmethod
    def _validate_pane_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("pane_id cannot be blank")
        if len(cleaned) > 64:
            raise ValueError("pane_id cannot exceed 64 characters")
        return cleaned

    @field_validator("root_symbol")
    @classmethod
    def _validate_root_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in SUPPORTED_ROOT_SYMBOLS:
            allowed = sorted(SUPPORTED_ROOT_SYMBOLS)
            raise ValueError(
                f"Unsupported root_symbol {value!r}; expected one of {allowed}"
            )
        return normalized

    @field_validator("timeframe")
    @classmethod
    def _validate_timeframe(cls, value: str) -> str:
        return normalize_timeframe(value)


class _LayoutPayloadBase(BaseModel):
    """Shared validation for layout create and update payloads."""

    model_config = ConfigDict(extra="forbid")

    name: str
    template_id: str
    panes: list[PaneConfigPayload]
    sync_config: SyncConfigPayload = Field(default_factory=SyncConfigPayload)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not (1 <= len(cleaned) <= 80):
            raise ValueError("Layout name must be 1..80 characters after trimming")
        return cleaned

    @field_validator("template_id")
    @classmethod
    def _validate_template_id(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in LAYOUT_TEMPLATE_PANE_COUNTS:
            raise ValueError(f"Unsupported template_id: {value!r}")
        return cleaned

    @model_validator(mode="after")
    def _validate_panes_match_template(self) -> _LayoutPayloadBase:
        expected = LAYOUT_TEMPLATE_PANE_COUNTS[self.template_id]
        if len(self.panes) != expected:
            raise ValueError(
                f"template_id {self.template_id!r} requires {expected} panes, got {len(self.panes)}"
            )
        seen_ids: set[str] = set()
        normalized_panes: list[PaneConfigPayload] = []
        for idx, pane in enumerate(self.panes, start=1):
            pid = pane.pane_id or f"pane-{idx}"
            if pid in seen_ids:
                raise ValueError(f"Duplicate pane_id {pid!r} in layout panes")
            seen_ids.add(pid)
            normalized_panes.append(pane.model_copy(update={"pane_id": pid}))
        self.panes = normalized_panes
        return self


class LayoutCreateRequest(_LayoutPayloadBase):
    """Request payload for creating a named layout."""

    is_default: StrictBool = False


class LayoutUpdateRequest(_LayoutPayloadBase):
    """Request payload for updating an existing named layout."""

    is_default: StrictBool | None = None


def _get_sqlite_store(request: Request) -> SqliteStore:
    return request.app.state.sqlite_store


def _serialize_layout(record: LayoutRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "template_id": record.template_id,
        "panes": record.panes,
        "sync_config": record.sync_config,
        "is_default": record.is_default,
        "created_at_utc": record.created_at_utc,
        "updated_at_utc": record.updated_at_utc,
    }


def _serialized_panes(payload: _LayoutPayloadBase) -> list[dict[str, Any]]:
    return [pane.model_dump() for pane in payload.panes]


@router.get("")
async def list_layouts(
    _session: Annotated[SessionData, Depends(require_session)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
) -> dict[str, Any]:
    """Ensure default '4 Future' layout exists and return all persisted layouts."""
    await sqlite_store.ensure_default_layout()
    records = await sqlite_store.list_layouts()
    return {"layouts": [_serialize_layout(record) for record in records]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_layout(
    body: LayoutCreateRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
) -> dict[str, Any]:
    """Create and persist a named multi-pane layout."""
    record = await sqlite_store.create_layout(
        name=body.name,
        template_id=body.template_id,
        panes=_serialized_panes(body),
        sync_config=body.sync_config.model_dump(),
        is_default=body.is_default,
    )
    return {"layout": _serialize_layout(record)}


@router.put("/{layout_id}")
async def update_layout(
    layout_id: str,
    body: LayoutUpdateRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
) -> dict[str, Any]:
    """Update an existing named multi-pane layout."""
    record = await sqlite_store.update_layout(
        layout_id,
        name=body.name,
        template_id=body.template_id,
        panes=_serialized_panes(body),
        sync_config=body.sync_config.model_dump(),
        is_default=body.is_default,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Layout {layout_id!r} not found",
        )
    return {"layout": _serialize_layout(record)}


@router.delete("/{layout_id}")
async def delete_layout(
    layout_id: str,
    _session: Annotated[SessionData, Depends(require_csrf)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
) -> dict[str, Any]:
    """Delete a persisted layout unless it is the final remaining layout."""
    try:
        deleted = await sqlite_store.delete_layout(layout_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Layout {layout_id!r} not found",
        )
    return {"deleted": True, "id": layout_id.strip()}
