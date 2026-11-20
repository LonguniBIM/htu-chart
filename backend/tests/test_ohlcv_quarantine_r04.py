"""Value-first tests for R04 strict OHLCV validation and SQLite quarantine."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher

from app.api import routes_market
from app.config import Settings
from app.engine.instruments import StorageDatasetKey
from app.engine.ohlcv import (
    DEFAULT_MIN_BAR_TIMESTAMP,
    extract_raw_bar_timestamp,
    parse_topstep_bar,
    validate_ohlcv_bar,
)
from app.main import create_app
from app.services.history_service import HistoryBackfillService
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.rest_client import TopstepRestClient

NOW_TS = int(datetime(2026, 11, 10, 15, 0, tzinfo=UTC).timestamp())


def _valid_raw() -> dict[str, Any]:
    return {
        "time": NOW_TS - 300,
        "open": 5950.25,
        "high": 5952.50,
        "low": 5949.75,
        "close": 5951.00,
        "volume": 120.0,
        "is_closed": True,
    }


def test_r04_validate_ohlcv_bar_strict_matrix() -> None:
    """Contract: Strict rejection of every malformed OHLCV variant and acceptance of volume=0.

    Defect caught: Missing bounds checks, boolean acceptance, or naive ISO timestamp coercion.
    """
    # parse_topstep_bar preserves None and missing keys without coercion
    wire_partial = {"t": NOW_TS - 60, "o": None, "h": 100.0, "c": 99.0, "v": 10}
    parsed = parse_topstep_bar(wire_partial)
    assert parsed["open"] is None
    assert "low" not in parsed

    non_mapping: Any = ["not", "a", "dict"]
    res_non_map = validate_ohlcv_bar(non_mapping, is_closed=True, now_utc=NOW_TS)
    assert res_non_map.is_valid is False
    assert res_non_map.issue_code == "invalid_record"

    # Valid with volume=0 and aware ISO timestamp
    valid_zero_vol = {
        "time": "2026-11-10T14:55:00Z",
        "open": 5950.0,
        "high": 5951.0,
        "low": 5949.0,
        "close": 5950.5,
        "volume": 0,
        "is_closed": True,
    }
    ok_res = validate_ohlcv_bar(valid_zero_vol, now_utc=NOW_TS)
    assert ok_res.is_valid is True
    assert ok_res.bar is not None
    assert ok_res.bar.volume == 0.0
    assert ok_res.bar.time == int(datetime(2026, 11, 10, 14, 55, tzinfo=UTC).timestamp())

    cases: list[tuple[dict[str, Any], str]] = [
        ({**_valid_raw(), "open": None}, "missing_price"),
        ({**_valid_raw(), "close": True}, "non_finite_price"),
        ({**_valid_raw(), "high": False}, "non_finite_price"),
        ({**_valid_raw(), "open": math.nan}, "non_finite_price"),
        ({**_valid_raw(), "high": math.inf}, "non_finite_price"),
        ({**_valid_raw(), "open": 10**400}, "non_finite_price"),
        ({**_valid_raw(), "low": 0.0}, "non_positive_price"),
        ({**_valid_raw(), "open": -10.0}, "non_positive_price"),
        ({**_valid_raw(), "low": 5950.75}, "ohlc_bounds_violation"),  # low > min(open, close)
        ({**_valid_raw(), "high": 5950.50}, "ohlc_bounds_violation"),  # high < max(open, close)
        ({**_valid_raw(), "low": 5960.0, "high": 5940.0}, "ohlc_bounds_violation"),
        ({**_valid_raw(), "volume": None}, "missing_volume"),
        ({**_valid_raw(), "volume": True}, "non_finite_volume"),
        ({**_valid_raw(), "volume": math.nan}, "non_finite_volume"),
        ({**_valid_raw(), "volume": 10**400}, "non_finite_volume"),
        ({**_valid_raw(), "volume": -1.0}, "negative_volume"),
        ({**_valid_raw(), "time": "2026-11-10T14:55:00"}, "naive_timestamp"),
        ({**_valid_raw(), "time": True}, "invalid_timestamp"),
        ({**_valid_raw(), "time": 1700000000.5}, "invalid_timestamp"),
        ({**_valid_raw(), "time": "2026-11-10T14:55:00.0000001Z"}, "invalid_timestamp"),
        ({**_valid_raw(), "time": DEFAULT_MIN_BAR_TIMESTAMP - 1}, "timestamp_out_of_bounds"),
        ({**_valid_raw(), "time": NOW_TS + 3600}, "timestamp_out_of_bounds"),
        ({**_valid_raw(), "is_closed": "yes"}, "invalid_is_closed"),
    ]
    for bad_bar, expected_code in cases:
        res = validate_ohlcv_bar(bad_bar, now_utc=NOW_TS)
        assert res.is_valid is False, f"Expected failure for {expected_code}"
        assert res.issue_code == expected_code

    assert extract_raw_bar_timestamp({"t": True}) is None
    assert extract_raw_bar_timestamp({"t": "2026-11-10T14:55:00.0000001Z"}) is None
    assert extract_raw_bar_timestamp({"t": "2026-11-10T14:55:00+00:00"}) == int(
        datetime(2026, 11, 10, 14, 55, tzinfo=UTC).timestamp()
    )


async def test_r04_end_to_end_quarantine_corrupts_coverage_and_exposes_redacted_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Malformed bars are quarantined, excluded from Parquet, and surfaced safely.

    Defect caught: Corrupted bars entering Parquet, marking interval COMPLETE, or leaking secrets.
    """
    monkeypatch.setattr(routes_market, "_utc_now_ts", lambda: NOW_TS)
    pw = "quarantine-test-pw"
    pw_hash = PasswordHasher().hash(pw)
    raw_secret = "secret-upstream-token-xyz123"
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    t1 = t0 + 60
    t2 = t0 + 120
    t_end = t0 + 180

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        {
                            "t": t0,
                            "o": 5900.0,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 10,
                        },
                        {
                            "t": t1,
                            "o": 5901.0,
                            "h": 5900.0,  # invalid: high < open
                            "l": 5899.0,
                            "c": 5900.5,
                            "v": 5,
                            "apiKey": raw_secret,
                        },
                        {
                            "t": t2,
                            "o": f"apiKey={raw_secret}",  # invalid price embedding raw secret
                            "h": 5903.0,
                            "l": 5900.0,
                            "c": 5902.5,
                            "v": -3,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "data",
    )
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
                conn = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": "dev-key-123"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn.status_code == 200

                bars_resp = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "1m",
                        "startTime": str(t0),
                        "endTime": str(t_end),
                    },
                )
                assert bars_resp.status_code == 200
                body = bars_resp.json()
                assert body["count"] == 1
                assert [b["time"] for b in body["bars"]] == [t0]
                assert body["quality"]["is_complete"] is False
                assert body["quality"]["quarantined_count"] == 2

                # Check SQLite quarantine and coverage directly
                key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
                sqlite_store: SqliteStore = app.state.sqlite_store
                assert await sqlite_store.is_interval_complete(key, t0, t_end) is False
                issues = await sqlite_store.list_quality_issues(key)
                assert len(issues) == 2
                issue_codes = {item["issue_code"] for item in issues}
                assert issue_codes == {"ohlc_bounds_violation", "non_finite_price"}
                for item in issues:
                    assert raw_secret not in item["detail"]
                    assert raw_secret not in item["raw_payload"]
                price_issue = next(i for i in issues if i["issue_code"] == "non_finite_price")
                assert "***REDACTED***" in price_issue["detail"]
                assert "***REDACTED***" in price_issue["raw_payload"]

                # Check GET /api/status reflects quality summary without secrets
                status_resp = await client.get("/api/status")
                assert status_resp.status_code == 200
                status_json = status_resp.json()
                assert raw_secret not in str(status_json)
                dq = status_json["data_quality"]
                assert dq["quarantined_bars"] == 2
                assert dq["corrupted_intervals"] == 1
                assert dq["by_issue_code"] == {
                    "non_finite_price": 1,
                    "ohlc_bounds_violation": 1,
                }

    # Verify direct HistoryBackfillService quarantine when called directly (including 10**400)
    async def overflow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        {
                            "t": t0,
                            "o": 5900.0,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 10,
                        },
                        {
                            "t": t1,
                            "o": 10**400,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 5,
                        },
                        {
                            "t": t2,
                            "o": 5901.0,
                            "h": 5903.0,
                            "l": 5900.0,
                            "c": 5902.5,
                            "v": 10**400,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(overflow_handler)) as upstream2:
        direct_sqlite = SqliteStore(tmp_path / "direct.db")
        direct_sqlite.initialize()
        try:
            direct_parquet = ParquetStore(tmp_path / "direct_data", sqlite_store=direct_sqlite)
            auth = TopstepAuthService(settings, upstream2)
            await auth.login("alice", "key-1")
            rest = TopstepRestClient(settings, auth, upstream2)
            svc = HistoryBackfillService(settings, rest, direct_parquet, direct_sqlite)
            res = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=t0,
                end_ts=t_end,
                now_utc=t_end + 120,
            )
            assert res.is_complete is False
            assert res.quarantined_count == 2
            assert [b.time for b in res.bars] == [t0]
            key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
            q_issues = await direct_sqlite.list_quality_issues(key)
            assert {item["issue_code"] for item in q_issues} == {
                "non_finite_price",
                "non_finite_volume",
            }
        finally:
            direct_sqlite.close()


