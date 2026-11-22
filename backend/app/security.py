"""Caller auth boundary, Host/Origin/CSRF, WS handshake, and secret redaction (F01)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
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

_REDACTED_FIELD_PATTERN = (
    r"Authorization|Cookie|Set-Cookie|topstep_session|access_token|apiKey|api_key|token|"
    r"TOPSTEPX_API_KEY|TELEGRAM_BOT_TOKEN|botToken|APP_PASSWORD_HASH|APP_SESSION_SECRET|password"
)
_AUTH_SCHEMES = r"Bearer|Basic|Token|Digest|ApiKey|OAuth|Negotiate|NTLM|AWS4-HMAC-SHA256"
_QUOTED_VALUE_ALT = r'(?:"(?:\\.|[^"\\])*"?(?:\\$)?|\'(?:\\.|[^\'\\])*\'?(?:\\$)?)'
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
_COOKIE_HEADER_RE = re.compile(
    r"(?im)((?:^|[<>\s])(?:Set-)?Cookie\b\s*[:=]\s*)(?![\s'\"]*\*\*\*REDACTED\*\*\*)([^\r\n]+)"
)
_AUTH_HEADER_RE = re.compile(
    rf"""(?i)\bAuthorization\s*([:=])\s*(?!['"])(?:(?:{_AUTH_SCHEMES})\s+)?[^\s,;'"]+"""
)
_JSON_SECRET_RE = re.compile(
    rf"""(?i)(?P<prefix>(?:"\b(?:{_REDACTED_FIELD_PATTERN})\b"|"""
    rf"""'\b(?:{_REDACTED_FIELD_PATTERN})\b')\s*:\s*)(?P<val>{_QUOTED_VALUE_ALT})"""
)
_QUOTED_KV_SECRET_RE = re.compile(
    rf"""(?i)\b(?P<key>{_REDACTED_FIELD_PATTERN})\b(?P<sep>\s*[:=]\s*)(?P<val>{_QUOTED_VALUE_ALT})"""
)
_KV_SECRET_RE = re.compile(
    rf"""(?i)\b({_REDACTED_FIELD_PATTERN})\b(\s*[:=]\s*)(?!\*\*\*REDACTED\*\*\*)([^\s,;'"}}\]]+)"""
)
_SENSITIVE_DICT_KEYS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "topstep_session",
    "access_token",
    "apikey",
    "api_key",
    "token",
    "bottoken",
    "topstepx_api_key",
    "telegram_bot_token",
    "app_password_hash",
    "app_session_secret",
    "password",
})
_PRINTF_SPEC_RE = re.compile(
    r"%(?:\([^)]+\))?[-#0 +]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa%]"
)
_NAMED_PRINTF_SPEC_RE = re.compile(
    r"%\((?P<key>[A-Za-z0-9_]+)\)[-+#0 *.\dhlL]*[diouxXeEfFgGcrsa]"
)
_MSG_SECRET_PREFIX_RE = re.compile(
    rf"""(?i)(?:(?:\bAuthorization\b\s*[:=]\s*(?:(?:{_AUTH_SCHEMES})\s+)?|"""
    rf"""\b(?:{_AUTH_SCHEMES})\s+|\b(?:{_REDACTED_FIELD_PATTERN})\b["']?\s*(?:[:=]\s*|\s+))["']?)$"""
)


def _redact_arg(
    value: Any,
    extra_secrets: tuple[str, ...],
    sensitive_keys: frozenset[str] = frozenset(),
) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return redact_secrets(bytes(value).decode("utf-8", errors="replace"), extra_secrets)
    if (
        hasattr(value, "opcode")
        and hasattr(value, "data")
        and isinstance(getattr(value, "data", None), (bytes, bytearray, memoryview))
    ):
        raw_text = bytes(value.data).decode("utf-8", errors="replace")
        redacted_bytes = redact_secrets(raw_text, extra_secrets).encode("utf-8", errors="replace")
        if is_dataclass(value) and not isinstance(value, type):
            with contextlib.suppress(TypeError):
                return replace(value, data=redacted_bytes)
        return redact_secrets(f"{value.opcode!r} {raw_text!r}", extra_secrets)
    if isinstance(value, str):
        return redact_secrets(value, extra_secrets)
    if isinstance(value, tuple):
        return tuple(_redact_arg(item, extra_secrets, sensitive_keys) for item in value)
    if isinstance(value, list):
        return [_redact_arg(item, extra_secrets, sensitive_keys) for item in value]
    if isinstance(value, dict):
        redacted_dict: dict[Any, Any] = {}
        for k, v in value.items():
            is_sensitive_key = isinstance(k, str) and (
                k.lower() in _SENSITIVE_DICT_KEYS or k in sensitive_keys
            )
            if is_sensitive_key and v not in (None, "", False):
                redacted_dict[k] = "***REDACTED***"
            else:
                redacted_dict[k] = _redact_arg(v, extra_secrets, sensitive_keys)
        return redacted_dict
    return redact_secrets(str(value), extra_secrets)


