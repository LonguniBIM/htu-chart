"""FastAPI entrypoint, non-blocking lifespan, and security wiring (F01, F02, R03)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    routes_alerts,
    routes_backtest,
    routes_drawings,
    routes_layout,
    routes_market,
    routes_status,
)
from app.config import Settings, load_settings
from app.engine.alert_engine import AlertEngine
from app.engine.bar_aggregator import BarAggregator
from app.notify.telegram import TelegramClient, build_telegram_httpx_timeout
from app.security import (
    HostOriginMiddleware,
    LoginGuard,
    SessionManager,
    install_global_log_redaction,
    redact_secrets,
)
from app.services.alert_service import AlertService
from app.services.backtest_service import BacktestService
from app.services.command_service import CommandService
from app.services.history_service import HistoryBackfillService
from app.services.live_feed_service import LiveFeedService
from app.services.ws_broadcaster import WsBroadcaster
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService, build_httpx_timeout
from app.topstep.errors import TopstepError
from app.topstep.rest_client import TopstepRestClient
from app.topstep.signalr_hub import SignalRMarketHub, WsConnectFactory

logger = logging.getLogger("topstep_chart")


def _install_redaction_filter(settings: Settings) -> None:
    extra = (
        settings.topstepx_api_key,
        settings.app_password_hash,
        settings.app_session_secret,
        settings.telegram_bot_token,
        settings.telegram_chat_id,
    )
    install_global_log_redaction(extra_secrets=extra)


async def _background_auto_connect(
    auth_service: TopstepAuthService,
    settings: Settings,
    live_feed_service: LiveFeedService | None = None,
) -> None:
    try:
        await auth_service.login(
            username=settings.topstepx_username,
            api_key=settings.topstepx_api_key,
            live=settings.topstepx_live,
        )
        logger.info("TopstepX background auto-connect succeeded (live=%s)", settings.topstepx_live)
        if live_feed_service is not None:
            await live_feed_service.on_auth_connected()
    except asyncio.CancelledError:
        raise
    except TopstepError as exc:
        logger.warning("TopstepX background auto-connect failed: %s", redact_secrets(str(exc)))
    except Exception as exc:
        logger.warning(
            "TopstepX background auto-connect unexpected error: %s",
            redact_secrets(str(exc)),
        )


def _resolve_frontend_dist_dir(settings: Settings) -> Path | None:
    """Resolve optional built SPA distribution directory if it contains index.html."""
    candidate = (
        settings.frontend_dist_dir
        if settings.frontend_dist_dir is not None
        else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    )
    resolved = candidate.resolve()
    if resolved.is_dir() and (resolved / "index.html").is_file():
        return resolved
    return None


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    signalr_ws_factory: WsConnectFactory | None = None,
    clock: Callable[[], int] | None = None,
    telegram_http_client: httpx.AsyncClient | None = None,
    backtest_worker_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings or load_settings()
    _install_redaction_filter(resolved_settings)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=build_httpx_timeout(resolved_settings))
        telegram_configured = bool(
            resolved_settings.telegram_bot_token.strip()
            and resolved_settings.telegram_chat_id.strip()
        )
        owns_telegram_client = telegram_http_client is None and telegram_configured
        tg_client = telegram_http_client
        if tg_client is None and telegram_configured:
            tg_client = httpx.AsyncClient(
                timeout=build_telegram_httpx_timeout(resolved_settings)
            )
        upstream_semaphore = asyncio.Semaphore(resolved_settings.upstream_max_concurrency)
        telegram_semaphore = asyncio.Semaphore(resolved_settings.telegram_max_concurrency)
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
        bar_aggregator = BarAggregator()
        ws_broadcaster = WsBroadcaster(
            client_queue_maxsize=resolved_settings.ws_client_queue_maxsize,
            send_timeout_sec=resolved_settings.ws_send_timeout_sec,
            supported_timeframes=bar_aggregator.timeframes,
            max_clients=resolved_settings.ws_max_clients,
        )
        command_service = CommandService(sqlite_store, broadcaster=ws_broadcaster)
        signalr_hub = SignalRMarketHub(
            resolved_settings,
            auth_service=auth_service,
            ws_factory=signalr_ws_factory,
        )
        alert_engine = AlertEngine(scanner_enabled=resolved_settings.alert_scanner_enabled)
        telegram_notifier = TelegramClient(
            resolved_settings,
            tg_client,
            semaphore=telegram_semaphore,
        )
        alert_service = AlertService(
            resolved_settings,
            alert_engine,
            sqlite_store,
            telegram_notifier,
            auth_service,
            clock=clock,
        )
        live_feed_service = LiveFeedService(
            resolved_settings,
            auth_service,
            rest_client,
            parquet_store,
            sqlite_store,
            aggregator=bar_aggregator,
            broadcaster=ws_broadcaster,
            signalr_hub=signalr_hub,
            on_bar_signal=alert_service.handle_bar_signal,
            clock=clock,
        )
        backtest_service = BacktestService(
            resolved_settings,
            parquet_store,
            sqlite_store=sqlite_store,
            clock=clock,
            worker_fn=backtest_worker_fn,
        )

        app_instance.state.settings = resolved_settings
        app_instance.state.http_client = client
        app_instance.state.telegram_http_client = tg_client
        app_instance.state.upstream_semaphore = upstream_semaphore
        app_instance.state.telegram_semaphore = telegram_semaphore
        app_instance.state.topstep_auth = auth_service
        app_instance.state.rest_client = rest_client
        app_instance.state.sqlite_store = sqlite_store
        app_instance.state.parquet_store = parquet_store
        app_instance.state.history_service = history_service
        app_instance.state.session_manager = session_manager
        app_instance.state.login_guard = login_guard
        app_instance.state.bar_aggregator = bar_aggregator
        app_instance.state.ws_broadcaster = ws_broadcaster
        app_instance.state.command_service = command_service
        app_instance.state.signalr_hub = signalr_hub
        app_instance.state.alert_engine = alert_engine
        app_instance.state.telegram_client = telegram_notifier
        app_instance.state.alert_service = alert_service
        app_instance.state.live_feed_service = live_feed_service
        app_instance.state.backtest_service = backtest_service

        await alert_service.start()
        await live_feed_service.start()
        await backtest_service.start()

        auth_task: asyncio.Task[None] | None = None
        has_creds = bool(
            resolved_settings.topstepx_username.strip()
            and resolved_settings.topstepx_api_key.strip()
        )
        if has_creds:
            auth_task = asyncio.create_task(
                _background_auto_connect(auth_service, resolved_settings, live_feed_service)
            )
        app_instance.state.auto_connect_task = auth_task

        try:
            yield
        finally:
            if auth_task is not None and not auth_task.done():
                auth_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await auth_task
            await backtest_service.stop()
            await live_feed_service.stop()
            await alert_service.stop()
            await ws_broadcaster.close_all()
            sqlite_store.close()
            if owns_telegram_client and tg_client is not None:
                await tg_client.aclose()
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
            resolved_settings.telegram_bot_token,
            resolved_settings.telegram_chat_id,
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
    application.include_router(routes_alerts.router)
    application.include_router(routes_backtest.router)
    application.include_router(routes_drawings.router)
    application.include_router(routes_layout.router)
    application.include_router(routes_market.router)
    application.include_router(routes_market.ws_router)

    dist_dir = _resolve_frontend_dist_dir(resolved_settings)
    if dist_dir is not None:
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            application.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="spa-assets",
            )

        @application.get("/{full_path:path}", include_in_schema=False)
        async def _serve_spa(full_path: str) -> FileResponse:
            cleaned = full_path.lstrip("/")
            if (
                cleaned == "api"
                or cleaned.startswith("api/")
                or cleaned == "ws"
                or cleaned.startswith("ws/")
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not Found",
                )
            if ".." in cleaned.replace("\\", "/").split("/"):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not Found",
                )
            dist_root = dist_dir.resolve()
            if cleaned:
                candidate = (dist_root / cleaned).resolve()
                if not candidate.is_relative_to(dist_root):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Not Found",
                    )
                if candidate.is_file():
                    return FileResponse(candidate)
            index_path = dist_root / "index.html"
            if not index_path.is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not Found",
                )
            return FileResponse(index_path)

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
