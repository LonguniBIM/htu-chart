"""Read-only TopstepX authentication, JWT auto-refresh, and key rotation tracking (F02, R03)."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx

from app.config import Settings
from app.security import redact_secrets
from app.topstep.errors import (
    KeyRotationRequiredError,
    TopstepAuthError,
    UpstreamHTTPError,
    UpstreamTimeoutError,
)

ConnectionState = Literal["DISCONNECTED", "CONNECTED"]


@dataclass(frozen=True)
class KeyRotationStatus:
    """Computed key age and rotation enforcement status."""

    key_age_days: int | None
    key_rotation_due: bool
    key_rotation_enforced: bool


def build_httpx_timeout(settings: Settings) -> httpx.Timeout:
    """Construct explicit connect/read/write/pool timeouts from Settings (R03)."""
    return httpx.Timeout(
        connect=settings.upstream_connect_timeout_sec,
        read=settings.upstream_read_timeout_sec,
        write=settings.upstream_write_timeout_sec,
        pool=settings.upstream_pool_timeout_sec,
    )


class TopstepAuthService:
    """Manages server-side TopstepX JWT lifecycle and key rotation policy."""

    LOGIN_ENDPOINT = "/api/Auth/loginKey"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._semaphore = semaphore or asyncio.Semaphore(settings.upstream_max_concurrency)
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._session_tokens: deque[str] = deque(maxlen=8)
        self._username: str | None = None
        self._api_key: str | None = None
        self._live: bool = settings.topstepx_live
        self._connected_at: datetime | None = None
        self._token_acquired_at: datetime | None = None
        self._connection_state: ConnectionState = "DISCONNECTED"
        self._last_error: str | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether an active TopstepX JWT is held in memory."""
        return self._token is not None

    @property
    def connection_state(self) -> ConnectionState:
        """Return current upstream connection state."""
        return self._connection_state

    @property
    def live(self) -> bool:
        """Return active TopstepX live/sim mode."""
        return self._live

    def compute_key_rotation(self, now: datetime | None = None) -> KeyRotationStatus:
        """Evaluate TOPSTEPX_KEY_UPDATED_AT age against KEY_ROTATION_DAYS."""
        updated_raw = self._settings.topstepx_key_updated_at
        if not updated_raw:
            return KeyRotationStatus(
                key_age_days=None,
                key_rotation_due=False,
                key_rotation_enforced=self._settings.key_rotation_enforce,
            )
        updated_date = date.fromisoformat(updated_raw)
        current_utc_date = (now or datetime.now(UTC)).date()
        age_days = max(0, (current_utc_date - updated_date).days)
        due = age_days >= self._settings.key_rotation_days
        return KeyRotationStatus(
            key_age_days=age_days,
            key_rotation_due=due,
            key_rotation_enforced=self._settings.key_rotation_enforce,
        )

    def _clear_session_state(
        self,
        error_msg: str | None = None,
        *,
        clear_credentials: bool = False,
    ) -> None:
        self._token = None
        self._token_acquired_at = None
        self._connected_at = None
        self._connection_state = "DISCONNECTED"
        self._last_error = error_msg
        if clear_credentials:
            self._username = None
            self._api_key = None

    def _enforce_key_rotation(self, now: datetime | None = None) -> None:
        rotation = self.compute_key_rotation(now=now)
        if rotation.key_rotation_enforced and rotation.key_age_days is None:
            msg = "TOPSTEPX_KEY_UPDATED_AT must be configured when KEY_ROTATION_ENFORCE=true"
            self._clear_session_state(msg)
            raise KeyRotationRequiredError(msg)
        if rotation.key_rotation_enforced and rotation.key_rotation_due:
            self._clear_session_state("TopstepX API key rotation is due and enforced")
            raise KeyRotationRequiredError(
                f"TopstepX API key age ({rotation.key_age_days}d) meets or exceeds "
                f"KEY_ROTATION_DAYS ({self._settings.key_rotation_days}d); "
                "rotate key before connecting."
            )

    def needs_refresh(self, now: datetime | None = None) -> bool:
        """Return True when the cached JWT is older than jwt_refresh_hours."""
        if self._token is None or self._token_acquired_at is None:
            return False
        current = now or datetime.now(UTC)
        return (current - self._token_acquired_at) >= timedelta(
            hours=self._settings.jwt_refresh_hours
        )

    async def login(
        self,
        username: str,
        api_key: str,
        live: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        """Authenticate with TopstepX /api/Auth/loginKey under total deadline."""
        try:
            await asyncio.wait_for(
                self._acquire_and_login(
                    username=username,
                    api_key=api_key,
                    live=live,
                    now=now,
                ),
                timeout=self._settings.upstream_total_timeout_sec,
            )
        except TimeoutError as exc:
            raise UpstreamTimeoutError("TopstepX login timed out") from exc

    async def _acquire_and_login(
        self,
        username: str,
        api_key: str,
        live: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        async with self._lock:
            await self._login_locked(username=username, api_key=api_key, live=live, now=now)

    async def _login_locked(
        self,
        username: str,
        api_key: str,
        live: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        self._enforce_key_rotation(now=now)
        cleaned_user = username.strip()
        cleaned_key = api_key.strip()
        if not cleaned_user or not cleaned_key:
            self._clear_session_state("TopstepX username and API key are required")
            raise TopstepAuthError("TopstepX username and API key are required")

        target_live = self._settings.topstepx_live if live is None else bool(live)
        url = f"{self._settings.topstepx_base_url.rstrip('/')}{self.LOGIN_ENDPOINT}"

        try:
            data = await self._post_login_with_retries(url, cleaned_user, cleaned_key)
        except asyncio.CancelledError:
            self._clear_session_state("TopstepX login timed out")
            raise
        except TimeoutError as exc:
            self._clear_session_state("TopstepX login timed out")
            raise UpstreamTimeoutError("TopstepX login timed out") from exc
        except UpstreamTimeoutError as exc:
            self._clear_session_state(redact_secrets(str(exc), (cleaned_key, cleaned_user)))
            raise
        except TopstepAuthError as exc:
            safe_msg = redact_secrets(str(exc), (cleaned_key, cleaned_user))
            self._clear_session_state(safe_msg, clear_credentials=True)
            raise
        except UpstreamHTTPError as exc:
            safe_msg = redact_secrets(str(exc), (cleaned_key, cleaned_user))
            self._clear_session_state(safe_msg)
            raise

        token = data.get("token")
        if not token or not isinstance(token, str):
            err_msg = redact_secrets(
                str(data.get("errorMessage") or "TopstepX login did not return a token"),
                (cleaned_key, cleaned_user),
            )
            self._clear_session_state(err_msg)
            raise TopstepAuthError(err_msg)

        acquired = now or datetime.now(UTC)
        self._token = token
        if token not in self._session_tokens:
            self._session_tokens.append(token)
        self._username = cleaned_user
        self._api_key = cleaned_key
        self._live = target_live
        self._connected_at = self._connected_at or acquired
        self._token_acquired_at = acquired
        self._connection_state = "CONNECTED"
        self._last_error = None

    async def _post_login_with_retries(
        self,
        url: str,
        username: str,
        api_key: str,
    ) -> dict[str, Any]:
        max_attempts = self._settings.upstream_max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            try:
                return await self._post_login(url, username, api_key)
            except TopstepAuthError:
                raise
            except UpstreamHTTPError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    raise
                last_exc = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise UpstreamTimeoutError("TopstepX login timed out") from exc
                safe_msg = redact_secrets(str(exc), (api_key, username))
                raise UpstreamHTTPError(f"TopstepX login transport error: {safe_msg}") from exc

        if isinstance(last_exc, UpstreamHTTPError):
            raise last_exc
        raise UpstreamTimeoutError("TopstepX login failed after bounded retries")

    async def _post_login(self, url: str, username: str, api_key: str) -> dict[str, Any]:
        async with (
            self._semaphore,
            self._client.stream(
                "POST",
                url,
                json={"userName": username, "apiKey": api_key},
                headers={
                    "Accept": "application/json, text/plain",
                    "Content-Type": "application/json",
                },
            ) as resp,
        ):
            raw_body = await resp.aread()
        text = raw_body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise UpstreamHTTPError(
                f"Invalid JSON from TopstepX auth (HTTP {resp.status_code})",
                status_code=resp.status_code,
            ) from exc

        if resp.status_code >= 500:
            err_msg = (
                (data.get("errorMessage") or data.get("message"))
                if isinstance(data, dict)
                else None
            )
            msg = err_msg or f"HTTP {resp.status_code}"
            raise UpstreamHTTPError(
                redact_secrets(str(msg), (api_key, username))[:300],
                status_code=resp.status_code,
            )

        if not isinstance(data, dict):
            if resp.status_code >= 400:
                raise TopstepAuthError(f"HTTP {resp.status_code}")
            raise TopstepAuthError("TopstepX auth returned unexpected non-object JSON payload")

        if resp.status_code >= 400:
            msg = data.get("errorMessage") or data.get("message") or f"HTTP {resp.status_code}"
            if resp.status_code in {401, 403}:
                raise TopstepAuthError(redact_secrets(str(msg), (api_key, username))[:300])
            raise UpstreamHTTPError(
                redact_secrets(str(msg), (api_key, username))[:300],
                status_code=resp.status_code,
            )

        if data.get("success") is False:
            msg = data.get("errorMessage") or f"TopstepX errorCode={data.get('errorCode')}"
            raise TopstepAuthError(redact_secrets(str(msg), (api_key, username))[:300])
        return data

    async def ensure_token(self, now: datetime | None = None) -> str:
        """Return a valid JWT, refreshing automatically when token age >= jwt_refresh_hours."""
        self._enforce_key_rotation(now=now)
        if self._token is None:
            raise TopstepAuthError("Not connected to TopstepX")
        if self.needs_refresh(now=now):
            try:
                await asyncio.wait_for(
                    self._refresh_locked(now=now),
                    timeout=self._settings.upstream_total_timeout_sec,
                )
            except TimeoutError as exc:
                raise UpstreamTimeoutError("TopstepX token refresh timed out") from exc
        if self._token is None:
            raise TopstepAuthError("Not connected to TopstepX")
        return self._token

    async def _refresh_locked(self, now: datetime | None = None) -> None:
        async with self._lock:
            if self._token is None:
                raise TopstepAuthError("Not connected to TopstepX")
            if self.needs_refresh(now=now):
                if not self._username or not self._api_key:
                    raise TopstepAuthError("Cannot refresh JWT without credentials")
                await self._login_locked(
                    username=self._username,
                    api_key=self._api_key,
                    live=self._live,
                    now=now,
                )

    async def disconnect(self) -> None:
        """Clear in-memory TopstepX credentials and transition to DISCONNECTED."""
        async with self._lock:
            self._token = None
            self._session_tokens.clear()
            self._username = None
            self._api_key = None
            self._connected_at = None
            self._token_acquired_at = None
            self._connection_state = "DISCONNECTED"
            self._last_error = None

    def active_redaction_secrets(self) -> tuple[str, ...]:
        """Return active in-memory credentials for internal redaction only."""
        return tuple(
            dict.fromkeys(
                s for s in (*self._session_tokens, self._token, self._api_key) if s
            )
        )

    def get_status(self, now: datetime | None = None) -> dict[str, Any]:
        """Return strictly redacted connection and key-rotation status."""
        rotation = self.compute_key_rotation(now=now)
        return {
            "connected": self._token is not None,
            "connection_state": self._connection_state,
            "live": self._live,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "key_age_days": rotation.key_age_days,
            "key_rotation_due": rotation.key_rotation_due,
            "key_rotation_enforced": rotation.key_rotation_enforced,
            "has_env_credentials": bool(
                self._settings.topstepx_username.strip()
                and self._settings.topstepx_api_key.strip()
            ),
            "last_error": self._last_error,
        }
