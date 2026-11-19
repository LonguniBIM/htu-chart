"""Value-first tests for R03 timeouts, deadlines, bounded concurrency, retries, and startup."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.main import create_app
from app.topstep.auth import TopstepAuthService, build_httpx_timeout
from app.topstep.errors import TopstepAuthError, UpstreamHTTPError, UpstreamTimeoutError
from app.topstep.rest_client import TopstepRestClient


class _StallingBodyStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"contracts": ['
        await asyncio.sleep(5.0)
        yield b"]}"


def test_r03_httpx_timeout_uses_explicit_settings_values() -> None:
    """Contract: build_httpx_timeout applies explicit connect/read/write/pool bounds.

    Defect caught: Falling back to unbounded or default httpx timeouts.
    """
    settings = Settings(
        upstream_connect_timeout_sec=3.5,
        upstream_read_timeout_sec=12.0,
        upstream_write_timeout_sec=8.0,
        upstream_pool_timeout_sec=4.0,
    )
    timeout = build_httpx_timeout(settings)
    assert timeout.connect == 3.5
    assert timeout.read == 12.0
    assert timeout.write == 8.0
    assert timeout.pool == 4.0


async def test_r03_stalled_response_body_raises_upstream_timeout_within_deadline() -> None:
    """Contract: Total operation deadline aborts stalled response body reads promptly.

    Defect caught: Hung body reads stalling FastAPI handlers indefinitely.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        return httpx.Response(200, stream=_StallingBodyStream())

    settings = Settings(
        upstream_total_timeout_sec=0.15,
        upstream_max_retries=2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        started = time.monotonic()
        with pytest.raises(UpstreamTimeoutError):
            await rest.search_contracts("MES")
        elapsed = time.monotonic() - started
        assert elapsed < 0.6


async def test_r03_semaphore_bounds_concurrency_and_retries_transient_503_only() -> None:
    """Contract: Semaphore caps concurrent upstream calls; 503 retries while 400 fails fast.

    Defect caught: Unbounded upstream fan-out or retrying permanent 4xx errors.
    """
    active = 0
    peak_active = 0
    search_attempts = 0
    bad_req_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak_active, search_attempts, bad_req_attempts
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})

        body = request.content.decode("utf-8")
        if '"RETRY_TEST"' in body:
            search_attempts += 1
            if search_attempts == 1:
                return httpx.Response(503, json={"errorMessage": "Service busy"})
            return httpx.Response(200, json={"contracts": []})

        if '"BAD_REQ"' in body:
            bad_req_attempts += 1
            return httpx.Response(400, json={"errorMessage": "Invalid searchText"})

        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"contracts": []})
        finally:
            active -= 1

    settings = Settings(
        upstream_max_concurrency=2,
        upstream_max_retries=2,
        upstream_total_timeout_sec=5.0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        await asyncio.gather(*(rest.search_contracts(f"SYM{i}") for i in range(8)))
        assert peak_active <= 2

        retried = await rest.search_contracts("RETRY_TEST")
        assert retried == []
        assert search_attempts == 2

        with pytest.raises(UpstreamHTTPError) as exc_info:
            await rest.search_contracts("BAD_REQ")
        assert exc_info.value.status_code == 400
        assert bad_req_attempts == 1


async def test_r03_non_blocking_lifespan_starts_disconnected_when_login_hangs() -> None:
    """Contract: Hanging TopstepX auto-connect does not delay startup and leaves DISCONNECTED state.

    Defect caught: Blocking uvicorn startup on hung upstream loginKey calls.
    """
    pw = "startup-test-pw"
    pw_hash = PasswordHasher().hash(pw)
    login_started = asyncio.Event()
    release_login = asyncio.Event()

    async def hanging_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            login_started.set()
            await release_login.wait()
            return httpx.Response(200, json={"success": True, "token": "late-jwt"})
        return httpx.Response(404)

    settings = Settings(
        app_password_hash=pw_hash,
        topstepx_username="alice",
        topstepx_api_key="key-123456",
        upstream_total_timeout_sec=10.0,
    )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging_handler)) as upstream:
            app = create_app(settings=settings, http_client=upstream)
            started = time.monotonic()
            async with app.router.lifespan_context(app):
                startup_elapsed = time.monotonic() - started
                assert startup_elapsed < 0.5

                await asyncio.sleep(0)
                asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
                async with httpx.AsyncClient(
                    transport=asgi, base_url="http://127.0.0.1:8000"
                ) as client:
                    login_resp = await client.post("/api/auth/login", json={"password": pw})
                    assert login_resp.status_code == 200
                    status_resp = await client.get("/api/status")
                    assert status_resp.status_code == 200
                    body = status_resp.json()
                    assert body["connected"] is False
                    assert body["connection_state"] == "DISCONNECTED"
                    assert login_started.is_set()
    finally:
        release_login.set()


