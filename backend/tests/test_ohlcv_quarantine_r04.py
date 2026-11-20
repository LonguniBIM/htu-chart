"""Value-first tests for R04 strict OHLCV validation and SQLite quarantine."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from argon2 import PasswordHasher

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
) -> None:
    """Contract: Malformed bars are quarantined, excluded from Parquet, and surfaced safely.

    Defect caught: Corrupted bars entering Parquet, marking interval COMPLETE, or leaking secrets.
    """
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
                            "o": 5901.0,
                            "h": 5903.0,
                            "l": 5900.0,
                            "c": 5902.5,
                            "v": -3,  # invalid: negative volume
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
                assert issue_codes == {"ohlc_bounds_violation", "negative_volume"}
                assert raw_secret not in str(issues)
                assert "***REDACTED***" in str(issues)

                # Check GET /api/status reflects quality summary without secrets
                status_resp = await client.get("/api/status")
                assert status_resp.status_code == 200
                status_json = status_resp.json()
                assert raw_secret not in str(status_json)
                dq = status_json["data_quality"]
                assert dq["quarantined_bars"] == 2
                assert dq["corrupted_intervals"] == 1
                assert dq["by_issue_code"] == {
                    "negative_volume": 1,
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
