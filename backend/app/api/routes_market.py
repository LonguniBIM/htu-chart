"""Read-only market contract resolution and historical bar routes (F01, F02, F04, R03, R04, R05)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from app.engine.ohlcv import validate_query_timestamp
from app.engine.session_calendar import normalize_timeframe, timeframe_from_topstep_unit
from app.security import SessionData, redact_secrets, require_session, validate_ws_handshake
from app.services.history_service import HistoryBackfillService
from app.services.live_feed_service import LiveFeedService
from app.services.ws_broadcaster import WsBroadcaster
from app.topstep.auth import TopstepAuthService
from app.topstep.errors import (
    AmbiguousContractError,
    ContractNotFoundError,
    KeyRotationRequiredError,
    TopstepAuthError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
)
from app.topstep.rest_client import TopstepRestClient, resolve_contract

router = APIRouter(prefix="/api", tags=["market"])
ws_router = APIRouter(tags=["market-ws"])


def _get_topstep_auth(request: Request) -> TopstepAuthService:
    return request.app.state.topstep_auth


def _get_rest_client(request: Request) -> TopstepRestClient:
    return request.app.state.rest_client


def _get_history_service(request: Request) -> HistoryBackfillService:
    return request.app.state.history_service


def _utc_now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def _parse_query_timestamp(raw: str | None, field_name: str) -> int | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty")
    if cleaned.lstrip("+-").isdigit():
        return validate_query_timestamp(int(cleaned), field_name)
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} ISO string must include timezone offset")
    return validate_query_timestamp(int(dt.timestamp()), field_name)


@router.get("/contracts")
async def get_contracts(
    _session: Annotated[SessionData, Depends(require_session)],
    topstep_auth: Annotated[TopstepAuthService, Depends(_get_topstep_auth)],
    rest_client: Annotated[TopstepRestClient, Depends(_get_rest_client)],
    symbols: Annotated[str, Query()] = "MES,MNQ,MYM,MGC",
    live: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    """Resolve active contracts strictly for requested root symbols."""
    if not topstep_auth.is_connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TopstepX is not connected",
        )

    effective_live = topstep_auth.live if live is None else bool(live)
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    results: dict[str, dict[str, Any]] = {}

    with topstep_auth.redaction_scope() as redaction_scope:
        for symbol in symbol_list:
            try:
                raw_contracts = await rest_client.search_contracts(symbol, live=effective_live)
                selected = resolve_contract(symbol, raw_contracts)
                results[symbol] = {
                    "selected": selected,
                    "error_type": None,
                    "error": None,
                }
            except (KeyRotationRequiredError, TopstepAuthError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=redact_secrets(str(exc), redaction_scope.secrets()),
                ) from exc
            except (
                ContractNotFoundError,
                AmbiguousContractError,
                UpstreamTimeoutError,
                UpstreamHTTPError,
            ) as exc:
                active_secrets = redaction_scope.secrets()
                if not topstep_auth.is_connected:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=redact_secrets(str(exc), active_secrets),
                    ) from exc
                results[symbol] = {
                    "selected": None,
                    "error_type": type(exc).__name__,
                    "error": redact_secrets(str(exc), active_secrets),
                }

    return {"live": effective_live, "contracts": results}


@router.get("/bars")
async def get_bars(
    _session: Annotated[SessionData, Depends(require_session)],
    topstep_auth: Annotated[TopstepAuthService, Depends(_get_topstep_auth)],
    history_service: Annotated[HistoryBackfillService, Depends(_get_history_service)],
    contract_id: Annotated[str | None, Query()] = None,
    contract_id_camel: Annotated[str | None, Query(alias="contractId")] = None,
    timeframe: Annotated[str | None, Query()] = None,
    unit: Annotated[int | None, Query()] = None,
    unit_number: Annotated[int | None, Query()] = None,
    unit_number_camel: Annotated[int | None, Query(alias="unitNumber")] = None,
    start_time: Annotated[str | None, Query()] = None,
    start_time_camel: Annotated[str | None, Query(alias="startTime")] = None,
    end_time: Annotated[str | None, Query()] = None,
    end_time_camel: Annotated[str | None, Query(alias="endTime")] = None,
    days: Annotated[int, Query(ge=1, le=365)] = 14,
    limit: Annotated[int | None, Query(ge=1, le=50000)] = None,
    live: Annotated[bool | None, Query()] = None,
    include_partial: Annotated[bool | None, Query()] = None,
    include_partial_camel: Annotated[bool | None, Query(alias="includePartial")] = None,
    resample_from: Annotated[str | None, Query()] = None,
    resample_from_camel: Annotated[str | None, Query(alias="resampleFrom")] = None,
    root_symbol: Annotated[str | None, Query()] = None,
    root_symbol_camel: Annotated[str | None, Query(alias="rootSymbol")] = None,
) -> dict[str, Any]:
    """Fetch cached/backfilled OHLCV bars with strict validation and coverage tracking."""
    eff_contract = contract_id if contract_id is not None else contract_id_camel
    eff_unit_number = unit_number if unit_number is not None else unit_number_camel
    eff_start_raw = start_time if start_time is not None else start_time_camel
    eff_end_raw = end_time if end_time is not None else end_time_camel
    eff_include_partial = (
        include_partial
        if include_partial is not None
        else (include_partial_camel if include_partial_camel is not None else False)
    )
    eff_resample_from = resample_from if resample_from is not None else resample_from_camel
    eff_root_symbol = root_symbol if root_symbol is not None else root_symbol_camel

    if not eff_contract or not eff_contract.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="contract_id is required",
        )

    effective_live = topstep_auth.live if live is None else bool(live)

    with topstep_auth.redaction_scope() as redaction_scope:
        try:
            if timeframe is not None:
                eff_timeframe = normalize_timeframe(timeframe)
            elif unit is not None or eff_unit_number is not None:
                if unit is None or eff_unit_number is None:
                    raise ValueError(
                        "Both unit and unitNumber are required when timeframe is omitted"
                    )
                eff_timeframe = timeframe_from_topstep_unit(unit, eff_unit_number)
            else:
                eff_timeframe = "1m"

            now_ts = _utc_now_ts()
            parsed_end = _parse_query_timestamp(eff_end_raw, "end_time")
            eff_end = parsed_end if parsed_end is not None else now_ts
            parsed_start = _parse_query_timestamp(eff_start_raw, "start_time")
            eff_start = parsed_start if parsed_start is not None else eff_end - (days * 86400)

            result = await history_service.sync_and_read_bars(
                contract_id=eff_contract,
                live=effective_live,
                timeframe=eff_timeframe,
                start_ts=eff_start,
                end_ts=eff_end,
                include_partial=bool(eff_include_partial),
                limit=limit,
                resample_from=eff_resample_from,
                root_symbol=eff_root_symbol,
                now_utc=now_ts,
                allow_upstream=topstep_auth.is_connected,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=redact_secrets(str(exc), redaction_scope.secrets()),
            ) from exc
        except (KeyRotationRequiredError, TopstepAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=redact_secrets(str(exc), redaction_scope.secrets()),
            ) from exc
        except UpstreamTimeoutError as exc:
            code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if not topstep_auth.is_connected
                else status.HTTP_504_GATEWAY_TIMEOUT
            )
            raise HTTPException(
                status_code=code,
                detail=redact_secrets(str(exc), redaction_scope.secrets()),
            ) from exc
        except UpstreamHTTPError as exc:
            code = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if not topstep_auth.is_connected
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(
                status_code=code,
                detail=redact_secrets(str(exc), redaction_scope.secrets()),
            ) from exc

    return {
        "provider": "topstepx",
        "live": effective_live,
        "contract_id": eff_contract,
        "root_symbol": result.root_symbol,
        "timeframe": result.timeframe,
        "start_time": eff_start,
        "end_time": eff_end,
        "count": len(result.bars),
        "include_partial": bool(eff_include_partial),
        "quality": {
            "is_complete": result.is_complete,
            "truncated": result.truncated,
            "provider_gap": result.provider_gap,
            "quarantined_count": result.quarantined_count,
        },
        "bars": [
            {
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "is_closed": b.is_closed,
            }
            for b in result.bars
        ],
    }


@ws_router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket) -> None:
    """Authenticated, Origin-validated local WebSocket stream for live OHLCV & feed status."""
    settings = websocket.app.state.settings
    session_manager = websocket.app.state.session_manager
    session = await validate_ws_handshake(websocket, settings, session_manager)
    if session is None:
        return

    await websocket.accept()
    broadcaster: WsBroadcaster = websocket.app.state.ws_broadcaster
    live_feed: LiveFeedService = websocket.app.state.live_feed_service
    ws_session = broadcaster.register(websocket)
    broadcaster.send_to_client(
        ws_session.client_id,
        {"type": "feed_status", **live_feed.get_status_snapshot()},
    )

    try:
        while not ws_session.closed_event.is_set():
            recv_task = asyncio.create_task(websocket.receive_json())
            closed_task = asyncio.create_task(ws_session.closed_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {recv_task, closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                for task in (recv_task, closed_task):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                raise

            for pending_task in pending:
                pending_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending_task

            if closed_task in done and ws_session.closed_event.is_set():
                break

            try:
                msg = recv_task.result()
            except (WebSocketDisconnect, RuntimeError):
                break
            except ValueError as exc:
                broadcaster.send_to_client(
                    ws_session.client_id,
                    {"type": "error", "detail": redact_secrets(str(exc))},
                )
                continue

            if session_manager.get_session(session.session_id) is None:
                await broadcaster.unregister(
                    ws_session.client_id,
                    close_transport=True,
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Session expired",
                )
                break

            if not isinstance(msg, dict):
                broadcaster.send_to_client(
                    ws_session.client_id,
                    {"type": "error", "detail": "WebSocket payload must be a JSON object"},
                )
                continue

            op = str(msg.get("type") or msg.get("action") or "").strip().lower()
            if op == "ping":
                broadcaster.send_to_client(ws_session.client_id, {"type": "pong"})
            elif op == "subscribe":
                raw_cid = str(msg.get("contract_id") or msg.get("contractId") or "")
                raw_tf = str(msg.get("timeframe") or "1m")
                try:
                    cid, tf = broadcaster.subscribe(ws_session.client_id, raw_cid, raw_tf)
                    broadcaster.send_to_client(
                        ws_session.client_id,
                        {"type": "subscribed", "contract_id": cid, "timeframe": tf},
                    )
                except ValueError as exc:
                    broadcaster.send_to_client(
                        ws_session.client_id,
                        {"type": "error", "detail": redact_secrets(str(exc))},
                    )
            elif op == "unsubscribe":
                raw_cid = str(msg.get("contract_id") or msg.get("contractId") or "")
                raw_tf = str(msg.get("timeframe") or "1m")
                try:
                    broadcaster.unsubscribe(ws_session.client_id, raw_cid, raw_tf)
                    broadcaster.send_to_client(
                        ws_session.client_id,
                        {
                            "type": "unsubscribed",
                            "contract_id": raw_cid.strip().upper(),
                            "timeframe": normalize_timeframe(raw_tf),
                        },
                    )
                except ValueError as exc:
                    broadcaster.send_to_client(
                        ws_session.client_id,
                        {"type": "error", "detail": redact_secrets(str(exc))},
                    )
            else:
                broadcaster.send_to_client(
                    ws_session.client_id,
                    {"type": "error", "detail": f"Unsupported message type: {op!r}"},
                )
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unregister(ws_session.client_id)
