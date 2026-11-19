"""Value-first tests for F02 CME metadata, StorageDatasetKey, and contract resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.engine.instruments import INSTRUMENTS, StorageDatasetKey, get_instrument_spec
from app.main import create_app
from app.topstep.auth import TopstepAuthService
from app.topstep.errors import (
    AmbiguousContractError,
    ContractNotFoundError,
    KeyRotationRequiredError,
    TopstepAuthError,
)
from app.topstep.rest_client import resolve_contract


def test_f02_cme_instrument_specs_match_exchange_tick_contracts() -> None:
    """Contract: MES, MNQ, MYM, MGC specs match CME/CBOT/COMEX tick_size and tick_value.

    Defect caught: Incorrect tick arithmetic skewing backtest P&L and risk sizing.
    """
    expected = {
        "MES": (0.25, 1.25),
        "MNQ": (0.25, 0.50),
        "MYM": (1.00, 0.50),
        "MGC": (0.10, 1.00),
    }
    assert set(INSTRUMENTS.keys()) == set(expected.keys())
    for sym, (tick_size, tick_value) in expected.items():
        spec = get_instrument_spec(sym.lower())
        assert spec.root_symbol == sym
        assert spec.tick_size == tick_size
        assert spec.tick_value == tick_value

    with pytest.raises(ValueError):
        get_instrument_spec("UNKNOWN")


def test_f02_storage_dataset_key_isolates_expiries_modes_and_blocks_traversal() -> None:
    """Contract: StorageDatasetKey prevents sim/live or expiry collisions and rejects traversal.

    Defect caught: Mixing Z26/H27 or sim/live bars into one Parquet file or path traversal writes.
    """
    z26_sim = StorageDatasetKey(
        provider="topstepx",
        live_mode=False,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
    )
    h27_sim = StorageDatasetKey(
        provider="topstepx",
        live_mode=False,
        contract_id="CON.F.US.MES.H27",
        timeframe="1m",
    )
    z26_live = StorageDatasetKey(
        provider="topstepx",
        live_mode=True,
        contract_id="CON.F.US.MES.Z26",
        timeframe="1m",
    )

    assert z26_sim.relative_parquet_path() == Path("topstepx/sim/CON.F.US.MES.Z26/1m.parquet")
    assert h27_sim.relative_parquet_path() == Path("topstepx/sim/CON.F.US.MES.H27/1m.parquet")
    assert z26_live.relative_parquet_path() == Path("topstepx/live/CON.F.US.MES.Z26/1m.parquet")
    assert len(
        {
            z26_sim.relative_parquet_path(),
            h27_sim.relative_parquet_path(),
            z26_live.relative_parquet_path(),
        }
    ) == 3

    for bad_id in ("../evil", "..\\evil", "a/b", "", " CON.F.US.MES.Z26"):
        with pytest.raises(ValueError):
            StorageDatasetKey(
                provider="topstepx",
                live_mode=False,
                contract_id=bad_id,
                timeframe="1m",
            )


def test_f02_resolve_contract_enforces_strict_identity_without_fallback() -> None:
    """Contract: resolve_contract never falls back to unrelated symbols or substring collisions.

    Defect caught: Returning MGC/MNQ or MES for ES, or picking arbitrarily among multiple active.
    """
    unrelated = [
        {"id": "CON.F.US.MGC.Z26", "symbolId": "F.US.MGC", "activeContract": True},
        {"id": "CON.F.US.MNQ.Z26", "symbolId": "F.US.MNQ", "activeContract": True},
    ]
    with pytest.raises(ContractNotFoundError):
        resolve_contract("MES", unrelated)

    # Substring collision guard: ES must not match F.US.MES / CON.F.US.MES.Z26
    mes_only = [{"id": "CON.F.US.MES.Z26", "symbolId": "F.US.MES", "activeContract": True}]
    with pytest.raises(ContractNotFoundError):
        resolve_contract("ES", mes_only)

    # One active among multiple matching expiries -> picks active
    mixed_active = [
        {"id": "CON.F.US.MES.U26", "symbolId": "F.US.MES", "activeContract": False},
        {"id": "CON.F.US.MES.Z26", "symbolId": "F.US.MES", "activeContract": True},
        {"id": "CON.F.US.MGC.Z26", "symbolId": "F.US.MGC", "activeContract": True},
    ]
    chosen = resolve_contract("mes", mixed_active)
    assert chosen["id"] == "CON.F.US.MES.Z26"

    # Multiple active contracts -> AmbiguousContractError
    two_active = [
        {"id": "CON.F.US.MES.Z26", "symbolId": "F.US.MES", "activeContract": True},
        {"id": "CON.F.US.MES.H27", "symbolId": "F.US.MES", "activeContract": True},
    ]
    with pytest.raises(AmbiguousContractError):
        resolve_contract("MES", two_active)

    # Zero active, 1 exact match -> returns it; >1 exact inactive -> AmbiguousContractError
    one_inactive = [{"id": "CON.F.US.MYM.Z26", "symbolId": "F.US.MYM", "activeContract": False}]
    assert resolve_contract("MYM", one_inactive)["id"] == "CON.F.US.MYM.Z26"
    two_inactive = [
        {"id": "CON.F.US.MYM.U26", "symbolId": "F.US.MYM", "activeContract": False},
        {"id": "CON.F.US.MYM.Z26", "symbolId": "F.US.MYM", "activeContract": False},
    ]
    with pytest.raises(AmbiguousContractError):
        resolve_contract("MYM", two_inactive)


async def test_f02_key_rotation_soft_vs_enforced_and_jwt_auto_refresh() -> None:
    """Contract: Stale key blocks auth only when KEY_ROTATION_ENFORCE=true; JWT refreshes >= 23h.

    Defect caught: Ignoring key rotation enforcement or letting 24h JWT expire without refresh.
    """
    login_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        login_calls += 1
        return httpx.Response(200, json={"success": True, "token": f"jwt-{login_calls}"})

    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    stale_date = (now.date() - timedelta(days=30)).isoformat()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        soft_settings = Settings(
            topstepx_key_updated_at=stale_date,
            key_rotation_days=30,
            key_rotation_enforce=False,
            jwt_refresh_hours=23.0,
        )
        soft_auth = TopstepAuthService(soft_settings, client)
        await soft_auth.login("alice", "key-1", live=False, now=now)
        status = soft_auth.get_status(now=now)
        assert status["connected"] is True
        assert status["key_age_days"] == 30
        assert status["key_rotation_due"] is True
        assert status["key_rotation_enforced"] is False

        # Before 23h -> reuses cached JWT; at 23h -> refreshes automatically
        assert await soft_auth.ensure_token(now=now + timedelta(hours=22)) == "jwt-1"
        assert login_calls == 1
        assert await soft_auth.ensure_token(now=now + timedelta(hours=23)) == "jwt-2"
        assert login_calls == 2

        strict_settings = Settings(
            topstepx_key_updated_at=stale_date,
            key_rotation_days=30,
            key_rotation_enforce=True,
        )
        strict_auth = TopstepAuthService(strict_settings, client)
        with pytest.raises(KeyRotationRequiredError):
            await strict_auth.login("alice", "key-1", live=False, now=now)
        assert strict_auth.get_status(now=now)["connected"] is False


async def test_f02_contracts_endpoint_reports_per_symbol_typed_errors() -> None:
    """Contract: GET /api/contracts returns 503 disconnected and typed errors per symbol connected.

    Defect caught: Unhandled exception or unrelated contract leak on /api/contracts.
    """
    pw = "test-pass"
    pw_hash = PasswordHasher().hash(pw)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/Contract/search":
            body = request.content.decode("utf-8")
            if '"MES"' in body:
                return httpx.Response(
                    200,
                    json={
                        "contracts": [
                            {
                                "id": "CON.F.US.MES.Z26",
                                "symbolId": "F.US.MES",
                                "activeContract": True,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "contracts": [
                        {
                            "id": "CON.F.US.MGC.Z26",
                            "symbolId": "F.US.MGC",
                            "activeContract": True,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    settings = Settings(app_env="development", app_password_hash=pw_hash)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings=settings, http_client=upstream)
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(
                transport=asgi,
                base_url="http://127.0.0.1:8000",
            ) as client:
                login_resp = await client.post("/api/auth/login", json={"password": pw})
                csrf = login_resp.json()["csrf_token"]

                disconnected = await client.get("/api/contracts?symbols=MES")
                assert disconnected.status_code == 503

                conn = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": "key-1"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn.status_code == 200

                res = await client.get("/api/contracts?symbols=MES,MNQ")
                assert res.status_code == 200
                payload = res.json()["contracts"]
                assert payload["MES"]["selected"]["id"] == "CON.F.US.MES.Z26"
                assert payload["MES"]["error_type"] is None
                assert payload["MNQ"]["selected"] is None
                assert payload["MNQ"]["error_type"] == "ContractNotFoundError"


async def test_f02_failed_jwt_refresh_clears_stale_token_and_disconnects() -> None:
    """Contract: Failed JWT refresh clears stale _token and sets connection_state=DISCONNECTED.

    Defect caught: Reporting connected=True alongside connection_state=DISCONNECTED after 401.
    """
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"success": True, "token": "jwt-initial"})
        return httpx.Response(401, json={"errorMessage": "Revoked apiKey"})

    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    settings = Settings(jwt_refresh_hours=23.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1", now=now)
        assert auth.is_connected is True

        with pytest.raises(TopstepAuthError):
            await auth.ensure_token(now=now + timedelta(hours=23))

        assert auth.is_connected is False
        status = auth.get_status(now=now + timedelta(hours=23))
        assert status["connected"] is False
        assert status["connection_state"] == "DISCONNECTED"