async def test_r03_login_retries_transient_503_and_fails_fast_on_401() -> None:
    """Contract: TopstepAuthService.login retries transient 5xx/timeouts, not 401/403.

    Defect caught: Permanent DISCONNECTED startup on transient 503 or retrying bad credentials.
    """
    mode = "retry_then_ok"
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if mode == "retry_then_ok":
            if attempts == 1:
                return httpx.Response(503, json={"errorMessage": "Upstream busy"})
            return httpx.Response(200, json={"success": True, "token": "jwt-recovered"})
        if mode == "always_401":
            return httpx.Response(401, json={"errorMessage": "Invalid key"})
        if mode == "always_503":
            return httpx.Response(503, json={"errorMessage": "Still down"})
        raise httpx.ReadTimeout("socket read timed out")

    settings = Settings(upstream_max_retries=2, upstream_total_timeout_sec=5.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        assert attempts == 2
        assert auth.is_connected is True

        mode = "always_401"
        attempts = 0
        with pytest.raises(TopstepAuthError):
            await auth.login("alice", "bad-key")
        assert attempts == 1
        assert auth.is_connected is False

        mode = "always_503"
        attempts = 0
        with pytest.raises(UpstreamHTTPError):
            await auth.login("alice", "key-1")
        assert attempts == 3
        assert auth.is_connected is False

        mode = "always_timeout"
        attempts = 0
        with pytest.raises(UpstreamTimeoutError):
            await auth.login("alice", "key-1")
        assert attempts == 3
        assert auth.is_connected is False


async def test_r03_total_deadline_includes_semaphore_wait_for_queued_calls() -> None:
    """Contract: Total operation deadline caps queued semaphore waiting plus HTTP execution.

    Defect caught: Queued calls bypassing upstream_total_timeout_sec while waiting on semaphore.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        await asyncio.sleep(0.09)
        return httpx.Response(200, json={"contracts": []})

    settings = Settings(
        upstream_max_concurrency=1,
        upstream_total_timeout_sec=0.12,
        upstream_max_retries=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        started = time.monotonic()
        results = await asyncio.gather(
            *(rest.search_contracts(f"SYM{i}") for i in range(4)),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 0.25
        successes = [r for r in results if isinstance(r, list)]
        timeouts = [r for r in results if isinstance(r, UpstreamTimeoutError)]
        assert len(successes) == 1
        assert len(timeouts) == 3


async def test_r03_lifespan_shutdown_cancels_auth_task_and_closes_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Lifespan shutdown cancels pending auto-connect task and closes owned AsyncClient.

    Defect caught: Leaked background tasks or unclosed httpx connection pools on app shutdown.
    """
    release_login = asyncio.Event()

    async def hanging_handler(_request: httpx.Request) -> httpx.Response:
        await release_login.wait()
        return httpx.Response(200, json={"success": True, "token": "late"})

    created_clients: list[httpx.AsyncClient] = []
    orig_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client = orig_async_client(transport=httpx.MockTransport(hanging_handler))
        created_clients.append(client)
        return client

    monkeypatch.setattr("app.main.httpx.AsyncClient", fake_async_client)

    settings = Settings(
        topstepx_username="alice",
        topstepx_api_key="key-123456",
        upstream_total_timeout_sec=10.0,
    )
    app = create_app(settings=settings)
    try:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            task = app.state.auto_connect_task
            assert task is not None and not task.done()
            assert len(created_clients) == 1
            assert created_clients[0].is_closed is False
        assert task.done()
        assert task.cancelled()
        assert created_clients[0].is_closed is True
    finally:
        release_login.set()
