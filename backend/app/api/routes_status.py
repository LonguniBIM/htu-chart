"""Health, caller auth, and TopstepX connection status routes (F01, F02, R03)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.config import Settings
from app.security import (
    SESSION_COOKIE_NAME,
    LoginGuard,
    LoginRateLimitedError,
    SessionData,
    SessionManager,
    get_session_manager,
    get_settings,
    is_https_request,
    redact_secrets,
    require_csrf,
    require_dev_loopback,
    require_session,
)
from app.services.alert_service import AlertService
from app.services.live_feed_service import LiveFeedService
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.errors import (
    KeyRotationRequiredError,
    TopstepAuthError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
)

router = APIRouter(prefix="/api", tags=["status"])


class LoginRequest(BaseModel):
    """Single-user caller login payload."""

    password: str


class ConnectRequest(BaseModel):
    """Dev-only credential connection payload."""

    userName: str = ""
    apiKey: str = ""
    live: bool | None = None


def _get_topstep_auth(request: Request) -> TopstepAuthService:
    return request.app.state.topstep_auth


def _get_login_guard(request: Request) -> LoginGuard:
    return request.app.state.login_guard


def _get_sqlite_store(request: Request) -> SqliteStore:
    return request.app.state.sqlite_store


def _get_live_feed_service(request: Request) -> LiveFeedService:
    return request.app.state.live_feed_service


def _get_alert_service(request: Request) -> AlertService:
    return request.app.state.alert_service


@router.get("/health")
async def health() -> dict[str, str]:
    """Minimal unauthenticated liveness probe (zero TopstepX or business metadata)."""
    return {"status": "ok"}


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session_manager: Annotated[SessionManager, Depends(get_session_manager)],
    login_guard: Annotated[LoginGuard, Depends(_get_login_guard)],
) -> dict[str, Any]:
    """Authenticate caller with Argon2id password and issue HttpOnly SameSite=Strict cookie."""
    client_key = request.client.host if request.client else "unknown"
    if login_guard.is_rate_limited(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts; please wait before retrying",
        )

    try:
        verified = await login_guard.verify_password_offload(
            session_manager,
            body.password,
            client_key=client_key,
        )
    except LoginRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    session = session_manager.create_session()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        httponly=True,
        samesite="strict",
        secure=is_https_request(request, settings),
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    return {"authenticated": True, "csrf_token": session.csrf_token}


@router.post("/auth/logout")
async def logout(
    response: Response,
    session: Annotated[SessionData, Depends(require_csrf)],
    session_manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> dict[str, bool]:
    """Invalidate active caller session and clear cookie."""
    session_manager.invalidate_session(session.session_id)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return {"authenticated": False}


@router.get("/status")
async def get_status(
    _session: Annotated[SessionData, Depends(require_session)],
    topstep_auth: Annotated[TopstepAuthService, Depends(_get_topstep_auth)],
    sqlite_store: Annotated[SqliteStore, Depends(_get_sqlite_store)],
    live_feed_service: Annotated[LiveFeedService, Depends(_get_live_feed_service)],
    alert_service: Annotated[AlertService, Depends(_get_alert_service)],
) -> dict[str, Any]:
    """Return strictly redacted TopstepX connection, rotation, feed, and alert status."""
    payload = topstep_auth.get_status()
    payload["data_quality"] = await sqlite_store.get_quality_summary()
    payload.update(live_feed_service.get_status_snapshot())
    payload["alerts"] = await alert_service.get_status_snapshot()
    return payload


@router.post("/connect")
async def connect_topstep(
    body: ConnectRequest,
    _session: Annotated[SessionData, Depends(require_csrf)],
    _dev_only: Annotated[None, Depends(require_dev_loopback)],
    settings: Annotated[Settings, Depends(get_settings)],
    topstep_auth: Annotated[TopstepAuthService, Depends(_get_topstep_auth)],
    live_feed_service: Annotated[LiveFeedService, Depends(_get_live_feed_service)],
) -> dict[str, Any]:
    """Dev-only loopback endpoint to authenticate TopstepX credentials."""
    username = body.userName.strip() or settings.topstepx_username.strip()
    api_key = body.apiKey.strip() or settings.topstepx_api_key.strip()
    if not username or not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TopstepX username and API key are required",
        )

    try:
        await topstep_auth.login(username=username, api_key=api_key, live=body.live)
    except KeyRotationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=redact_secrets(str(exc), (api_key, username)),
        ) from exc
    except TopstepAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=redact_secrets(str(exc), (api_key, username)),
        ) from exc
    except UpstreamTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=redact_secrets(str(exc), (api_key, username)),
        ) from exc
    except UpstreamHTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=redact_secrets(str(exc), (api_key, username)),
        ) from exc

    await live_feed_service.on_auth_connected()
    return topstep_auth.get_status()


@router.post("/disconnect")
async def disconnect_topstep(
    _session: Annotated[SessionData, Depends(require_csrf)],
    topstep_auth: Annotated[TopstepAuthService, Depends(_get_topstep_auth)],
    live_feed_service: Annotated[LiveFeedService, Depends(_get_live_feed_service)],
) -> dict[str, Any]:
    """Clear in-memory TopstepX JWT and mark connection DISCONNECTED."""
    await live_feed_service.on_auth_disconnected()
    await topstep_auth.disconnect()
    return topstep_auth.get_status()
