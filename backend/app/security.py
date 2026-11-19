"""Caller auth boundary, Host/Origin/CSRF, WS handshake, and secret redaction (F01)."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, WebSocket, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings

SESSION_COOKIE_NAME = "topstep_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER_RE = re.compile(r"(?i)\bAuthorization\s*([:=])\s*(?:Bearer\s+)?[^\s,;'\"]+")
_JSON_SECRET_RE = re.compile(
    r"""(?i)(["']\b(?:access_token|apiKey|api_key|token|"""
    r"""TOPSTEPX_API_KEY|TELEGRAM_BOT_TOKEN|APP_PASSWORD_HASH|password)"""
    r"""\b["']\s*:\s*["'])([^"']*)(["'])"""
)
_KV_SECRET_RE = re.compile(
    r"""(?i)\b(access_token|apiKey|api_key|token|"""
    r"""TOPSTEPX_API_KEY|TELEGRAM_BOT_TOKEN|APP_PASSWORD_HASH|password)"""
    r"""\b(\s*[:=]\s*)(?!\*\*\*REDACTED\*\*\*)([^\s,;'"}\]]+)"""
)


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask bearer tokens, API keys, passwords, and known secrets in text."""
    if not text:
        return text
    redacted = _AUTH_HEADER_RE.sub(r"Authorization\1 ***REDACTED***", text)
    redacted = _BEARER_RE.sub("Bearer ***REDACTED***", redacted)
    redacted = _JSON_SECRET_RE.sub(r"\1***REDACTED***\3", redacted)
    redacted = _KV_SECRET_RE.sub(r"\1\2***REDACTED***", redacted)
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts credentials from log messages and tracebacks."""

    def __init__(self, extra_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._extra_secrets = tuple(s for s in extra_secrets if s and len(s) >= 6)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact_secrets(rendered, self._extra_secrets)
        record.args = ()
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact_secrets(
                formatter.formatException(record.exc_info),
                self._extra_secrets,
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, self._extra_secrets)
        return True


@dataclass(frozen=True)
class SessionData:
    """Server-side authenticated caller session."""

    session_id: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime


class SessionManager:
    """Single-user Argon2id authenticator and in-memory session registry."""

    def __init__(self, password_hash: str, ttl_seconds: int = 86400) -> None:
        self._password_hash = password_hash.strip()
        self._ttl_seconds = ttl_seconds
        self._hasher = PasswordHasher()
        self._sessions: dict[str, SessionData] = {}

    def verify_password(self, password: str) -> bool:
        """Verify candidate password against configured Argon2id hash (fail closed)."""
        if not password or not self._password_hash.startswith("$argon2id$"):
            return False
        try:
            return self._hasher.verify(self._password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def create_session(self) -> SessionData:
        """Issue a new cryptographically random session and bound CSRF token."""
        now = datetime.now(UTC)
        session = SessionData(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str | None) -> SessionData | None:
        """Look up an active non-expired session by ID."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if datetime.now(UTC) >= session.expires_at:
            self._sessions.pop(session_id, None)
            return None
        return session

    def invalidate_session(self, session_id: str | None) -> None:
        """Remove a session from the active registry."""
        if session_id:
            self._sessions.pop(session_id, None)

    @staticmethod
    def verify_csrf(session: SessionData, token: str | None) -> bool:
        """Verify X-CSRF-Token in constant time."""
        if not token:
            return False
        return hmac.compare_digest(session.csrf_token, token)


def _strip_host_port(host_header: str) -> tuple[str, str | None]:
    """Return (full_lower, host_without_port_lower) for IPv4, hostname, or bracketed IPv6."""
    cleaned = host_header.strip().lower()
    if not cleaned:
        return "", None
    if cleaned.startswith("["):
        end_bracket = cleaned.find("]")
        if end_bracket == -1:
            return cleaned, None
        bracketed = cleaned[: end_bracket + 1]
        remainder = cleaned[end_bracket + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return cleaned, None
        return cleaned, bracketed
    if ":" in cleaned:
        host_part, port_part = cleaned.rsplit(":", 1)
        if port_part.isdigit():
            return cleaned, host_part
        return cleaned, None
    return cleaned, cleaned


def is_allowed_host(host_header: str | None, allowed_hosts: Iterable[str]) -> bool:
    """Validate Host header against APP_ALLOWED_HOSTS (DNS rebinding guard)."""
    if not host_header:
        return False
    allowed = {h.strip().lower() for h in allowed_hosts if h.strip()}
    full_host, bare_host = _strip_host_port(host_header)
    if full_host in allowed:
        return True
    if bare_host is None:
        return False
    if bare_host in allowed:
        return True
    if bare_host.startswith("[") and bare_host.endswith("]"):
        return bare_host[1:-1] in allowed
    return False


def _normalize_origin(origin_header: str) -> str | None:
    raw = origin_header.strip()
    if not raw or raw.lower() == "null":
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def is_allowed_origin(origin_header: str | None, allowed_origins: Iterable[str]) -> bool:
    """Validate Origin header strictly against APP_ALLOWED_ORIGINS."""
    if not origin_header:
        return False
    normalized = _normalize_origin(origin_header)
    if normalized is None:
        return False
    allowed = {o.strip().rstrip("/").lower() for o in allowed_origins if o.strip()}
    return normalized in allowed


class HostOriginMiddleware:
    """Pure ASGI middleware enforcing AllowedHost and AllowedOrigin checks."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        host_header = headers.get("host")
        if not is_allowed_host(host_header, self.settings.app_allowed_hosts or []):
            resp = JSONResponse(
                {"detail": "Disallowed Host header"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
            await resp(scope, receive, send)
            return

        origin_header = headers.get("origin")
        if origin_header is not None:
            if not is_allowed_origin(origin_header, self.settings.app_allowed_origins or []):
                resp = JSONResponse(
                    {"detail": "Disallowed Origin header"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
                await resp(scope, receive, send)
                return
        elif (
            scope.get("method", "").upper() in MUTATING_METHODS
            and headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
        ):
            resp = JSONResponse(
                {"detail": "Cross-site request requires an allowed Origin"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


def get_settings(request: Request) -> Settings:
    """Resolve application Settings from FastAPI app state."""
    return request.app.state.settings


def get_session_manager(request: Request) -> SessionManager:
    """Resolve SessionManager from FastAPI app state."""
    return request.app.state.session_manager


def require_session(
    request: Request,
    session_manager: Annotated[SessionManager, Depends(get_session_manager)],
) -> SessionData:
    """Enforce valid caller session cookie on protected API routes."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return session


def require_csrf(
    request: Request,
    session: Annotated[SessionData, Depends(require_session)],
) -> SessionData:
    """Enforce X-CSRF-Token on authenticated mutating requests."""
    if request.method.upper() in MUTATING_METHODS:
        csrf_token = request.headers.get(CSRF_HEADER_NAME)
        if not SessionManager.verify_csrf(session, csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing CSRF token",
            )
    return session


def require_dev_loopback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Restrict dev-only credential routes to APP_ENV=development and loopback client IP."""
    if settings.app_env != "development":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential connect route is disabled in production",
        )
    client_host = request.client.host if request.client else ""
    if client_host not in LOOPBACK_IPS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential connect route requires a loopback client IP",
        )


def is_https_request(request: Request, settings: Settings) -> bool:
    """Determine whether session cookie Secure flag must be set."""
    return bool(settings.app_cookie_secure or request.url.scheme == "https")


async def validate_ws_handshake(
    websocket: WebSocket,
    settings: Settings,
    session_manager: SessionManager,
) -> SessionData | None:
    """Validate Host, Origin, and session cookie on WebSocket handshake before accept()."""
    host_header = websocket.headers.get("host")
    if not is_allowed_host(host_header, settings.app_allowed_hosts or []):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    origin_header = websocket.headers.get("origin")
    if not is_allowed_origin(origin_header, settings.app_allowed_origins or []):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    session_id = websocket.cookies.get(SESSION_COOKIE_NAME)
    session = session_manager.get_session(session_id)
    if session is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    return session