def _sensitive_positional_slots(msg: str) -> set[int]:
    slots: set[int] = set()
    pos_idx = 0
    for match in _PRINTF_SPEC_RE.finditer(msg):
        spec = match.group(0)
        if spec == "%%":
            continue
        if spec.startswith("%("):
            continue
        prefix = msg[: match.start()]
        if _MSG_SECRET_PREFIX_RE.search(prefix):
            slots.add(pos_idx)
        pos_idx += 1
    return slots


def _sensitive_named_keys(msg: str) -> frozenset[str]:
    keys: set[str] = set()
    for match in _NAMED_PRINTF_SPEC_RE.finditer(msg):
        prefix = msg[: match.start()]
        if _MSG_SECRET_PREFIX_RE.search(prefix):
            keys.add(match.group("key"))
    return frozenset(keys)


def _closing_quote(val: str, quote: str) -> str:
    if len(val) < 2 or val[-1] != quote:
        return ""
    preceding = val[:-1]
    slash_count = len(preceding) - len(preceding.rstrip("\\"))
    return quote if slash_count % 2 == 0 else ""


def _redact_json_match(match: re.Match[str]) -> str:
    val = match.group("val")
    quote = val[0]
    closing = _closing_quote(val, quote)
    return f"{match.group('prefix')}{quote}***REDACTED***{closing}"


def _redact_kv_quoted_match(match: re.Match[str]) -> str:
    val = match.group("val")
    quote = val[0]
    closing = _closing_quote(val, quote)
    return f"{match.group('key')}{match.group('sep')}{quote}***REDACTED***{closing}"


def _redact_log_message_template(msg: str, extra_secrets: tuple[str, ...]) -> str:
    """Redact literal secrets in a logging template while preserving % format specifiers."""

    def _safe_sub(
        pattern: re.Pattern[str],
        repl: str | Callable[[re.Match[str]], str],
        text: str,
    ) -> str:
        current_spans = [(m.start(), m.end()) for m in _PRINTF_SPEC_RE.finditer(text)]

        def _cb(match: re.Match[str]) -> str:
            m_start, m_end = match.span()
            if any(s < m_end and m_start < e for s, e in current_spans):
                return match.group(0)
            if callable(repl):
                return repl(match)
            return match.expand(repl)

        return pattern.sub(_cb, text)

    redacted = _safe_sub(_JSON_SECRET_RE, _redact_json_match, msg)
    redacted = _safe_sub(_QUOTED_KV_SECRET_RE, _redact_kv_quoted_match, redacted)
    redacted = _safe_sub(_AUTH_HEADER_RE, r"Authorization\1 ***REDACTED***", redacted)
    redacted = _safe_sub(_COOKIE_HEADER_RE, r"\1***REDACTED***", redacted)
    redacted = _safe_sub(_BEARER_RE, "Bearer ***REDACTED***", redacted)
    redacted = _safe_sub(_KV_SECRET_RE, r"\1\2***REDACTED***", redacted)
    redacted = _safe_sub(_JWT_RE, "***REDACTED***", redacted)
    for secret in extra_secrets:
        redacted = _redact_extra_secret(redacted, secret)
    return redacted


