"""Read-only market contract resolution routes (F01, F02, R03)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.security import SessionData, redact_secrets, require_session
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


def _get_topstep_auth(request: Request) -> TopstepAuthService:
    return request.app.state.topstep_auth


def _get_rest_client(request: Request) -> TopstepRestClient:
    return request.app.state.rest_client


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
                detail=redact_secrets(str(exc)),
            ) from exc
        except (
            ContractNotFoundError,
            AmbiguousContractError,
            UpstreamTimeoutError,
            UpstreamHTTPError,
        ) as exc:
            results[symbol] = {
                "selected": None,
                "error_type": type(exc).__name__,
                "error": redact_secrets(str(exc)),
            }

    return {"live": effective_live, "contracts": results}
