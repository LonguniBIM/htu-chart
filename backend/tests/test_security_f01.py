"""Value-first tests for F01 caller auth, Host/Origin/CSRF, redaction, and read-only guard."""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect
from uvicorn.logging import AccessFormatter

import app.config as config_mod
from app.config import DEFAULT_ENV_FILE, Settings, load_settings
from app.main import create_app
from app.security import (
    LoginGuard,
    LoginRateLimitedError,
    SecretRedactionFilter,
    SessionManager,
    redact_secrets,
    validate_ws_handshake,
)

TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)


def _mock_topstep_transport(
    jwt_value: str = "jwt-secret-value-12345",
    fail_with_secret: str | None = None,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            if fail_with_secret:
                return httpx.Response(
                    401,
                    json={"errorMessage": f"Rejected apiKey={fail_with_secret}"},
                )
            return httpx.Response(200, json={"success": True, "token": jwt_value})
        return httpx.Response(404, json={"errorMessage": "Not found"})

    return httpx.MockTransport(handler)


@asynccontextmanager
async def _async_client(
    settings: Settings,
    *,
    client_ip: str = "127.0.0.1",
    transport: httpx.AsyncBaseTransport | None = None,
):
    upstream = httpx.AsyncClient(transport=transport or _mock_topstep_transport())
    app = create_app(settings=settings, http_client=upstream)
    try:
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=(client_ip, 54321))
            async with httpx.AsyncClient(
                transport=asgi,
                base_url="http://127.0.0.1:8000",
            ) as client:
                yield client, app
    finally:
        await upstream.aclose()


async def _login_and_get_csrf(client: httpx.AsyncClient, password: str = TEST_PASSWORD) -> str:
    resp = await client.post("/api/auth/login", json={"password": password})
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert isinstance(data["csrf_token"], str) and len(data["csrf_token"]) >= 20
    return data["csrf_token"]


async def test_f01_login_issues_httponly_samesite_strict_cookie_and_rejects_bad_passwords() -> None:
    """Contract: Argon2id login issues HttpOnly/SameSite=Strict cookie and fails closed.

    Defect caught: Missing cookie flags, unverified password login, or empty-hash bypass.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(settings) as (client, _):
        bad_resp = await client.post("/api/auth/login", json={"password": "wrong-password"})
        assert bad_resp.status_code == 401
        assert "topstep_session" not in bad_resp.cookies

        ok_resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert ok_resp.status_code == 200
        set_cookie = ok_resp.headers.get("set-cookie", "")
        lowered = set_cookie.lower()
        assert "topstep_session=" in lowered
        assert "httponly" in lowered
        assert "samesite=strict" in lowered

    unset_settings = Settings(app_password_hash="")
    async with _async_client(unset_settings) as (client, _):
        fail_closed = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert fail_closed.status_code == 401

    secure_settings = Settings(app_password_hash=TEST_PASSWORD_HASH, app_cookie_secure=True)
    async with _async_client(secure_settings) as (client, _):
        sec_resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert sec_resp.status_code == 200
        assert "secure" in sec_resp.headers.get("set-cookie", "").lower()


async def test_f01_unauthenticated_api_routes_return_401_while_health_is_minimal() -> None:
    """Contract: All business/status routes require caller session; /api/health exposes no metadata.

    Defect caught: Unauthenticated callers inspecting status, contracts, or disconnecting TopstepX.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(settings) as (client, _):
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        assert (await client.get("/api/status")).status_code == 401
        assert (await client.get("/api/contracts")).status_code == 401
        assert (await client.post("/api/disconnect")).status_code == 401
        assert (await client.post("/api/auth/logout")).status_code == 401


