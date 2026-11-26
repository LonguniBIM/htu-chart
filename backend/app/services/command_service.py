"""Unified CommandService for domain-coordinate drawings, OCC, idempotency, and Undo/Redo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from app.engine.instruments import StorageDatasetKey, get_instrument_spec, validate_root_symbol
from app.engine.ohlcv import DEFAULT_MIN_BAR_TIMESTAMP
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.sqlite_store import (
    DrawingCommandExecutionResult,
    DrawingRecord,
    SqliteStore,
)

DrawingType = Literal[
    "trendline",
    "ray",
    "hline",
    "fvg_box",
    "order_block",
    "long_position",
    "short_position",
    "measure",
]
DrawingActor = Literal["user", "agent"]
DrawingAction = Literal["create", "update", "delete", "clear", "batch"]
BatchAction = Literal["create", "update", "delete"]

_SQLITE_INT64_MAX = 2**63 - 1
_DRAWING_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_PANE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FORBIDDEN_PIXEL_KEYS = frozenset(
    {
        "x",
        "y",
        "pixel_x",
        "pixel_y",
        "screen_x",
        "screen_y",
        "clientx",
        "clienty",
        "offsetx",
        "offsety",
    }
)
_MAX_SOURCE_BAR_REFS = 64
_MAX_JSON_FIELD_CHARS = 8192
_TWO_ANCHOR_TYPES = frozenset({"trendline", "ray", "fvg_box", "order_block", "measure"})


def _assert_no_pixel_keys(value: Any, path: str = "coordinates") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key_str = str(raw_key)
            if key_str.strip().lower() in _FORBIDDEN_PIXEL_KEYS:
                raise ValueError(
                    f"Forbidden pixel coordinate key {key_str!r} in {path}; "
                    "only domain (time, price) coordinates are allowed"
                )
            _assert_no_pixel_keys(nested, f"{path}.{key_str}")
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            _assert_no_pixel_keys(item, f"{path}[{idx}]")


def _validate_bar_ref_timestamp(value: int, field_name: str = "time") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer Unix timestamp")
    if value < DEFAULT_MIN_BAR_TIMESTAMP or value > _SQLITE_INT64_MAX:
        raise ValueError(
            f"{field_name} ({value}) must be within "
            f"[{DEFAULT_MIN_BAR_TIMESTAMP}, {_SQLITE_INT64_MAX}]"
        )
    return value


def _validate_drawing_id(value: str, field_name: str = "id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned or cleaned in {".", ".."} or not _DRAWING_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"{field_name} must match ^[A-Za-z0-9._-]{{1,80}}$ and not be '.' or '..'"
        )
    return cleaned


def _validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise ValueError("idempotency_key must match ^[A-Za-z0-9._:-]{1,128}$")
    return value


def _validate_json_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    try:
        serialized = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite JSON without NaN/Infinity: {exc}") from exc
    if len(serialized) > _MAX_JSON_FIELD_CHARS:
        raise ValueError(
            f"{field_name} serialized JSON length ({len(serialized)}) exceeds "
            f"{_MAX_JSON_FIELD_CHARS} characters"
        )
    return dict(value)


def normalize_drawing_scope(
    symbol: str,
    contract_id: str | None = None,
    pane_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Validate and normalize (symbol, contract_id, pane_id) drawing scope coordinates."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty CME root symbol")
    norm_symbol = get_instrument_spec(symbol).root_symbol

    norm_contract: str | None = None
    if contract_id is not None:
        if not isinstance(contract_id, str):
            raise ValueError("contract_id must be a string when provided")
        stripped_cid = contract_id.strip()
        if stripped_cid:
            norm_contract = stripped_cid.upper()
            StorageDatasetKey("topstepx", False, norm_contract, "1m")
            validate_root_symbol(norm_contract, norm_symbol)

    norm_pane: str | None = None
    if pane_id is not None:
        if not isinstance(pane_id, str):
            raise ValueError("pane_id must be a string when provided")
        stripped_pid = pane_id.strip()
        if stripped_pid:
            if stripped_pid in {".", ".."} or not _PANE_ID_RE.fullmatch(stripped_pid):
                raise ValueError(
                    f"pane_id {pane_id!r} must match ^[A-Za-z0-9._-]{{1,64}}$"
                )
            norm_pane = stripped_pid

    return norm_symbol, norm_contract, norm_pane


class DrawingAnchor(BaseModel):
    """Domain-coordinate anchor point (time, price) — pixels strictly forbidden."""

    model_config = ConfigDict(extra="forbid")

    role: str
    time: StrictInt
    price: float

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 32:
            raise ValueError("anchor role must be 1..32 non-whitespace characters")
        return cleaned

    @field_validator("time")
    @classmethod
    def _validate_time(cls, value: int) -> int:
        return _validate_bar_ref_timestamp(value, "anchor.time")

    @field_validator("price", mode="before")
    @classmethod
    def _validate_price(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("anchor.price must be a finite positive number")
        try:
            fval = float(value)
        except OverflowError as exc:
            raise ValueError("anchor.price must be a finite positive number") from exc
        if not math.isfinite(fval) or fval <= 0.0:
            raise ValueError("anchor.price must be finite and strictly > 0")
        return fval


class DrawingCoordinates(BaseModel):
    """Domain-only drawing coordinates containing typed (time, price) anchors."""

    model_config = ConfigDict(extra="forbid")

    anchors: list[DrawingAnchor]

    @model_validator(mode="before")
    @classmethod
    def _reject_pixel_keys(cls, data: Any) -> Any:
        _assert_no_pixel_keys(data, "coordinates")
        return data


def validate_type_and_anchors(
    drawing_type: DrawingType,
    coordinates: DrawingCoordinates,
) -> None:
    """Enforce per-type anchor counts, roles, and price/direction invariants."""
    anchors = coordinates.anchors
    roles = [anchor.role for anchor in anchors]

    if drawing_type == "hline":
        if roles != ["level"]:
            raise ValueError("hline requires exactly 1 anchor with role 'level'")
        return

    if drawing_type in _TWO_ANCHOR_TYPES:
        if roles != ["start", "end"]:
            raise ValueError(
                f"{drawing_type} requires exactly 2 anchors with roles ['start', 'end']"
            )
        if drawing_type == "ray" and (
            (anchors[0].time, anchors[0].price) == (anchors[1].time, anchors[1].price)
        ):
            raise ValueError("ray requires distinct start and end (time, price) coordinates")
        return

    if drawing_type == "long_position":
        if roles != ["entry", "target", "stop"]:
            raise ValueError(
                "long_position requires exactly 3 anchors with roles ['entry', 'target', 'stop']"
            )
        entry, target, stop = anchors
        if target.price <= entry.price or stop.price >= entry.price:
            raise ValueError(
                "long_position requires target.price > entry.price and stop.price < entry.price"
            )
        return

    if drawing_type == "short_position":
        if roles != ["entry", "target", "stop"]:
            raise ValueError(
                "short_position requires exactly 3 anchors with roles ['entry', 'target', 'stop']"
            )
        entry, target, stop = anchors
        if target.price >= entry.price or stop.price <= entry.price:
            raise ValueError(
                "short_position requires target.price < entry.price and stop.price > entry.price"
            )
        return

    raise ValueError(f"Unsupported drawing type: {drawing_type!r}")


class DrawingCreatePayload(BaseModel):
    """Validated payload for creating a domain-coordinate drawing."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: DrawingType
    coordinates: DrawingCoordinates
    created_by: DrawingActor | None = None
    source_bar_refs: list[StrictInt] = Field(default_factory=list)
    style: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str | None) -> str | None:
        return _validate_drawing_id(value, "drawing.id") if value is not None else None

    @field_validator("source_bar_refs")
    @classmethod
    def _check_source_bar_refs(cls, refs: list[int]) -> list[int]:
        if len(refs) > _MAX_SOURCE_BAR_REFS:
            raise ValueError(f"source_bar_refs cannot exceed {_MAX_SOURCE_BAR_REFS} items")
        return [_validate_bar_ref_timestamp(ts, "source_bar_refs[]") for ts in refs]

    @field_validator("style")
    @classmethod
    def _check_style(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_dict(value, "style")

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_dict(value, "metadata")

    @model_validator(mode="after")
    def _check_type_anchors(self) -> DrawingCreatePayload:
        validate_type_and_anchors(self.type, self.coordinates)
        return self


class DrawingUpdatePayload(BaseModel):
    """Validated payload for updating an existing drawing."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: DrawingType | None = None
    coordinates: DrawingCoordinates | None = None
    created_by: DrawingActor | None = None
    source_bar_refs: list[StrictInt] | None = None
    style: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str | None) -> str | None:
        return _validate_drawing_id(value, "drawing.id") if value is not None else None

    @field_validator("source_bar_refs")
    @classmethod
    def _check_source_bar_refs(cls, refs: list[int] | None) -> list[int] | None:
        if refs is None:
            return None
        if len(refs) > _MAX_SOURCE_BAR_REFS:
            raise ValueError(f"source_bar_refs cannot exceed {_MAX_SOURCE_BAR_REFS} items")
        return [_validate_bar_ref_timestamp(ts, "source_bar_refs[]") for ts in refs]

    @field_validator("style")
    @classmethod
    def _check_style(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_json_dict(value, "style") if value is not None else None

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_json_dict(value, "metadata") if value is not None else None

    @model_validator(mode="after")
    def _check_non_empty_and_consistent(self) -> DrawingUpdatePayload:
        if (
            self.type is None
            and self.coordinates is None
            and self.source_bar_refs is None
            and self.style is None
            and self.metadata is None
        ):
            raise ValueError(
                "update requires at least one of type, coordinates, source_bar_refs, "
                "style, or metadata"
            )
        if self.type is not None and self.coordinates is not None:
            validate_type_and_anchors(self.type, self.coordinates)
        return self


def _resolve_update_target_id(
    command_drawing_id: str | None,
    payload_drawing_id: str | None,
) -> str:
    if command_drawing_id is not None and payload_drawing_id is not None:
        if command_drawing_id != payload_drawing_id:
            raise ValueError(
                f"Mismatched drawing_id ({command_drawing_id!r}) and "
                f"drawing.id ({payload_drawing_id!r})"
            )
        return command_drawing_id
    resolved = command_drawing_id or payload_drawing_id
    if resolved is None:
        raise ValueError("update requires drawing_id or drawing.id")
    return resolved


class BatchOperation(BaseModel):
    """Single validated create/update/delete operation inside an atomic batch command."""

    model_config = ConfigDict(extra="forbid")

    action: BatchAction
    drawing: dict[str, Any] | None = None
    drawing_id: str | None = None
    created_by: DrawingActor | None = None

    @field_validator("drawing_id")
    @classmethod
    def _check_drawing_id(cls, value: str | None) -> str | None:
        return _validate_drawing_id(value, "drawing_id") if value is not None else None

    @model_validator(mode="after")
    def _validate_operation(self) -> BatchOperation:
        if self.action == "create":
            if self.drawing is None or self.drawing_id is not None:
                raise ValueError("batch create requires drawing and forbids drawing_id")
            try:
                parsed_create = DrawingCreatePayload.model_validate(self.drawing)
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc
            self.drawing = parsed_create.model_dump()
            return self

        if self.action == "update":
            if self.drawing is None:
                raise ValueError("batch update requires drawing")
            try:
                parsed_update = DrawingUpdatePayload.model_validate(self.drawing)
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc
            self.drawing_id = _resolve_update_target_id(self.drawing_id, parsed_update.id)
            self.drawing = {
                k: v for k, v in parsed_update.model_dump().items() if v is not None
            }
            return self

        if self.action == "delete":
            if self.drawing is not None or self.drawing_id is None:
                raise ValueError("batch delete requires drawing_id and forbids drawing")
            return self

        raise ValueError(f"Unsupported batch action: {self.action!r}")


class DrawingCommandRequest(BaseModel):
    """Validated command envelope for POST /api/drawings/{symbol}."""

    model_config = ConfigDict(extra="forbid")

    action: DrawingAction
    expected_revision: StrictInt
    idempotency_key: str | None = None
    contract_id: str | None = None
    pane_id: str | None = None
    created_by: DrawingActor = "user"
    drawing: dict[str, Any] | None = None
    drawing_id: str | None = None
    operations: list[BatchOperation] | None = None

    @field_validator("expected_revision")
    @classmethod
    def _check_expected_revision(cls, value: int) -> int:
        if value < 0 or value > _SQLITE_INT64_MAX:
            raise ValueError(f"expected_revision must be within [0, {_SQLITE_INT64_MAX}]")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _check_idempotency_key(cls, value: str | None) -> str | None:
        return _validate_idempotency_key(value)

    @field_validator("drawing_id")
    @classmethod
    def _check_drawing_id(cls, value: str | None) -> str | None:
        return _validate_drawing_id(value, "drawing_id") if value is not None else None

    @model_validator(mode="after")
    def _validate_action_payload(self) -> DrawingCommandRequest:
        if self.action == "create":
            if self.drawing is None or self.drawing_id is not None or self.operations is not None:
                raise ValueError(
                    "create command requires drawing and forbids drawing_id/operations"
                )
            try:
                parsed_create = DrawingCreatePayload.model_validate(self.drawing)
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc
            self.drawing = parsed_create.model_dump()
            return self

        if self.action == "update":
            if self.drawing is None or self.operations is not None:
                raise ValueError("update command requires drawing and forbids operations")
            try:
                parsed_update = DrawingUpdatePayload.model_validate(self.drawing)
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc
            self.drawing_id = _resolve_update_target_id(self.drawing_id, parsed_update.id)
            self.drawing = {
                k: v for k, v in parsed_update.model_dump().items() if v is not None
            }
            return self

        if self.action == "delete":
            if self.drawing is not None or self.drawing_id is None or self.operations is not None:
                raise ValueError(
                    "delete command requires drawing_id and forbids drawing/operations"
                )
            return self

        if self.action == "clear":
            if (
                self.drawing is not None
                or self.drawing_id is not None
                or self.operations is not None
            ):
                raise ValueError("clear command forbids drawing, drawing_id, and operations")
            return self

        if self.action == "batch":
            if self.drawing is not None or self.drawing_id is not None:
                raise ValueError("batch command forbids top-level drawing and drawing_id")
            if not self.operations or len(self.operations) > 500:
                raise ValueError("batch command requires 1..500 operations")
            return self

        raise ValueError(f"Unsupported command action: {self.action!r}")


class UndoRedoRequest(BaseModel):
    """Validated payload for POST /api/drawings/{symbol}/undo and /redo."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt
    idempotency_key: str | None = None
    contract_id: str | None = None
    pane_id: str | None = None
    actor: DrawingActor = "user"

    @field_validator("expected_revision")
    @classmethod
    def _check_expected_revision(cls, value: int) -> int:
        if value < 0 or value > _SQLITE_INT64_MAX:
            raise ValueError(f"expected_revision must be within [0, {_SQLITE_INT64_MAX}]")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _check_idempotency_key(cls, value: str | None) -> str | None:
        return _validate_idempotency_key(value)


def _compute_request_fingerprint(
    *,
    symbol: str,
    contract_id: str | None,
    pane_id: str | None,
    expected_revision: int,
    command_type: str,
    command_payload: Mapping[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "symbol": symbol,
            "contract_id": contract_id,
            "pane_id": pane_id,
            "expected_revision": expected_revision,
            "command_type": command_type,
            "command_payload": command_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_instrument(symbol: str) -> dict[str, Any]:
    spec = get_instrument_spec(symbol)
    return {
        "root_symbol": spec.root_symbol,
        "name": spec.name,
        "exchange": spec.exchange,
        "tick_size": spec.tick_size,
        "tick_value": spec.tick_value,
        "currency": spec.currency,
    }


def _serialize_drawing(record: DrawingRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "revision": record.revision,
        "type": record.type,
        "symbol": record.symbol,
        "contract_id": record.contract_id,
        "pane_id": record.pane_id,
        "created_by": record.created_by,
        "source_bar_refs": list(record.source_bar_refs),
        "coordinates": record.coordinates,
        "style": record.style,
        "metadata": record.metadata,
        "created_at_utc": record.created_at_utc,
        "updated_at_utc": record.updated_at_utc,
    }


def _merge_and_validate_update(
    drawing_id: str,
    existing: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate merged update fields against full DrawingCreatePayload domain invariants."""
    candidate = {
        "id": drawing_id,
        "type": patch.get("type", existing["type"]),
        "coordinates": patch.get("coordinates", existing["coordinates"]),
        "created_by": existing["created_by"],
        "source_bar_refs": patch.get("source_bar_refs", existing["source_bar_refs"]),
        "style": patch.get("style", existing["style"]),
        "metadata": patch.get("metadata", existing["metadata"]),
    }
    try:
        validated = DrawingCreatePayload.model_validate(candidate)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return validated.model_dump()


class CommandService:
    """Unified command pipeline for user and agent drawing mutations with WS broadcast."""

    def __init__(
        self,
        sqlite_store: SqliteStore,
        *,
        broadcaster: WsBroadcaster | None = None,
    ) -> None:
        self._sqlite_store = sqlite_store
        self._broadcaster = broadcaster
        self._scope_locks: dict[tuple[str, str | None, str | None], asyncio.Lock] = {}
        self._last_emitted_revision: dict[tuple[str, str | None, str | None], int] = {}

    def _scope_lock(
        self,
        symbol: str,
        contract_id: str | None,
        pane_id: str | None,
    ) -> asyncio.Lock:
        key = (symbol, contract_id, pane_id)
        lock = self._scope_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[key] = lock
        return lock

    async def list_scope_drawings(
        self,
        symbol: str,
        *,
        contract_id: str | None = None,
        pane_id: str | None = None,
    ) -> dict[str, Any]:
        """Return persisted drawings, revision metadata, and CME instrument spec for scope."""
        norm_symbol, norm_contract, norm_pane = normalize_drawing_scope(
            symbol, contract_id, pane_id
        )
        snapshot = await self._sqlite_store.list_drawings_scope(
            norm_symbol,
            contract_id=norm_contract,
            pane_id=norm_pane,
        )
        return {
            "symbol": snapshot.symbol,
            "contract_id": snapshot.contract_id,
            "pane_id": snapshot.pane_id,
            "instrument": _serialize_instrument(snapshot.symbol),
            "scope_revision": snapshot.scope_revision,
            "can_undo": snapshot.can_undo,
            "can_redo": snapshot.can_redo,
            "drawings": [_serialize_drawing(record) for record in snapshot.drawings],
        }

    def _emit_if_changed(
        self,
        result: DrawingCommandExecutionResult,
        serialized_drawings: list[dict[str, Any]],
    ) -> None:
        scope_key = (result.symbol, result.contract_id, result.pane_id)
        last_rev = self._last_emitted_revision.get(scope_key, -1)
        if (
            result.changed
            and not result.deduplicated
            and result.scope_revision > last_rev
            and self._broadcaster is not None
        ):
            self._last_emitted_revision[scope_key] = result.scope_revision
            self._broadcaster.publish_drawings_update(
                {
                    "symbol": result.symbol,
                    "contract_id": result.contract_id,
                    "pane_id": result.pane_id,
                    "scope_revision": result.scope_revision,
                    "command_type": result.command_type,
                    "can_undo": result.can_undo,
                    "can_redo": result.can_redo,
                    "drawings": serialized_drawings,
                }
            )

    def _format_command_response(
        self,
        result: DrawingCommandExecutionResult,
    ) -> dict[str, Any]:
        serialized_drawings = [_serialize_drawing(record) for record in result.drawings]
        self._emit_if_changed(result, serialized_drawings)
        return {
            "symbol": result.symbol,
            "contract_id": result.contract_id,
            "pane_id": result.pane_id,
            "instrument": _serialize_instrument(result.symbol),
            "scope_revision": result.scope_revision,
            "can_undo": result.can_undo,
            "can_redo": result.can_redo,
            "deduplicated": result.deduplicated,
            "affected_ids": list(result.affected_ids),
            "drawings": serialized_drawings,
        }

    async def execute_command(
        self,
        symbol: str,
        command: DrawingCommandRequest | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate and execute create/update/delete/clear/batch command atomically."""
        if isinstance(command, DrawingCommandRequest):
            req = command
        else:
            try:
                req = DrawingCommandRequest.model_validate(command)
            except ValidationError as exc:
                raise ValueError(str(exc)) from exc

        norm_symbol, norm_contract, norm_pane = normalize_drawing_scope(
            symbol, req.contract_id, req.pane_id
        )

        # Compute fingerprint BEFORE generating missing drawing IDs so retries match.
        fingerprint_payload: dict[str, Any] = {
            "action": req.action,
            "created_by": req.created_by,
            "drawing": req.drawing,
            "drawing_id": req.drawing_id,
            "operations": (
                [op.model_dump() for op in req.operations]
                if req.operations is not None
                else None
            ),
        }
        fingerprint = _compute_request_fingerprint(
            symbol=norm_symbol,
            contract_id=norm_contract,
            pane_id=norm_pane,
            expected_revision=req.expected_revision,
            command_type=req.action,
            command_payload=fingerprint_payload,
        )

        operations: list[dict[str, Any]] = []
        if req.action == "create":
            if req.drawing is None:
                raise ValueError("create command requires drawing")
            drawing_spec = dict(req.drawing)
            if drawing_spec.get("id") is None:
                drawing_spec["id"] = f"draw-{uuid.uuid4().hex}"
            operations.append({"action": "create", "drawing": drawing_spec})
        elif req.action == "update":
            if req.drawing is None or req.drawing_id is None:
                raise ValueError("update command requires drawing and drawing_id")
            operations.append(
                {
                    "action": "update",
                    "drawing_id": req.drawing_id,
                    "drawing": dict(req.drawing),
                }
            )
        elif req.action == "delete":
            if req.drawing_id is None:
                raise ValueError("delete command requires drawing_id")
            operations.append({"action": "delete", "drawing_id": req.drawing_id})
        elif req.action == "batch":
            if req.operations is None:
                raise ValueError("batch command requires operations")
            for op in req.operations:
                if op.action == "create":
                    if op.drawing is None:
                        raise ValueError("batch create requires drawing")
                    spec = dict(op.drawing)
                    if spec.get("id") is None:
                        spec["id"] = f"draw-{uuid.uuid4().hex}"
                    if spec.get("created_by") is None and op.created_by is not None:
                        spec["created_by"] = op.created_by
                    operations.append({"action": "create", "drawing": spec})
                elif op.action == "update":
                    if op.drawing is None or op.drawing_id is None:
                        raise ValueError("batch update requires drawing and drawing_id")
                    operations.append(
                        {
                            "action": "update",
                            "drawing_id": op.drawing_id,
                            "drawing": dict(op.drawing),
                        }
                    )
                elif op.action == "delete":
                    if op.drawing_id is None:
                        raise ValueError("batch delete requires drawing_id")
                    operations.append({"action": "delete", "drawing_id": op.drawing_id})

        async with self._scope_lock(norm_symbol, norm_contract, norm_pane):
            result = await self._sqlite_store.execute_drawing_command(
                symbol=norm_symbol,
                contract_id=norm_contract,
                pane_id=norm_pane,
                expected_revision=req.expected_revision,
                command_type=req.action,
                actor=req.created_by,
                operations=operations,
                request_fingerprint=fingerprint,
                idempotency_key=req.idempotency_key,
                merge_validator=_merge_and_validate_update,
            )
            return self._format_command_response(result)

    async def undo(
        self,
        symbol: str,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
        contract_id: str | None = None,
        pane_id: str | None = None,
        actor: DrawingActor = "user",
    ) -> dict[str, Any]:
        """Undo the most recent drawing command in the target scope."""
        try:
            req = UndoRedoRequest(
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                contract_id=contract_id,
                pane_id=pane_id,
                actor=actor,
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

        norm_symbol, norm_contract, norm_pane = normalize_drawing_scope(
            symbol, req.contract_id, req.pane_id
        )
        fingerprint = _compute_request_fingerprint(
            symbol=norm_symbol,
            contract_id=norm_contract,
            pane_id=norm_pane,
            expected_revision=req.expected_revision,
            command_type="undo",
            command_payload={"action": "undo", "actor": req.actor},
        )
        async with self._scope_lock(norm_symbol, norm_contract, norm_pane):
            result = await self._sqlite_store.undo_drawing_command(
                symbol=norm_symbol,
                contract_id=norm_contract,
                pane_id=norm_pane,
                expected_revision=req.expected_revision,
                actor=req.actor,
                request_fingerprint=fingerprint,
                idempotency_key=req.idempotency_key,
            )
            return self._format_command_response(result)

    async def redo(
        self,
        symbol: str,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
        contract_id: str | None = None,
        pane_id: str | None = None,
        actor: DrawingActor = "user",
    ) -> dict[str, Any]:
        """Redo the most recently undone drawing command in the target scope."""
        try:
            req = UndoRedoRequest(
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                contract_id=contract_id,
                pane_id=pane_id,
                actor=actor,
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

        norm_symbol, norm_contract, norm_pane = normalize_drawing_scope(
            symbol, req.contract_id, req.pane_id
        )
        fingerprint = _compute_request_fingerprint(
            symbol=norm_symbol,
            contract_id=norm_contract,
            pane_id=norm_pane,
            expected_revision=req.expected_revision,
            command_type="redo",
            command_payload={"action": "redo", "actor": req.actor},
        )
        async with self._scope_lock(norm_symbol, norm_contract, norm_pane):
            result = await self._sqlite_store.redo_drawing_command(
                symbol=norm_symbol,
                contract_id=norm_contract,
                pane_id=norm_pane,
                expected_revision=req.expected_revision,
                actor=req.actor,
                request_fingerprint=fingerprint,
                idempotency_key=req.idempotency_key,
            )
            return self._format_command_response(result)
