"""Application configuration and security settings (F01, F02, R03)."""

from __future__ import annotations

import ipaddress
import json
import math
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_LOOPBACK_URL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_market_hub_url(value: str) -> str:
    """Validate TopstepX Market Hub URL: wss:// required unless host is loopback."""
    cleaned = value.strip()
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("TOPSTEPX_RTC_URL must be a valid ws:// or wss:// URL")
    hostname = (parsed.hostname or "").lower()
    if scheme == "ws" and hostname not in _LOOPBACK_URL_HOSTNAMES:
        raise ValueError(
            "Non-loopback SignalR Market Hub URL must use wss:// (TLS) to protect TopstepX JWTs"
        )
    if not parsed.path.lower().rstrip("/").endswith("/hubs/market"):
        raise ValueError(
            "TOPSTEPX_RTC_URL must target the read-only Market Hub (/hubs/market) (SEC-001)"
        )
    return cleaned


def validate_telegram_api_base_url(value: str) -> str:
    """Validate Telegram Bot API base URL: https:// required unless host is loopback."""
    cleaned = value.strip()
    try:
        parsed = urlparse(cleaned)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_BASE_URL must be a valid http:// or https:// URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("TELEGRAM_API_BASE_URL must be a valid http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("TELEGRAM_API_BASE_URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("TELEGRAM_API_BASE_URL must not contain query or fragment components")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("TELEGRAM_API_BASE_URL must include a valid hostname")
    if scheme == "http" and hostname not in _LOOPBACK_URL_HOSTNAMES:
        raise ValueError(
            "Non-loopback TELEGRAM_API_BASE_URL must use https:// (TLS) to protect bot tokens"
        )
    return cleaned.rstrip("/")


def _parse_float_list(value: object) -> list[float]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("ALERT_RETRY_BACKOFF_SEC cannot be empty")
        if stripped.startswith("["):
            try:
                parsed: object = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("ALERT_RETRY_BACKOFF_SEC JSON array is invalid") from exc
            if not isinstance(parsed, list):
                raise ValueError("ALERT_RETRY_BACKOFF_SEC JSON must be an array of numbers")
            raw_items: list[object] = parsed
        else:
            raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise ValueError("ALERT_RETRY_BACKOFF_SEC must be a list or comma-separated string")

    if not raw_items:
        raise ValueError("ALERT_RETRY_BACKOFF_SEC must contain at least one positive delay")

    floats: list[float] = []
    for item in raw_items:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            raise ValueError("ALERT_RETRY_BACKOFF_SEC entries must be positive numbers")
        try:
            num = float(item)
        except ValueError as exc:
            raise ValueError("ALERT_RETRY_BACKOFF_SEC entries must be positive numbers") from exc
        if not math.isfinite(num) or num <= 0.0:
            raise ValueError("ALERT_RETRY_BACKOFF_SEC entries must be positive finite floats")
        floats.append(num)
    return floats


def _is_forbidden_bind_host(host: str) -> bool:
    if not host or host == "*":
        return True
    unbracketed = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(unbracketed).is_unspecified
    except ValueError:
        return False