async def test_f01_csrf_token_required_on_all_authenticated_mutations() -> None:
    """Contract: Authenticated POST mutations require matching X-CSRF-Token header.

    Defect caught: Cross-site form/fetch mutations executing without CSRF validation.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(settings) as (client, _):
        csrf_token = await _login_and_get_csrf(client)

        no_csrf = await client.post("/api/disconnect")
        assert no_csrf.status_code == 403

        bad_csrf = await client.post("/api/disconnect", headers={"X-CSRF-Token": "wrong-token"})
        assert bad_csrf.status_code == 403

        good_csrf = await client.post("/api/disconnect", headers={"X-CSRF-Token": csrf_token})
        assert good_csrf.status_code == 200
        assert good_csrf.json()["connected"] is False

        logout_no_csrf = await client.post("/api/auth/logout")
        assert logout_no_csrf.status_code == 403

        logout_ok = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout_ok.status_code == 200
        assert (await client.get("/api/status")).status_code == 401


async def test_f01_host_and_origin_allowlist_blocks_dns_rebinding_and_foreign_origins() -> None:
    """Contract: Untrusted Host or Origin headers must be rejected with 403.

    Defect caught: DNS rebinding or cross-origin browser requests reaching local API endpoints.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(settings) as (client, _):
        bad_host = await client.get("/api/health", headers={"Host": "evil.example"})
        assert bad_host.status_code == 403

        bad_origin = await client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert bad_origin.status_code == 403

        null_origin = await client.post(
            "/api/auth/login",
            json={"password": TEST_PASSWORD},
            headers={"Origin": "null"},
        )
        assert null_origin.status_code == 403

        malformed_origin = await client.get("/api/health", headers={"Origin": "http://["})
        assert malformed_origin.status_code == 403

        ok_origin = await client.get("/api/health", headers={"Origin": "http://127.0.0.1:8000"})
        assert ok_origin.status_code == 200