async def test_r04_http_resample_volume_overflow_returns_200_and_deduplicates_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Post-resample Float64 overflow returns HTTP 200 and deduplicates SQLite issues.

    Defect caught: Resampled volume=inf causing HTTP 500 or inflating /api/status on cache reads.
    """
    monkeypatch.setattr(routes_market, "_utc_now_ts", lambda: NOW_TS)
    pw = "resample-overflow-pw"
    pw_hash = PasswordHasher().hash(pw)
    b_1400 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    b_1405 = b_1400 + 300

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        {
                            "t": b_1400,
                            "o": 5900.0,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 1e308,
                        },
                        {
                            "t": b_1400 + 60,
                            "o": 5901.0,
                            "h": 5903.0,
                            "l": 5900.0,
                            "c": 5902.0,
                            "v": 1e308,
                        },
                        {
                            "t": b_1405,
                            "o": 5902.0,
                            "h": 5904.0,
                            "l": 5901.0,
                            "c": 5903.0,
                            "v": 25.0,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "data",
    )
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
                conn = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": "key-1"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn.status_code == 200

                # First call backfills 1m, resamples 5m, quarantines overflow 14:00, returns 200
                r1 = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "resampleFrom": "1m",
                        "startTime": str(b_1400),
                        "endTime": str(b_1405),
                    },
                )
                assert r1.status_code == 200
                body1 = r1.json()
                assert [b["time"] for b in body1["bars"]] == [b_1405]
                assert body1["quality"]["is_complete"] is False
                assert body1["quality"]["quarantined_count"] == 1

                s1 = await client.get("/api/status")
                assert s1.status_code == 200
                assert s1.json()["data_quality"] == {
                    "quarantined_bars": 1,
                    "corrupted_intervals": 1,
                    "truncated_intervals": 0,
                    "provider_gap_intervals": 0,
                    "by_issue_code": {"non_finite_volume": 1},
                }

                # Second cached call also returns 200 and does NOT inflate /api/status counters
                r2 = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "resampleFrom": "1m",
                        "startTime": str(b_1400),
                        "endTime": str(b_1405),
                    },
                )
                assert r2.status_code == 200
                body2 = r2.json()
                assert [b["time"] for b in body2["bars"]] == [b_1405]
                assert body2["quality"]["is_complete"] is False
                assert body2["quality"]["quarantined_count"] == 1

                s2 = await client.get("/api/status")
                assert s2.status_code == 200
                assert s2.json()["data_quality"] == {
                    "quarantined_bars": 1,
                    "corrupted_intervals": 1,
                    "truncated_intervals": 0,
                    "provider_gap_intervals": 0,
                    "by_issue_code": {"non_finite_volume": 1},
                }


async def test_r04_quarantine_redacts_runtime_jwt_and_connect_api_key_in_ohlcv_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Quarantine redacts unlabeled runtime JWT and /api/connect apiKey in OHLCV fields.

    Defect caught: Unlabeled runtime JWT or dev-connect apiKey leaking into SQLite quarantine.
    """
    monkeypatch.setattr(routes_market, "_utc_now_ts", lambda: NOW_TS)
    pw = "runtime-redact-pw"
    pw_hash = PasswordHasher().hash(pw)
    runtime_api_key = "runtime-connect-apikey-secret999"
    runtime_jwt = "eyJhbGciOiJIUzI1NiJ9.runtime-topstep-jwt-payload.sig987654"
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    t1 = t0 + 60
    t2 = t0 + 120
    t_end = t0 + 180

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": runtime_jwt})
        if request.url.path == "/api/History/retrieveBars":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bars": [
                        {
                            "t": t0,
                            "o": 5900.0,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 10,
                        },
                        {
                            "t": t1,
                            "o": runtime_jwt,
                            "h": 5902.0,
                            "l": 5899.0,
                            "c": 5901.0,
                            "v": 10,
                        },
                        {
                            "t": t2,
                            "o": 5901.0,
                            "h": 5903.0,
                            "l": 5900.0,
                            "c": runtime_api_key,
                            "v": 10,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        topstepx_api_key="",
        data_dir=tmp_path / "data",
    )
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
                conn = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": runtime_api_key},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn.status_code == 200

                bars_resp = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "1m",
                        "startTime": str(t0),
                        "endTime": str(t_end),
                    },
                )
                assert bars_resp.status_code == 200
                body = bars_resp.json()
                assert body["count"] == 1
                assert [b["time"] for b in body["bars"]] == [t0]
                assert body["quality"]["is_complete"] is False
                assert body["quality"]["quarantined_count"] == 2

                key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
                issues = await app.state.sqlite_store.list_quality_issues(key)
                assert len(issues) == 2
                assert {item["issue_code"] for item in issues} == {"non_finite_price"}
                for item in issues:
                    assert runtime_jwt not in item["detail"]
                    assert runtime_api_key not in item["detail"]
                    assert runtime_jwt not in item["raw_payload"]
                    assert runtime_api_key not in item["raw_payload"]
                    assert "***REDACTED***" in item["detail"]
                    assert "***REDACTED***" in item["raw_payload"]

                status_resp = await client.get("/api/status")
                assert status_resp.status_code == 200
                status_json = status_resp.json()
                assert status_json["data_quality"]["quarantined_bars"] == 2
                assert runtime_jwt not in str(status_json)
                assert runtime_api_key not in str(status_json)