def _redact_extra_secret(text: str, secret: str) -> str:
    """Redact full secret and any truncated head/tail fragments (>=16 chars)."""
    if not secret or len(secret) < 6:
        return text
    redacted = text.replace(secret, "***REDACTED***")
    frag_len = 16
    if len(secret) >= 32:
        head = secret[:frag_len]
        if head in redacted:
            out: list[str] = []
            idx = 0
            while idx < len(redacted):
                pos = redacted.find(head, idx)
                if pos == -1:
                    out.append(redacted[idx:])
                    break
                out.append(redacted[idx:pos])
                match_len = frag_len
                while (
                    pos + match_len < len(redacted)
                    and match_len < len(secret)
                    and redacted[pos + match_len] == secret[match_len]
                ):
                    match_len += 1
                out.append("***REDACTED***")
                idx = pos + match_len
            redacted = "".join(out)
        tail = secret[-frag_len:]
        if tail in redacted:
            out_tail: list[str] = []
            idx = 0
            while idx < len(redacted):
                pos = redacted.find(tail, idx)
                if pos == -1:
                    out_tail.append(redacted[idx:])
                    break
                start = pos
                s_idx = len(secret) - frag_len
                while start > idx and s_idx > 0 and redacted[start - 1] == secret[s_idx - 1]:
                    start -= 1
                    s_idx -= 1
                out_tail.append(redacted[idx:start])
                out_tail.append("***REDACTED***")
                idx = pos + frag_len
            redacted = "".join(out_tail)
    return redacted


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask bearer tokens, API keys, passwords, and known secrets in text."""
    if not text:
        return text
    redacted = _JSON_SECRET_RE.sub(_redact_json_match, text)
    redacted = _QUOTED_KV_SECRET_RE.sub(_redact_kv_quoted_match, redacted)
    redacted = _AUTH_HEADER_RE.sub(r"Authorization\1 ***REDACTED***", redacted)
    redacted = _COOKIE_HEADER_RE.sub(r"\1***REDACTED***", redacted)
    redacted = _BEARER_RE.sub("Bearer ***REDACTED***", redacted)
    redacted = _KV_SECRET_RE.sub(r"\1\2***REDACTED***", redacted)
    redacted = _JWT_RE.sub("***REDACTED***", redacted)
    for secret in extra_secrets:
        redacted = _redact_extra_secret(redacted, secret)
    return redacted


class SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts credentials from log messages and tracebacks."""

    def __init__(self, extra_secrets: Iterable[str] | Callable[[], Iterable[str]] = ()) -> None:
        super().__init__()
        if callable(extra_secrets):
            self._secrets_provider: Callable[[], Iterable[str]] | None = extra_secrets
            self._extra_secrets: tuple[str, ...] = ()
        else:
            self._secrets_provider = None
            self._extra_secrets = tuple(s for s in extra_secrets if s and len(s) >= 6)

    def _effective_secrets(self) -> tuple[str, ...]:
        dynamic = self._secrets_provider() if self._secrets_provider is not None else ()
        combined = {
            s
            for s in (*self._extra_secrets, *dynamic, *_ACTIVE_EXTRA_SECRETS)
            if s and len(s) >= 6
        }
        return tuple(sorted(combined, key=len, reverse=True))

    def filter(self, record: logging.LogRecord) -> bool:
        eff_secrets = self._effective_secrets()
        sensitive_named: frozenset[str] = frozenset()
        if isinstance(record.msg, str):
            sensitive_slots = (
                _sensitive_positional_slots(record.msg)
                if isinstance(record.args, tuple)
                else set()
            )
            if isinstance(record.args, tuple) and len(record.args) >= 2:
                for idx in range(1, len(record.args)):
                    prev_arg = record.args[idx - 1]
                    if (
                        isinstance(prev_arg, str)
                        and prev_arg.strip().lower() in {"cookie", "set-cookie", "authorization"}
                    ):
                        sensitive_slots.add(idx)
            if isinstance(record.args, dict):
                sensitive_named = _sensitive_named_keys(record.msg)
            if record.args:
                record.msg = _redact_log_message_template(record.msg, eff_secrets)
            else:
                record.msg = redact_secrets(record.msg, eff_secrets)
        else:
            sensitive_slots = set()
            record.msg = _redact_arg(record.msg, eff_secrets)

        if isinstance(record.args, tuple):
            redacted_items = []
            for idx, item in enumerate(record.args):
                if idx in sensitive_slots:
                    redacted_items.append("***REDACTED***")
                else:
                    redacted_items.append(_redact_arg(item, eff_secrets))
            record.args = tuple(redacted_items)
        elif record.args:
            record.args = _redact_arg(
                record.args,
                eff_secrets,
                sensitive_keys=sensitive_named,
            )

        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact_secrets(
                formatter.formatException(record.exc_info),
                eff_secrets,
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text, eff_secrets)
        return True


_BASE_LOG_RECORD_FACTORY = logging.getLogRecordFactory()
_ACTIVE_EXTRA_SECRETS: set[str] = set()
_REDACTING_FACTORY_INSTALLED = False