def test_f01_websocket_handshake_validates_origin_and_session_before_accept() -> None:
    """Contract: validate_ws_handshake rejects bad Origin or unauthenticated WS before accept().

    Defect caught: Unauthenticated or cross-origin WebSocket clients subscribing to market stream.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH)
    app = create_app(settings=settings)

    @app.websocket("/ws/test")
    async def ws_test(websocket: WebSocket) -> None:
        session = await validate_ws_handshake(
            websocket,
            app.state.settings,
            app.state.session_manager,
        )
        if session is None:
            return
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        with pytest.raises(WebSocketDisconnect) as exc_unauth:
            with client.websocket_connect(
                "/ws/test",
                headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
            ):
                pass
        assert exc_unauth.value.code == 1008

        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        session_cookie = login_resp.cookies["topstep_session"]

        with pytest.raises(WebSocketDisconnect) as exc_host:
            with client.websocket_connect(
                "/ws/test",
                headers={
                    "Host": "evil.example",
                    "Origin": "http://127.0.0.1:8000",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ):
                pass
        assert exc_host.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as exc_origin:
            with client.websocket_connect(
                "/ws/test",
                headers={
                    "Host": "127.0.0.1:8000",
                    "Origin": "https://evil.example",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ):
                pass
        assert exc_origin.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as exc_malformed_origin:
            with client.websocket_connect(
                "/ws/test",
                headers={
                    "Host": "127.0.0.1:8000",
                    "Origin": "http://[",
                    "Cookie": f"topstep_session={session_cookie}",
                },
            ):
                pass
        assert exc_malformed_origin.value.code == 1008

        with client.websocket_connect(
            "/ws/test",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "Cookie": f"topstep_session={session_cookie}",
            },
        ) as ws:
            assert ws.receive_json() == {"ok": True}


@pytest.mark.parametrize("bad_host", [".".join(["0"] * 4), "::", "[::]", "*", ""])
def test_f01_bind_host_rejects_wildcard_and_empty_hosts(bad_host: str) -> None:
    """Contract: SEC-003 forbids wildcard or empty APP_BIND_HOST values in Settings.

    Defect caught: Implicitly binding all network interfaces on startup.
    """
    with pytest.raises(ValidationError):
        Settings(app_bind_host=bad_host)


async def test_f01_dev_connect_route_locked_to_development_and_loopback() -> None:
    """Contract: POST /api/connect requires APP_ENV=development, loopback IP, session, and CSRF.

    Defect caught: Remote or production callers injecting TopstepX credentials over HTTP.
    """
    prod_settings = Settings(app_env="production", app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(prod_settings, client_ip="127.0.0.1") as (client, _):
        csrf = await _login_and_get_csrf(client)
        res = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert res.status_code == 403

    dev_settings = Settings(app_env="development", app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(dev_settings, client_ip="198.51.100.42") as (client, _):
        csrf = await _login_and_get_csrf(client)
        res = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert res.status_code == 403

    async with _async_client(dev_settings, client_ip="127.0.0.1") as (client, _):
        unauth_conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
        )
        assert unauth_conn.status_code == 401

        csrf = await _login_and_get_csrf(client)

        no_csrf_conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
        )
        assert no_csrf_conn.status_code == 403

        bad_csrf_conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
            headers={"X-CSRF-Token": "wrong-csrf-token"},
        )
        assert bad_csrf_conn.status_code == 403

        res = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["connected"] is True
        assert body["connection_state"] == "CONNECTED"
        serialized = str(body)
        assert "secret-key-123" not in serialized
        assert "jwt-secret-value-12345" not in serialized


async def test_f01_secret_redaction_and_hard_readonly_order_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract: Secrets are redacted in logs/status; zero order/position routes exist on app.

    Defect caught: Raw API keys/JWTs leaking in logs or status, or live order routes exposed.
    """
    raw_api_key = "super-secret-api-key-999"
    raw_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload"
    raw_tg = "123456:ABC-DEF-telegram-bot-token"

    sample = (
        f"Authorization: Bearer {raw_jwt} "
        f'payload={{"apiKey": "{raw_api_key}", "TELEGRAM_BOT_TOKEN": "{raw_tg}"}}'
    )
    redacted = redact_secrets(sample)
    assert raw_api_key not in redacted
    assert raw_jwt not in redacted
    assert raw_tg not in redacted
    assert "***REDACTED***" in redacted

    test_logger = logging.getLogger("test_redaction_probe")
    flt = SecretRedactionFilter(extra_secrets=(raw_api_key,))
    test_logger.addFilter(flt)
    try:
        with caplog.at_level(logging.WARNING, logger="test_redaction_probe"):
            test_logger.warning("Failed call apiKey=%s access_token=%s", raw_api_key, raw_jwt)
        assert raw_api_key not in caplog.text
        assert raw_jwt not in caplog.text
        assert "***REDACTED***" in caplog.text
    finally:
        test_logger.removeFilter(flt)

    settings = Settings(
        app_env="development",
        app_password_hash=TEST_PASSWORD_HASH,
        topstepx_api_key=raw_api_key,
    )
    async with _async_client(
        settings,
        transport=_mock_topstep_transport(fail_with_secret=raw_api_key),
    ) as (client, app):
        csrf = await _login_and_get_csrf(client)
        fail_conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": raw_api_key},
            headers={"X-CSRF-Token": csrf},
        )
        assert fail_conn.status_code == 401
        assert raw_api_key not in fail_conn.text

        status_resp = await client.get("/api/status")
        assert status_resp.status_code == 200
        status_json: dict[str, Any] = status_resp.json()
        assert raw_api_key not in str(status_json)
        assert TEST_PASSWORD_HASH not in str(status_json)
        assert {
            "connected",
            "connection_state",
            "live",
            "connected_at",
            "key_age_days",
            "key_rotation_due",
            "key_rotation_enforced",
            "has_env_credentials",
            "last_error",
            "data_quality",
        }.issubset(status_json.keys())
        assert set(status_json["data_quality"].keys()) == {
            "quarantined_bars",
            "corrupted_intervals",
            "truncated_intervals",
            "provider_gap_intervals",
            "by_issue_code",
        }

        order_probe = await client.post("/api/Order/place", json={})
        assert order_probe.status_code in {401, 403, 404, 405}

        route_paths = [getattr(route, "path", "").lower() for route in app.routes]
        assert not any(
            forbidden in p for p in route_paths for forbidden in ("order", "position", "hubs/user")
        )