def _parse_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            if parsed is not None:
                raise ValueError("Expected JSON array of strings")
        return [part.strip() for part in stripped.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("Expected list or comma-separated string")


class Settings(BaseSettings):
    """Typed runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    app_env: Literal["development", "production"] = "development"
    app_bind_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_allowed_hosts: list[str] | None = None
    app_allowed_origins: list[str] | None = None
    app_password_hash: str = ""
    app_session_secret: str = ""
    app_cookie_secure: bool = False
    session_ttl_seconds: int = Field(default=86400, ge=60)
    login_rate_limit_attempts: int = Field(default=5, ge=1, le=100)
    login_rate_limit_window_sec: float = Field(default=60.0, gt=0.0)
    login_max_concurrency: int = Field(default=2, ge=1, le=16)

    topstepx_username: str = ""
    topstepx_api_key: str = ""
    topstepx_live: bool = False
    topstepx_base_url: str = "https://api.topstepx.com"
    topstepx_key_updated_at: str | None = None
    key_rotation_days: int = Field(default=30, ge=1)
    key_rotation_enforce: bool = False
    jwt_refresh_hours: float = Field(default=23.0, gt=0.0, lt=24.0)

    upstream_connect_timeout_sec: float = Field(default=5.0, gt=0.0)
    upstream_read_timeout_sec: float = Field(default=15.0, gt=0.0)
    upstream_write_timeout_sec: float = Field(default=10.0, gt=0.0)
    upstream_pool_timeout_sec: float = Field(default=5.0, gt=0.0)
    upstream_total_timeout_sec: float = Field(default=25.0, gt=0.0)
    upstream_max_concurrency: int = Field(default=4, ge=1, le=32)
    upstream_max_retries: int = Field(default=2, ge=0, le=5)

    history_page_limit: int = Field(default=5000, ge=1, le=20000)
    history_max_pages: int = Field(default=50, ge=1, le=500)
    history_backfill_timeout_sec: float = Field(default=120.0, gt=0.0)

    topstepx_rtc_url: str = "wss://rtc.topstepx.com/hubs/market"
    signalr_enabled: bool = True
    signalr_recovery_queue_maxsize: int = Field(default=4096, ge=1, le=65536)
    signalr_recovery_timeout_sec: float = Field(default=30.0, gt=0.0)
    signalr_reconnect_initial_delay_sec: float = Field(default=2.0, gt=0.0)
    signalr_reconnect_max_delay_sec: float = Field(default=60.0, gt=0.0)
    signalr_ping_interval_sec: float = Field(default=15.0, gt=0.0)
    signalr_heartbeat_timeout_sec: float = Field(default=45.0, gt=0.0)

    ws_client_queue_maxsize: int = Field(default=64, ge=1, le=4096)
    ws_send_timeout_sec: float = Field(default=5.0, gt=0.0)
    ws_max_clients: int = Field(default=32, ge=1, le=256)

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base_url: str = "https://api.telegram.org"
    alert_ttl_seconds: int = Field(default=90, ge=1, le=3600)
    alert_max_retries: int = Field(default=3, ge=0, le=10)
    alert_retry_backoff_sec: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [2.0, 5.0, 15.0]
    )
    alert_dispatch_poll_interval_sec: float = Field(default=1.0, gt=0.0, le=60.0)
    telegram_connect_timeout_sec: float = Field(default=5.0, gt=0.0)
    telegram_read_timeout_sec: float = Field(default=10.0, gt=0.0)
    telegram_write_timeout_sec: float = Field(default=10.0, gt=0.0)
    telegram_pool_timeout_sec: float = Field(default=5.0, gt=0.0)
    telegram_total_timeout_sec: float = Field(default=15.0, gt=0.0)
    telegram_max_concurrency: int = Field(default=2, ge=1, le=8)
    alert_scanner_enabled: bool = True
    alert_dispatcher_enabled: bool = True

    backtest_enabled: bool = True
    backtest_max_queue_depth: int = Field(default=3, ge=1, le=16)
    backtest_max_bars: int = Field(default=50000, ge=10, le=500000)
    backtest_max_date_range_days: int = Field(default=365, ge=1, le=3650)
    backtest_timeout_sec: float = Field(default=30.0, gt=0.0, le=600.0)
    backtest_max_result_bytes: int = Field(default=5_000_000, ge=1024, le=50_000_000)
    backtest_max_jobs_retained: int = Field(default=64, ge=1, le=500)

    frontend_dist_dir: Path | None = None
    data_dir: Path = Path("data")

    @field_validator("frontend_dist_dir", mode="before")
    @classmethod
    def _validate_frontend_dist_dir(cls, value: object) -> Path | None:
        if value is None:
            return None
        if isinstance(value, Path):
            return value
        cleaned = str(value).strip()
        return Path(cleaned) if cleaned else None

    @field_validator("app_env", mode="before")
    @classmethod
    def _validate_app_env(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"development", "production"}:
            raise ValueError("APP_ENV must be 'development' or 'production'")
        return normalized

    @field_validator("app_bind_host")
    @classmethod
    def _validate_bind_host(cls, value: str) -> str:
        cleaned = value.strip()
        if _is_forbidden_bind_host(cleaned):
            raise ValueError(
                f"Wildcard or empty APP_BIND_HOST '{cleaned}' is forbidden (SEC-003). "
                "Bind explicitly to 127.0.0.1 or a configured Tailscale IP."
            )
        return cleaned

    @field_validator("app_allowed_hosts", "app_allowed_origins", mode="before")
    @classmethod
    def _validate_lists(cls, value: object) -> list[str] | None:
        return _parse_str_list(value)

    @field_validator("topstepx_rtc_url")
    @classmethod
    def _validate_rtc_url(cls, value: str) -> str:
        return validate_market_hub_url(value)

    @field_validator("telegram_api_base_url")
    @classmethod
    def _validate_telegram_api_base_url(cls, value: str) -> str:
        return validate_telegram_api_base_url(value)

    @field_validator("alert_retry_backoff_sec", mode="before")
    @classmethod
    def _validate_alert_retry_backoff_sec(cls, value: object) -> list[float]:
        return _parse_float_list(value)

    @field_validator("topstepx_key_updated_at", mode="before")
    @classmethod
    def _validate_key_updated_at(cls, value: object) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        date.fromisoformat(cleaned)
        return cleaned

    @model_validator(mode="after")
    def _populate_allowlists(self) -> Settings:
        if self.app_allowed_hosts is None:
            hosts = list(DEFAULT_LOOPBACK_HOSTS)
            bind_lower = self.app_bind_host.lower()
            if bind_lower not in hosts:
                hosts.append(bind_lower)
            self.app_allowed_hosts = hosts
        else:
            self.app_allowed_hosts = [h.lower() for h in self.app_allowed_hosts]

        if self.app_allowed_origins is None:
            origins = [
                "http://127.0.0.1",
                "http://localhost",
                "http://[::1]",
                "https://127.0.0.1",
                "https://localhost",
                "https://[::1]",
                f"http://127.0.0.1:{self.app_port}",
                f"http://localhost:{self.app_port}",
                f"http://[::1]:{self.app_port}",
                f"https://127.0.0.1:{self.app_port}",
                f"https://localhost:{self.app_port}",
                f"https://[::1]:{self.app_port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ]
            bind_host = self.app_bind_host.lower()
            if bind_host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
                origins.extend(
                    [
                        f"http://{bind_host}:{self.app_port}",
                        f"https://{bind_host}:{self.app_port}",
                        f"http://{bind_host}",
                        f"https://{bind_host}",
                    ]
                )
            self.app_allowed_origins = list(dict.fromkeys(origins))
        else:
            self.app_allowed_origins = [
                o.rstrip("/").lower() for o in self.app_allowed_origins if o.strip()
            ]

        return self


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_settings(env_file: Path | None = None) -> Settings:
    """Load Settings, optionally reading an explicit .env file if it exists."""
    target_env = env_file or DEFAULT_ENV_FILE
    if target_env.is_file():
        return Settings(_env_file=target_env)
    return Settings()
