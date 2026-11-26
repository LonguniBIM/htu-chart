"""Authenticated backtest strategy, submission, polling, and cancellation routes (F01, R02)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator

from app.engine.strategies import list_strategies
from app.security import SessionData, require_csrf, require_session
from app.services.backtest_service import (
    BacktestDisabledError,
    BacktestJobNotCancellableError,
    BacktestJobNotFoundError,
    BacktestJobStatus,
    BacktestQueueFullError,
    BacktestService,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestSubmitRequest(BaseModel):
    """Validated request body for POST /api/backtest."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    root_symbol: str | None = None
    timeframe: str = "5m"
    resample_from: str | None = None
    live_mode: StrictBool = False
    start_time: StrictInt
    end_time: StrictInt
    strategy_id: str = "ict_fvg_sweep"
    params: dict[str, Any] | None = None
    max_bars: StrictInt | None = None

    @field_validator("contract_id", "timeframe", "strategy_id")
    @classmethod
    def _validate_non_empty_str(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty")
        return cleaned


def _get_backtest_service(request: Request) -> BacktestService:
    return request.app.state.backtest_service


@router.get("/strategies")
async def get_backtest_strategies(
    _session: Annotated[SessionData, Depends(require_session)],
) -> dict[str, Any]:
    """List statically registered Python backtest strategies and parameter schemas."""
    items = list_strategies()
    return {"strategies": items, "count": len(items)}


@router.get("")
async def list_backtest_jobs(
    _session: Annotated[SessionData, Depends(require_session)],
    backtest_service: Annotated[BacktestService, Depends(_get_backtest_service)],
    job_status: Annotated[BacktestJobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List recent backtest jobs and bounded queue telemetry."""
    jobs = backtest_service.list_jobs(status=job_status, limit=limit)
    snapshot = backtest_service.get_status_snapshot()
    return {
        "items": [job.to_summary_dict() for job in jobs],
        "count": len(jobs),
        **snapshot,
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_backtest_job(
    body: BacktestSubmitRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    backtest_service: Annotated[BacktestService, Depends(_get_backtest_service)],
) -> dict[str, Any]:
    """Validate parameters and enqueue an isolated R02 backtest job."""
    try:
        job = backtest_service.submit_job(
            contract_id=body.contract_id,
            root_symbol=body.root_symbol,
            timeframe=body.timeframe,
            resample_from=body.resample_from,
            live_mode=body.live_mode,
            start_time=body.start_time,
            end_time=body.end_time,
            strategy_id=body.strategy_id,
            params=body.params,
            max_bars=body.max_bars,
        )
    except BacktestDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except BacktestQueueFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return job.to_summary_dict()


@router.get("/{job_id}")
async def get_backtest_job(
    job_id: str,
    _session: Annotated[SessionData, Depends(require_session)],
    backtest_service: Annotated[BacktestService, Depends(_get_backtest_service)],
) -> dict[str, Any]:
    """Fetch status and full result payload for a single backtest job."""
    try:
        job = backtest_service.get_job(job_id)
    except BacktestJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return job.to_detail_dict()


@router.delete("/{job_id}")
async def cancel_backtest_job(
    job_id: str,
    _session: Annotated[SessionData, Depends(require_csrf)],
    backtest_service: Annotated[BacktestService, Depends(_get_backtest_service)],
) -> dict[str, Any]:
    """Cancel a QUEUED or RUNNING backtest job (terminating worker & respawning pool)."""
    try:
        job = await backtest_service.cancel_job(job_id)
    except BacktestJobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except BacktestJobNotCancellableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return job.to_summary_dict()