def test_f01_descendant_logger_redaction_and_tracebacks_on_attached_root_handler() -> None:
    """Contract: Descendant loggers and newly attached root handlers never emit raw secrets.

    Defect caught: Ancestor logger filters bypassed on propagated uvicorn.error/worker logs.
    """
    raw_api_key = "descendant-secret-api-key-777"
    raw_bearer = "descendant-jwt-token-888"
    create_app(
        settings=Settings(
            app_password_hash=TEST_PASSWORD_HASH,
            topstepx_api_key=raw_api_key,
        )
    )

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    try:
        logging.getLogger("uvicorn.error").warning(
            "Upstream auth failed apiKey=%s Authorization: Bearer %s",
            raw_api_key,
            raw_bearer,
        )
        try:
            raise RuntimeError(f"Crash with secret {raw_api_key} and token={raw_bearer}")
        except RuntimeError:
            logging.getLogger("topstep_chart.worker").exception(
                "Worker exception with apiKey=%s", raw_api_key
            )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(prev_level)

    emitted = buf.getvalue()
    assert "***REDACTED***" in emitted
    assert raw_api_key not in emitted
    assert raw_bearer not in emitted


async def test_f01_validation_errors_and_non_ascii_csrf_never_leak_or_crash() -> None:
    """Contract: 422 validation errors strip raw input secrets; non-ASCII CSRF returns 403.

    Defect caught: FastAPI echoing raw submitted passwords in 422 JSON or 500 on non-ASCII CSRF.
    """
    settings = Settings(app_env="development", app_password_hash=TEST_PASSWORD_HASH)
    async with _async_client(settings, client_ip="127.0.0.1") as (client, _):
        malformed_login_1 = await client.post(
            "/api/auth/login",
            json=[{"password": "probe-actual-password-111"}],
        )
        assert malformed_login_1.status_code == 422
        assert "probe-actual-password-111" not in malformed_login_1.text

        malformed_login_2 = await client.post(
            "/api/auth/login",
            json={"password": {"private": "probe-actual-password-222"}},
        )
        assert malformed_login_2.status_code == 422
        assert "probe-actual-password-222" not in malformed_login_2.text

        csrf = await _login_and_get_csrf(client)

        malformed_connect = await client.post(
            "/api/connect",
            json=[{"apiKey": "probe-connect-secret-333"}],
            headers={"X-CSRF-Token": csrf},
        )
        assert malformed_connect.status_code == 422
        assert "probe-connect-secret-333" not in malformed_connect.text

        non_ascii_csrf = await client.post(
            "/api/disconnect",
            headers=[(b"x-csrf-token", "bài-kiểm-tra-không-ascii".encode())],
        )
        assert non_ascii_csrf.status_code == 403

        conn = await client.post(
            "/api/connect",
            json={"userName": "alice", "apiKey": "secret-key-999"},
            headers={"X-CSRF-Token": csrf},
        )
        assert conn.status_code == 200
        assert conn.json()["connected"] is True

        disc = await client.post("/api/disconnect", headers={"X-CSRF-Token": csrf})
        assert disc.status_code == 200
        assert disc.json()["connected"] is False
        assert disc.json()["connection_state"] == "DISCONNECTED"


def test_f01_uvicorn_access_formatter_preserves_5_args_and_redacts_secrets() -> None:
    """Contract: SecretRedactionFilter preserves 5-tuple record.args for Uvicorn AccessFormatter.

    Defect caught: Flattening record.args=() causing ValueError in Uvicorn AccessFormatter.
    """
    create_app(settings=Settings(app_password_hash=TEST_PASSWORD_HASH))

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AccessFormatter("%(client_addr)s - %(request_line)s %(status_code)s"))
    access_logger = logging.getLogger("uvicorn.access")
    prev_level = access_logger.level
    prev_propagate = access_logger.propagate
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    access_logger.addHandler(handler)
    try:
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:51234",
            "GET",
            "/api/contracts?apiKey=raw-access-key&token=raw-jwt-value",
            "1.1",
            200,
        )
    finally:
        access_logger.removeHandler(handler)
        access_logger.setLevel(prev_level)
        access_logger.propagate = prev_propagate

    output = buf.getvalue()
    assert "200" in output
    assert "raw-access-key" not in output
    assert "raw-jwt-value" not in output
    assert "***REDACTED***" in output