def install_global_log_redaction(extra_secrets: Iterable[str] = ()) -> None:
    """Install log-record factory and handler filters to redact secrets across all loggers."""
    global _REDACTING_FACTORY_INSTALLED
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            _ACTIVE_EXTRA_SECRETS.add(secret)

    if not _REDACTING_FACTORY_INSTALLED:
        base_factory = _BASE_LOG_RECORD_FACTORY

        def _redacting_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = base_factory(*args, **kwargs)
            SecretRedactionFilter(_ACTIVE_EXTRA_SECRETS).filter(record)
            return record

        logging.setLogRecordFactory(_redacting_record_factory)
        _REDACTING_FACTORY_INSTALLED = True

    logger_names = (
        "",
        "topstep_chart",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "httpx",
        "websockets",
        "websockets.client",
        "websockets.server",
        "websockets.protocol",
    )
    for name in logger_names:
        target_logger = logging.getLogger(name)
        target_logger.filters = [
            flt for flt in target_logger.filters if not isinstance(flt, SecretRedactionFilter)
        ]
        target_logger.addFilter(SecretRedactionFilter(_ACTIVE_EXTRA_SECRETS))
        for handler in target_logger.handlers:
            handler.filters = [
                flt for flt in handler.filters if not isinstance(flt, SecretRedactionFilter)
            ]
            handler.addFilter(SecretRedactionFilter(_ACTIVE_EXTRA_SECRETS))


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
        return hmac.compare_digest(
            session.csrf_token.encode("utf-8"),
            token.encode("utf-8"),
        )


class LoginRateLimitedError(Exception):
    """Raised when caller exceeds failed login quota or concurrent verification limit."""


class LoginGuard:
    """Sliding-window failed-login rate limiter and bounded Argon2id thread admission."""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        max_concurrency: int = 2,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def is_rate_limited(self, client_key: str) -> bool:
        """Return True if client_key has exhausted failed login attempts in window."""
        now = time.monotonic()
        bucket = self._failures.get(client_key)
        if not bucket:
            return False
        self._prune(bucket, now)
        if not bucket:
            self._failures.pop(client_key, None)
            return False
        return len(bucket) >= self._max_attempts

    def record_failure(self, client_key: str) -> None:
        """Record a failed login timestamp for client_key."""
        now = time.monotonic()
        bucket = self._failures[client_key]
        self._prune(bucket, now)
        bucket.append(now)

    def reset(self, client_key: str) -> None:
        """Clear failed login history after successful authentication."""
        self._failures.pop(client_key, None)

    def _finalize_attempt_and_release(
        self,
        client_key: str | None,
        verified: bool,
    ) -> None:
        if client_key is not None:
            if verified:
                self.reset(client_key)
            else:
                self.record_failure(client_key)
        self._semaphore.release()

    def _release_permit_threadsafe(
        self,
        loop: asyncio.AbstractEventLoop,
        client_key: str | None,
        verified: bool,
    ) -> None:
        if loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(
                self._finalize_attempt_and_release,
                client_key,
                verified,
            )

    async def verify_password_offload(
        self,
        session_manager: SessionManager,
        password: str,
        client_key: str | None = None,
        acquire_timeout: float = 1.0,
    ) -> bool:
        """Run Argon2id verification off the event loop with bounded concurrency."""
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=acquire_timeout)
        except TimeoutError as exc:
            raise LoginRateLimitedError(
                "Too many concurrent login attempts; try again shortly"
            ) from exc

        if client_key is not None and self.is_rate_limited(client_key):
            self._semaphore.release()
            raise LoginRateLimitedError(
                "Too many failed login attempts; please wait before retrying"
            )

        loop = asyncio.get_running_loop()
        state_lock = threading.Lock()
        state = {"started": False, "aborted": False}

        def _worker() -> bool:
            with state_lock:
                if state["aborted"]:
                    return False
                state["started"] = True
            verified = False
            try:
                verified = session_manager.verify_password(password)
                return verified
            finally:
                self._release_permit_threadsafe(loop, client_key, verified)

        try:
            return await asyncio.to_thread(_worker)
        except BaseException:
            should_release = False
            with state_lock:
                if not state["started"]:
                    state["aborted"] = True
                    should_release = True
            if should_release:
                self._semaphore.release()
            raise


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
    try:
        parsed = urlparse(raw)
        _ = parsed.port
    except ValueError:
        return None
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