async def test_r04_quarantine_redacts_credentials_across_midflight_disconnect_key_swap_and_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Redaction scope masks secrets across mid-flight disconnect, key swap, and refresh.

    Defect caught: /api/disconnect, /api/connect, or JWT refresh clearing active secrets mid-flight.
    """
    monkeypatch.setattr(routes_market, "_utc_now_ts", lambda: NOW_TS)
    pw = "midflight-redact-pw"
    pw_hash = PasswordHasher().hash(pw)

    api_key_1 = "runtime-disconnect-apikey-111111"
    jwt_1 = "eyJhbGciOiJIUzI1NiJ9.disconnect-jwt-111111.sigA"
    api_key_2 = "runtime-old-rotate-apikey-222222"
    jwt_2 = "eyJhbGciOiJIUzI1NiJ9.old-rotate-jwt-222222.sigB"
    api_key_3 = "runtime-new-rotate-apikey-333333"
    jwt_3 = "eyJhbGciOiJIUzI1NiJ9.new-rotate-jwt-333333.sigC"
    jwt_4 = "eyJhbGciOiJIUzI1NiJ9.refreshed-jwt-444444.sigD"

    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    next_login_token = jwt_1
    bars_payload: list[dict[str, Any]] = []
    entered_evt = asyncio.Event()
    release_evt = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": next_login_token})
        if request.url.path == "/api/History/retrieveBars":
            entered_evt.set()
            await release_evt.wait()
            return httpx.Response(200, json={"success": True, "bars": bars_payload})
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        topstepx_api_key="",
        data_dir=tmp_path / "data",
        jwt_refresh_hours=23.0,
    )
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
                key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")

                # Sub-scenario A: Mid-flight /api/disconnect
                next_login_token = jwt_1
                conn1 = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": api_key_1},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn1.status_code == 200

                entered_evt.clear()
                release_evt.clear()
                bars_payload = [
                    {"t": t0, "o": 5900.0, "h": 5902.0, "l": 5899.0, "c": 5901.0, "v": 10},
                    {"t": t0 + 60, "o": jwt_1, "h": 5902.0, "l": 5899.0, "c": 5901.0, "v": 10},
                    {
                        "t": t0 + 120,
                        "o": 5901.0,
                        "h": 5903.0,
                        "l": 5900.0,
                        "c": api_key_1,
                        "v": 10,
                    },
                ]
                task_a = asyncio.create_task(
                    client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MES.Z26",
                            "timeframe": "1m",
                            "startTime": str(t0),
                            "endTime": str(t0 + 180),
                        },
                    )
                )
                await entered_evt.wait()
                disc_a = await client.post("/api/disconnect", headers={"X-CSRF-Token": csrf})
                assert disc_a.status_code == 200
                assert app.state.topstep_auth.is_connected is False
                assert app.state.topstep_auth.active_redaction_secrets() == ()

                release_evt.set()
                resp_a = await task_a
                assert resp_a.status_code == 200
                body_a = resp_a.json()
                assert body_a["count"] == 1
                assert body_a["quality"]["is_complete"] is False
                assert body_a["quality"]["quarantined_count"] == 2

                issues_a = await app.state.sqlite_store.list_quality_issues(key)
                assert len(issues_a) == 2
                assert {item["issue_code"] for item in issues_a} == {"non_finite_price"}
                for item in issues_a:
                    assert jwt_1 not in item["detail"]
                    assert api_key_1 not in item["detail"]
                    assert jwt_1 not in item["raw_payload"]
                    assert api_key_1 not in item["raw_payload"]
                    assert "***REDACTED***" in item["detail"]
                    assert "***REDACTED***" in item["raw_payload"]
                assert len(app.state.topstep_auth._active_scopes) == 0

                # Sub-scenario B: Mid-flight /api/connect key swap
                next_login_token = jwt_2
                conn2 = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": api_key_2},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn2.status_code == 200

                entered_evt.clear()
                release_evt.clear()
                b_start = t0 + 600
                bars_payload = [
                    {"t": b_start, "o": 5910.0, "h": 5912.0, "l": 5909.0, "c": 5911.0, "v": 10},
                    {
                        "t": b_start + 60,
                        "o": jwt_2,
                        "h": 5912.0,
                        "l": 5909.0,
                        "c": 5911.0,
                        "v": 10,
                    },
                    {
                        "t": b_start + 120,
                        "o": 5911.0,
                        "h": api_key_3,
                        "l": 5910.0,
                        "c": api_key_2,
                        "v": 10,
                    },
                ]
                task_b = asyncio.create_task(
                    client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MES.Z26",
                            "timeframe": "1m",
                            "startTime": str(b_start),
                            "endTime": str(b_start + 180),
                        },
                    )
                )
                await entered_evt.wait()
                next_login_token = jwt_3
                conn3 = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": api_key_3},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn3.status_code == 200

                release_evt.set()
                resp_b = await task_b
                assert resp_b.status_code == 200
                assert resp_b.json()["quality"]["quarantined_count"] == 2

                issues_b = await app.state.sqlite_store.list_quality_issues(key)
                assert len(issues_b) == 4
                new_issues_b = [item for item in issues_b if item["bar_time"] >= b_start]
                assert len(new_issues_b) == 2
                for item in new_issues_b:
                    for secret in (jwt_2, api_key_2, jwt_3, api_key_3):
                        assert secret not in item["detail"]
                        assert secret not in item["raw_payload"]
                    assert "***REDACTED***" in item["detail"]
                    assert "***REDACTED***" in item["raw_payload"]

                status_b = await client.get("/api/status")
                assert status_b.status_code == 200
                for secret in (jwt_2, api_key_2, jwt_3, api_key_3):
                    assert secret not in str(status_b.json())

                # Sub-scenario C: Mid-flight JWT auto-refresh + immediate /api/disconnect
                assert app.state.topstep_auth._token_acquired_at is not None
                app.state.topstep_auth._token_acquired_at -= timedelta(hours=24)
                next_login_token = jwt_4

                entered_evt.clear()
                release_evt.clear()
                c_start = t0 + 1200
                bars_payload = [
                    {"t": c_start, "o": 5920.0, "h": 5922.0, "l": 5919.0, "c": 5921.0, "v": 10},
                    {
                        "t": c_start + 60,
                        "o": jwt_4,
                        "h": 5922.0,
                        "l": 5919.0,
                        "c": 5921.0,
                        "v": 10,
                    },
                ]
                task_c = asyncio.create_task(
                    client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MES.Z26",
                            "timeframe": "1m",
                            "startTime": str(c_start),
                            "endTime": str(c_start + 180),
                        },
                    )
                )
                await entered_evt.wait()
                disc_c = await client.post("/api/disconnect", headers={"X-CSRF-Token": csrf})
                assert disc_c.status_code == 200
                assert app.state.topstep_auth.active_redaction_secrets() == ()

                release_evt.set()
                resp_c = await task_c
                assert resp_c.status_code == 200
                assert resp_c.json()["quality"]["quarantined_count"] == 1

                issues_c = await app.state.sqlite_store.list_quality_issues(key)
                new_issues_c = [item for item in issues_c if item["bar_time"] >= c_start]
                assert len(new_issues_c) == 1
                assert jwt_4 not in new_issues_c[0]["detail"]
                assert jwt_4 not in new_issues_c[0]["raw_payload"]
                assert "***REDACTED***" in new_issues_c[0]["detail"]
                assert "***REDACTED***" in new_issues_c[0]["raw_payload"]

                status_c = await client.get("/api/status")
                assert status_c.status_code == 200
                assert jwt_4 not in str(status_c.json())
                assert len(app.state.topstep_auth._active_scopes) == 0