async def test_f01_login_rate_limits_failures_and_offloads_argon2_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Failed logins rate-limit at 429 and Argon2 verification runs off the event loop.

    Defect caught: CPU-heavy Argon2 blocking /api/health or unauthenticated brute-force flooding.
    """
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        login_rate_limit_attempts=2,
        login_rate_limit_window_sec=60.0,
        login_max_concurrency=2,
    )
    async with _async_client(settings, client_ip="127.0.0.1") as (client, app):
        for _ in range(2):
            bad = await client.post("/api/auth/login", json={"password": "wrong"})
            assert bad.status_code == 401

        limited = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert limited.status_code == 429

        # Test off-loop execution from a second IP: slow verify_password must not block /api/health
        orig_verify = app.state.session_manager.verify_password
        loop_tid = threading.get_ident()
        worker_tid: int | None = None
        verify_started = threading.Event()

        def slow_verify(pw: str) -> bool:
            nonlocal worker_tid
            worker_tid = threading.get_ident()
            verify_started.set()
            time.sleep(0.15)
            return orig_verify(pw)

        monkeypatch.setattr(app.state.session_manager, "verify_password", slow_verify)
        asgi_other = httpx.ASGITransport(app=app, client=("127.0.0.2", 54321))
        async with httpx.AsyncClient(transport=asgi_other, base_url="http://127.0.0.1:8000") as c2:
            t0 = time.monotonic()
            login_task = asyncio.create_task(
                c2.post("/api/auth/login", json={"password": TEST_PASSWORD})
            )
            while not verify_started.is_set():
                await asyncio.sleep(0.002)
            health_resp = await c2.get("/api/health")
            health_elapsed = time.monotonic() - t0
            assert health_resp.status_code == 200
            assert health_elapsed < 0.10
            ok_resp = await login_task
            assert ok_resp.status_code == 200
            assert worker_tid is not None and worker_tid != loop_tid


def test_f01_quoted_logging_templates_redact_positional_secret_args() -> None:
    """Contract: Quoted JSON-style logging templates redact positional secret arguments.

    Defect caught: Literal secrets leaking through quoted '%s' templates like '{"password": "%s"}'.
    """
    create_app(settings=Settings(app_password_hash=TEST_PASSWORD_HASH))
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    try:
        logging.getLogger("topstep_chart.audit").warning(
            '{"password": "%s"}',
            "quoted-secret-xyz",
        )
        logging.getLogger("topstep_chart.audit").warning(
            "{'apiKey': '%s'}",
            "single-quoted-secret-abc",
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(prev_level)

    emitted = buf.getvalue()
    assert "quoted-secret-xyz" not in emitted
    assert "single-quoted-secret-abc" not in emitted
    assert "***REDACTED***" in emitted


async def test_f01_login_guard_retains_permit_until_cancelled_worker_thread_exits() -> None:
    """Contract: Cancelling login does not release semaphore before the OS worker thread exits.

    Defect caught: Cancelled asyncio.to_thread calls exceeding login_max_concurrency.
    """
    guard = LoginGuard(max_concurrency=1)
    entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    active_workers = 0
    peak_workers = 0

    class _BlockingSessionManager(SessionManager):
        def verify_password(self, password: str) -> bool:
            nonlocal active_workers, peak_workers
            with count_lock:
                active_workers += 1
                peak_workers = max(peak_workers, active_workers)
            try:
                if password == "block":
                    entered.set()
                    release.wait(timeout=5.0)
                return True
            finally:
                with count_lock:
                    active_workers -= 1

    mgr = _BlockingSessionManager(TEST_PASSWORD_HASH)
    task1 = asyncio.create_task(guard.verify_password_offload(mgr, "block", acquire_timeout=1.0))
    await asyncio.to_thread(entered.wait, 2.0)

    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    # Worker thread #1 is still blocked; login #2 must NOT run concurrently above max_concurrency=1
    with pytest.raises(LoginRateLimitedError):
        await guard.verify_password_offload(mgr, "fast", acquire_timeout=0.05)
    assert peak_workers == 1

    release.set()
    await asyncio.sleep(0.05)
    assert await guard.verify_password_offload(mgr, "fast", acquire_timeout=1.0) is True
    assert peak_workers == 1


async def test_f01_load_settings_resolves_repo_root_env_and_authenticates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: DEFAULT_ENV_FILE targets topstep-chart-app/.env and load_settings() loads it.

    Defect caught: Wrong Path.parents index ignoring topstep-chart-app/.env at startup.
    """
    assert DEFAULT_ENV_FILE == Path(__file__).resolve().parents[2] / ".env"
    assert DEFAULT_ENV_FILE.parent.name == "topstep-chart-app"
    assert (DEFAULT_ENV_FILE.parent / ".env.example").is_file()

    env_pw = "repo-root-env-password"
    env_hash = PasswordHasher().hash(env_pw)
    tmp_env = tmp_path / ".env"
    tmp_env.write_text(f"APP_PASSWORD_HASH='{env_hash}'\n", encoding="utf-8")

    monkeypatch.setattr(config_mod, "DEFAULT_ENV_FILE", tmp_env)
    loaded = load_settings()
    assert loaded.app_password_hash == env_hash

    async with _async_client(loaded) as (client, _):
        resp = await client.post("/api/auth/login", json={"password": env_pw})
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True


