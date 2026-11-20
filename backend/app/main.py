"""FastAPI entrypoint, non-blocking lifespan, and security wiring (F01, F02, R03)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import routes_market, routes_status
from app.config import Settings, load_settings
from app.security import (
    HostOriginMiddleware,
    LoginGuard,
    SessionManager,
    install_global_log_redaction,
    redact_secrets,
)
from app.services.history_service import HistoryBackfillService
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService, build_httpx_timeout
from app.topstep.errors import TopstepError
from app.topstep.rest_client import TopstepRestClient

logger = logging.getLogger("topstep_chart")


def _install_redaction_filter(settings: Settings) -> None:
    extra = (settings.topstepx_api_key, settings.app_password_hash, settings.app_session_secret)
    install_global_log_redaction(extra_secrets=extra)


async def _background_auto_connect(auth_service: TopstepAuthService, settings: Settings) -> None:
    try:
        await auth_service.login(
            username=settings.topstepx_username,
            api_key=settings.topstepx_api_key,
            live=settings.topstepx_live,
        )
        logger.info("TopstepX background auto-connect succeeded (live=%s)", settings.topstepx_live)
    except asyncio.CancelledError:
        raise
    except TopstepError as exc:
        logger.warning("TopstepX background auto-connect failed: %s", redact_secrets(str(exc)))
    except Exception as exc:
        logger.warning(
            "TopstepX background auto-connect unexpected error: %s",
            redact_secrets(str(exc)),
        )


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings or load_settings()
    _install_redaction_filter(resolved_settings)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=build_httpx_timeout(resolved_settings))
        upstream_semaphore = asyncio.Semaphore(resolved_settings.upstream_max_concurrency)
        auth_service = TopstepAuthService(
            resolved_settings,
            client,
            semaphore=upstream_semaphore,
        )
        rest_client = TopstepRestClient(
            resolved_settings,
            auth_service,
            client,
            semaphore=upstream_semaphore,
        )
        sqlite_store = SqliteStore(resolved_settings.data_dir / "metadata.db")
        sqlite_store.initialize()
        parquet_store = ParquetStore(
            resolved_settings.data_dir,
            sqlite_store=sqlite_store,
        )
        history_service = HistoryBackfillService(
            resolved_settings,
            rest_client,
            parquet_store,
            sqlite_store,
        )
        session_manager = SessionManager(
            password_hash=resolved_settings.app_password_hash,
            ttl_seconds=resolved_settings.session_ttl_seconds,
        )
        login_guard = LoginGuard(
            max_attempts=resolved_settings.login_rate_limit_attempts,
            window_seconds=resolved_settings.login_rate_limit_window_sec,
            max_concurrency=resolved_settings.login_max_concurrency,
        )

        app_instance.state.settings = resolved_settings
        app_instance.state.http_client = client
        app_instance.state.upstream_semaphore = upstream_semaphore
        app_instance.state.topstep_auth = auth_service
        app_instance.state.rest_client = rest_client
        app_instance.state.sqlite_store = sqlite_store
        app_instance.state.parquet_store = parquet_store
        app_instance.state.history_service = history_service
        app_instance.state.session_manager = session_manager
        app_instance.state.login_guard = login_guard

        auth_task: asyncio.Task[None] | None = None
        has_creds = bool(
            resolved_settings.topstepx_username.strip()
            and resolved_settings.topstepx_api_key.strip()
        )
        if has_creds:
            auth_task = asyncio.create_task(
                _background_auto_connect(auth_service, resolved_settings)
            )
        app_instance.state.auto_connect_task = auth_task

        try:
            yield
        finally:
            if auth_task is not None and not auth_task.done():
                auth_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await auth_task
            sqlite_store.close()
            if owns_client:
                await client.aclose()

    application = FastAPI(
        title="TopstepX Futures Terminal API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.add_middleware(HostOriginMiddleware, settings=resolved_settings)

    @application.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        extra = (
            resolved_settings.topstepx_api_key,
            resolved_settings.app_password_hash,
            resolved_settings.app_session_secret,
        )
        sanitized: list[dict[str, Any]] = []
        for err in exc.errors():
            sanitized.append(
                {
                    "type": err.get("type"),
                    "loc": err.get("loc"),
                    "msg": redact_secrets(str(err.get("msg", "Validation error")), extra),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": sanitized},
        )

    @application.exception_handler(Exception)
    async def _redacted_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled server error: %s", redact_secrets(str(exc)))
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    application.include_router(routes_status.router)
    application.include_router(routes_market.router)
    return application


app = create_app()


if __name__ == "__main__":
    cfg = load_settings()
    uvicorn.run(
        "app.main:app",
        host=cfg.app_bind_host,
        port=cfg.app_port,
        workers=1,
    )
