"""Value-first tests for F01 caller auth, Host/Origin/CSRF, redaction, and read-only guard."""

from __future__ import annotations

import io
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.security import SecretRedactionFilter, redact_secrets, validate_ws_handshake

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
        csrf = await _login_and_get_csrf(client)
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
        assert set(status_json.keys()) == {
            "connected",
            "connection_state",
            "live",
            "connected_at",
            "key_age_days",
            "key_rotation_due",
            "key_rotation_enforced",
            "has_env_credentials",
            "last_error",
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