def test_f01_quoted_kv_and_named_logging_placeholders_are_redacted() -> None:
    """Contract: Quoted key='val'/key=\"val\" assignments and named Bearer placeholders redact.

    Defect caught: Unmasked password=\"...\", apiKey='...', or Bearer %(credential)s logs.
    """
    direct = redact_secrets(
        "login failed password=\"probe-secret-value\" apiKey='probe-key-value'"
    )
    assert "probe-secret-value" not in direct
    assert "probe-key-value" not in direct
    assert "***REDACTED***" in direct

    create_app(settings=Settings(app_password_hash=TEST_PASSWORD_HASH))
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    try:
        logging.getLogger("topstep_chart.auth").warning(
            "Rejected password=\"probe-secret-value\" apiKey='probe-key-value'"
        )
        logging.getLogger("topstep_chart.auth").warning(
            "user=%(user)s Authorization: Bearer %(credential)s",
            {"user": "alice", "credential": "raw-jwt-named-999"},
        )
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(prev_level)

    emitted = buf.getvalue()
    assert "probe-secret-value" not in emitted
    assert "probe-key-value" not in emitted
    assert "raw-jwt-named-999" not in emitted
    assert "alice" in emitted
    assert "***REDACTED***" in emitted


async def test_f01_expired_session_returns_401_on_status() -> None:
    """Contract: Expired caller session cookies are rejected with 401 Unauthorized.

    Defect caught: SessionManager failing to evict or reject expired sessions.
    """
    settings = Settings(app_password_hash=TEST_PASSWORD_HASH, session_ttl_seconds=60)
    async with _async_client(settings) as (client, app):
        await _login_and_get_csrf(client)
        ok_status = await client.get("/api/status")
        assert ok_status.status_code == 200

        session_id = client.cookies["topstep_session"]
        active = app.state.session_manager._sessions[session_id]
        app.state.session_manager._sessions[session_id] = type(active)(
            session_id=active.session_id,
            csrf_token=active.csrf_token,
            created_at=active.created_at - timedelta(hours=2),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        expired_status = await client.get("/api/status")
        assert expired_status.status_code == 401


def test_f01_load_settings_parses_bracketed_ipv6_allowed_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Comma-separated bracketed IPv6 host lists do not crash JSON parsing.

    Defect caught: Leading '[' triggering json.loads() crash on APP_ALLOWED_HOSTS=[::1],localhost.
    """
    env_single = tmp_path / "single.env"
    env_single.write_text("APP_ALLOWED_HOSTS=[::1]\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_ENV_FILE", env_single)
    single_settings = load_settings()
    assert single_settings.app_allowed_hosts == ["[::1]"]

    env_multi = tmp_path / "multi.env"
    env_multi.write_text("APP_ALLOWED_HOSTS=[::1],localhost\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_ENV_FILE", env_multi)
    multi_settings = load_settings()
    assert multi_settings.app_allowed_hosts == ["[::1]", "localhost"]


def test_f01_redacts_embedded_quotes_escapes_and_basic_authorization() -> None:
    """Contract: Redaction handles embedded/escaped quotes and non-Bearer Authorization schemes.

    Defect caught: Partial suffix leak after embedded quote or Authorization: Basic credential leak.
    """
    embedded = redact_secrets('{"password":"prefix\'private-suffix-987"}')
    assert "private-suffix-987" not in embedded
    assert "prefix" not in embedded
    assert "***REDACTED***" in embedded

    escaped = redact_secrets('{"password":"prefix\\"escaped-secret-123"}')
    assert "escaped-secret-123" not in escaped
    assert "prefix" not in escaped
    assert "***REDACTED***" in escaped

    basic_auth = redact_secrets("Authorization: Basic cHJpdmF0ZS1wYXNzd29yZA==")
    assert "cHJpdmF0ZS1wYXNzd29yZA==" not in basic_auth
    assert basic_auth == "Authorization: ***REDACTED***"


def test_f01_redaction_is_linear_time_on_unterminated_backslashes_and_masks_basic_logs() -> None:
    """Contract: Redaction finishes in linear time on adversarial backslashes and masks Basic logs.

    Defect caught: Exponential ReDoS on unterminated quotes and unmasked Basic auth log args.
    """
    adversarial_kv = 'password="' + ("\\" * 100)
    adversarial_json = '{"password": "' + ("\\" * 100)
    unterminated_kv = 'password="private-value-987'
    unterminated_json = '{"password": "truncated-secret-999'

    t0 = time.monotonic()
    redacted_adv_kv = redact_secrets(adversarial_kv)
    redacted_adv_json = redact_secrets(adversarial_json)
    elapsed_direct = time.monotonic() - t0
    assert elapsed_direct < 0.05
    assert "\\" not in redacted_adv_kv
    assert "\\" not in redacted_adv_json
    assert "***REDACTED***" in redacted_adv_kv
    assert "***REDACTED***" in redacted_adv_json

    redacted_unterm_kv = redact_secrets(unterminated_kv)
    redacted_unterm_json = redact_secrets(unterminated_json)
    assert "private-value-987" not in redacted_unterm_kv
    assert "truncated-secret-999" not in redacted_unterm_json
    assert "***REDACTED***" in redacted_unterm_kv
    assert "***REDACTED***" in redacted_unterm_json

    create_app(settings=Settings(app_password_hash=TEST_PASSWORD_HASH))
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AccessFormatter("%(client_addr)s - %(request_line)s %(status_code)s"))
    access_logger = logging.getLogger("uvicorn.access")
    prev_level = access_logger.level
    prev_propagate = access_logger.propagate
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    access_logger.addHandler(handler)
    try:
        t1 = time.monotonic()
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:51234",
            "GET",
            f"/api/status?{adversarial_kv}",
            "1.1",
            401,
        )
        assert (time.monotonic() - t1) < 0.05
    finally:
        access_logger.removeHandler(handler)
        access_logger.setLevel(prev_level)
        access_logger.propagate = prev_propagate

    log_buf = io.StringIO()
    root_handler = logging.StreamHandler(log_buf)
    root_logger = logging.getLogger()
    prev_root_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(root_handler)
    try:
        logging.getLogger("topstep_chart.auth").warning(
            "Authorization: Basic %s",
            "raw-basic-pos-123",
        )
        logging.getLogger("topstep_chart.auth").warning(
            "Authorization: Basic %(credential)s",
            {"credential": "raw-basic-named-456"},
        )
    finally:
        root_logger.removeHandler(root_handler)
        root_logger.setLevel(prev_root_level)

    emitted = log_buf.getvalue()
    assert "raw-basic-pos-123" not in emitted
    assert "raw-basic-named-456" not in emitted
    assert emitted.count("***REDACTED***") >= 2


async def test_f01_concurrent_login_rechecks_rate_limit_after_permit_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Queued login rechecks failed-login rate limit after acquiring permit.

    Defect caught: Two concurrent wrong logins bypassing login_max_attempts=1 before failure record.
    """
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        login_rate_limit_attempts=1,
        login_rate_limit_window_sec=60.0,
        login_max_concurrency=1,
    )
    async with _async_client(settings, client_ip="127.0.0.1") as (client, app):
        call_count = 0
        count_lock = threading.Lock()
        first_entered = threading.Event()
        release_first = threading.Event()

        def slow_failing_verify(_pw: str) -> bool:
            nonlocal call_count
            with count_lock:
                call_count += 1
            first_entered.set()
            release_first.wait(timeout=2.0)
            return False

        monkeypatch.setattr(app.state.session_manager, "verify_password", slow_failing_verify)

        task1 = asyncio.create_task(client.post("/api/auth/login", json={"password": "wrong-1"}))
        while not first_entered.is_set():
            await asyncio.sleep(0.002)

        task2 = asyncio.create_task(client.post("/api/auth/login", json={"password": "wrong-2"}))
        await asyncio.sleep(0.02)
        release_first.set()

        r1, r2 = await asyncio.gather(task1, task2)
        assert sorted([r1.status_code, r2.status_code]) == [401, 429]
        assert call_count == 1


async def test_f01_alert_routes_require_session_csrf_and_redact_telegram_secrets() -> None:
    """Contract: /api/alerts/* enforces session/CSRF and redact_secrets masks bot URL & chat_id.

    Defect caught: Unauthenticated/missing-CSRF alert mutations or /bot<token> URL secret leaks.
    """
    bot_token = "987654321:AAH_super_secret_bot_token_value"
    chat_id = "-1009988776655"
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
    )

    raw_url_error = (
        f"POST https://api.telegram.org/bot{bot_token}/sendMessage "
        f'payload={{"chat_id": "{chat_id}", "TELEGRAM_CHAT_ID": "{chat_id}"}}'
    )
    redacted = redact_secrets(raw_url_error, (bot_token, chat_id))
    assert bot_token not in redacted
    assert chat_id not in redacted
    assert "/bot***REDACTED***/sendMessage" in redacted

    async with _async_client(settings) as (client, _):
        assert (await client.get("/api/alerts/status")).status_code == 401
        assert (await client.get("/api/alerts/history")).status_code == 401
        scanner_unauth = await client.post("/api/alerts/scanner", json={"enabled": False})
        assert scanner_unauth.status_code == 401
        assert (await client.post("/api/alerts/test")).status_code == 401

        csrf_token = await _login_and_get_csrf(client)
        assert (
            await client.post("/api/alerts/scanner", json={"enabled": False})
        ).status_code == 403
        assert (await client.post("/api/alerts/test")).status_code == 403

        ok_scanner = await client.post(
            "/api/alerts/scanner",
            json={"enabled": False},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert ok_scanner.status_code == 200
        assert ok_scanner.json()["scanner_enabled"] is False

        status_resp = await client.get("/api/alerts/status")
        assert status_resp.status_code == 200
        assert bot_token not in status_resp.text
        assert chat_id not in status_resp.text
