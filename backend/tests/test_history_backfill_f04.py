"""Value-first tests for F04 paginated history backfill, coverage intervals, and /api/bars."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher

from app.api import routes_market
from app.config import Settings
from app.engine.instruments import StorageDatasetKey
from app.engine.session_calendar import NY_TZ
from app.main import create_app
from app.services.history_service import HistoryBackfillService
from app.storage.parquet_store import ParquetStore
from app.storage.sqlite_store import SqliteStore
from app.topstep.auth import TopstepAuthService
from app.topstep.errors import TopstepAuthError, UpstreamHTTPError, UpstreamTimeoutError
from app.topstep.rest_client import TopstepRestClient

BASE_T = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())


def _wire_bar(ts: int, price: float = 5950.0) -> dict[str, object]:
    return {
        "t": ts,
        "o": price,
        "h": price + 2.0,
        "l": price - 1.0,
        "c": price + 1.0,
        "v": 15,
    }


async def test_f04_three_page_backward_pagination_and_max_pages_truncation(
    tmp_path: Path,
) -> None:
    """Contract: Backward pagination fetches all pages cleanly and flags max_pages truncation.

    Defect caught: Single-call truncation at limit or silent loss when max_pages is hit.
    """
    all_bars = [_wire_bar(BASE_T + i * 60, 5900.0 + i * 0.25) for i in range(250)]
    start_ts = BASE_T
    end_ts = BASE_T + 250 * 60
    captured_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            captured_payloads.append(payload)
            req_end = int(datetime.fromisoformat(str(payload["endTime"])).timestamp())
            req_start = int(datetime.fromisoformat(str(payload["startTime"])).timestamp())
            limit = int(payload["limit"])
            eligible = [b for b in all_bars if req_start <= int(b["t"]) <= req_end]
            page = eligible[-limit:]
            return httpx.Response(200, json={"success": True, "bars": page})
        return httpx.Response(404)

    settings = Settings(history_page_limit=100, history_max_pages=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        full = await rest.retrieve_bars_paginated(
            contract_id="CON.F.US.MES.Z26",
            live=False,
            timeframe="1m",
            start_ts=start_ts,
            end_ts=end_ts,
            page_limit=100,
            max_pages=10,
        )
        assert full.pages_fetched == 3
        assert len(full.raw_bars) == 250
        assert full.truncated is False
        assert full.provider_gap is False
        assert full.earliest_bar_time == BASE_T
        assert full.latest_bar_time == BASE_T + 249 * 60
        assert captured_payloads[0]["includePartialBar"] is True
        assert captured_payloads[1]["includePartialBar"] is False
        assert captured_payloads[2]["includePartialBar"] is False

    # Cap max_pages=2 via HistoryBackfillService -> TRUNCATED coverage recorded
    trunc_settings = Settings(
        data_dir=tmp_path / "data",
        history_page_limit=100,
        history_max_pages=2,
    )
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(tmp_path / "data", sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(trunc_settings, client)
            await auth.login("alice", "key-1")
            rest = TopstepRestClient(trunc_settings, auth, client)
            svc = HistoryBackfillService(trunc_settings, rest, parquet_store, sqlite_store)

            res = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=start_ts,
                end_ts=end_ts,
                now_utc=end_ts + 120,
            )
            assert res.truncated is True
            assert res.is_complete is False
            assert len(res.bars) == 200
            key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "1m")
            assert await sqlite_store.is_interval_complete(key, start_ts, end_ts) is False
            intervals = await sqlite_store.get_coverage_intervals(key)
            assert any(iv.status == "TRUNCATED" for iv in intervals)
    finally:
        sqlite_store.close()


async def test_f04_non_progressing_and_empty_open_vs_weekend_gap_handling() -> None:
    """Contract: Non-progressing bars halt immediately; open vs weekend sets provider_gap.

    Defect caught: Infinite pagination loops or silent gap acceptance during open market hours.
    """
    call_count = 0
    mode = "non_progressing"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            call_count += 1
            if mode == "non_progressing":
                # Always returns same single timestamp > start_ts with limit=1
                return httpx.Response(
                    200, json={"success": True, "bars": [_wire_bar(BASE_T + 300)]}
                )
            if mode == "empty_open_followup":
                if call_count == 1:
                    return httpx.Response(
                        200, json={"success": True, "bars": [_wire_bar(BASE_T + 180)]}
                    )
                return httpx.Response(200, json={"success": True, "bars": []})
            if mode == "empty_weekend":
                return httpx.Response(200, json={"success": True, "bars": []})
        return httpx.Response(404)

    settings = Settings(history_page_limit=1, history_max_pages=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        # 1. Non-progressing timestamps halt on page 2 without infinite loop
        call_count = 0
        non_prog = await rest.retrieve_bars_paginated(
            contract_id="CON.F.US.MES.Z26",
            live=False,
            timeframe="1m",
            start_ts=BASE_T,
            end_ts=BASE_T + 600,
            page_limit=1,
            max_pages=10,
        )
        assert call_count == 2
        assert non_prog.provider_gap is True
        assert non_prog.truncated is True

        # 2. Empty follow-up during open CME session -> provider_gap=True
        mode = "empty_open_followup"
        call_count = 0
        open_gap = await rest.retrieve_bars_paginated(
            contract_id="CON.F.US.MES.Z26",
            live=False,
            timeframe="1m",
            start_ts=BASE_T,
            end_ts=BASE_T + 300,
            page_limit=1,
            max_pages=10,
        )
        assert open_gap.provider_gap is True

        # 3. Empty weekend range (Saturday 2026-11-14) -> provider_gap=False
        mode = "empty_weekend"
        call_count = 0
        sat_start = int(datetime(2026, 11, 14, 10, 0, tzinfo=NY_TZ).timestamp())
        sat_end = int(datetime(2026, 11, 14, 16, 0, tzinfo=NY_TZ).timestamp())
        weekend = await rest.retrieve_bars_paginated(
            contract_id="CON.F.US.MES.Z26",
            live=False,
            timeframe="1m",
            start_ts=sat_start,
            end_ts=sat_end,
            page_limit=100,
            max_pages=10,
        )
        assert weekend.provider_gap is False
        assert weekend.truncated is False


async def test_f04_incremental_cache_hit_partial_extension_and_provisional_upgrade(
    tmp_path: Path,
) -> None:
    """Contract: Complete cache uses 0 upstream calls; open bar stays provisional until close.

    Defect caught: Re-fetching cached ranges or permanently marking active bar interval COMPLETE.
    """
    history_calls: list[tuple[int, int]] = []
    bar_0 = int(datetime(2026, 11, 10, 13, 55, tzinfo=UTC).timestamp())
    bar_1 = bar_0 + 300  # 14:00:00..14:05:00
    bar_2 = bar_1 + 300  # 14:05:00..14:10:00
    current_close_for_bar1 = 5951.0

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            payload = json.loads(request.content.decode("utf-8"))
            req_s = int(datetime.fromisoformat(str(payload["startTime"])).timestamp())
            req_e = int(datetime.fromisoformat(str(payload["endTime"])).timestamp())
            history_calls.append((req_s, req_e))
            candidates = [
                _wire_bar(bar_0, 5940.0),
                {
                    "t": bar_1,
                    "o": 5950.0,
                    "h": 5955.0,
                    "l": 5949.0,
                    "c": current_close_for_bar1,
                    "v": 40,
                },
                _wire_bar(bar_2, 5960.0),
            ]
            matched = [b for b in candidates if req_s <= int(b["t"]) <= req_e]
            return httpx.Response(200, json={"success": True, "bars": matched})
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(tmp_path / "data", sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-1")
            rest = TopstepRestClient(settings, auth, client)
            svc = HistoryBackfillService(settings, rest, parquet_store, sqlite_store)

            # Query during active bar_1 (now = 14:02:00) with include_partial=True
            res1 = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="5m",
                start_ts=bar_0,
                end_ts=bar_1 + 120,
                include_partial=True,
                now_utc=bar_1 + 120,
            )
            assert [(b.time, b.is_closed) for b in res1.bars] == [(bar_0, True), (bar_1, False)]

            # Closed range [bar_0, bar_1] is cached -> 0 upstream calls when querying [bar_0, bar_1]
            calls_before_hit = len(history_calls)
            res_hit = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="5m",
                start_ts=bar_0,
                end_ts=bar_1,
                include_partial=False,
                now_utc=bar_1 + 120,
            )
            assert len(history_calls) == calls_before_hit
            assert res_hit.upstream_requests == 0
            assert res_hit.is_complete is True
            assert [b.time for b in res_hit.bars] == [bar_0]

            # Advance clock past bar_1 close (14:05:10) and update close -> upgrades to closed
            current_close_for_bar1 = 5954.25
            calls_before_upgrade = len(history_calls)
            res_up = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="5m",
                start_ts=bar_0,
                end_ts=bar_2,
                include_partial=False,
                now_utc=bar_2 + 10,
            )
            assert len(history_calls) == calls_before_upgrade + 1
            # Only missing closed sub-range [bar_1, bar_2] was fetched
            assert history_calls[-1] == (bar_1, bar_2)
            assert [(b.time, b.is_closed, b.close) for b in res_up.bars] == [
                (bar_0, True, 5941.0),
                (bar_1, True, 5954.25),
            ]

            # Verify heal_existing=False vs True on SqliteStore non-COMPLETE protection
            key = StorageDatasetKey("topstepx", False, "CON.F.US.MES.Z26", "5m")
            await sqlite_store.record_coverage(
                key, bar_0, bar_1, status="TRUNCATED", truncated=True
            )
            await sqlite_store.record_coverage(
                key, bar_0, bar_2, status="COMPLETE", heal_existing=False
            )
            assert await sqlite_store.is_interval_complete(key, bar_0, bar_2) is False
            await sqlite_store.record_coverage(
                key, bar_0, bar_2, status="COMPLETE", heal_existing=True
            )
            assert await sqlite_store.is_interval_complete(key, bar_0, bar_2) is True
    finally:
        sqlite_store.close()


async def test_f04_bars_http_endpoint_auth_validation_offline_and_resample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: /api/bars enforces 401/400/503/504, offline cache, resample, and refresh errors.

    Defect caught: Unauthenticated bars access, traversal 500s, clock flakiness, or offline bugs.
    """
    pw = "bars-endpoint-pw"
    pw_hash = PasswordHasher().hash(pw)
    login_mode = "ok"
    history_mode = "ok"
    t0 = int(datetime(2026, 11, 10, 14, 0, tzinfo=UTC).timestamp())
    bars_1m = [_wire_bar(t0 + i * 60, 5900.0 + i) for i in range(5)]
    t_end = t0 + 300

    # Deterministic current UTC clock inside an active 5m bar (15:02:00 UTC)
    fixed_now = int(datetime(2026, 11, 10, 15, 2, 0, tzinfo=UTC).timestamp())
    open_5m_start = int(datetime(2026, 11, 10, 15, 0, 0, tzinfo=UTC).timestamp())
    closed_5m_start = open_5m_start - 300
    live_end = open_5m_start + 300
    monkeypatch.setattr(routes_market, "_utc_now_ts", lambda: fixed_now)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            if login_mode == "fail":
                return httpx.Response(401, json={"errorMessage": "Revoked"})
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            if history_mode == "timeout":
                await asyncio.sleep(0.3)
                return httpx.Response(200, json={"success": True, "bars": []})
            payload = json.loads(request.content.decode("utf-8"))
            req_s = int(datetime.fromisoformat(str(payload["startTime"])).timestamp())
            req_e = int(datetime.fromisoformat(str(payload["endTime"])).timestamp())
            pool = bars_1m + [
                _wire_bar(closed_5m_start, 6000.0),
                _wire_bar(open_5m_start, 6010.0),
            ]
            matched = [b for b in pool if req_s <= int(b["t"]) <= req_e]
            return httpx.Response(200, json={"success": True, "bars": matched})
        return httpx.Response(404)

    settings = Settings(
        app_env="development",
        app_password_hash=pw_hash,
        data_dir=tmp_path / "data",
        jwt_refresh_hours=23.0,
        history_backfill_timeout_sec=0.1,
        upstream_max_retries=0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings=settings, http_client=upstream)
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(
                transport=asgi,
                base_url="http://127.0.0.1:8000",
            ) as client:
                # 401 unauthenticated
                assert (
                    await client.get("/api/bars", params={"contractId": "CON.F.US.MES.Z26"})
                ).status_code == 401

                login_resp = await client.post("/api/auth/login", json={"password": pw})
                csrf = login_resp.json()["csrf_token"]

                # 400 validation checks (even while disconnected)
                assert (
                    await client.get("/api/bars", params={"contractId": "../evil"})
                ).status_code == 400
                assert (
                    await client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MES.Z26",
                            "startTime": str(t_end),
                            "endTime": str(t0),
                        },
                    )
                ).status_code == 400
                assert (
                    await client.get(
                        "/api/bars",
                        params={"contractId": "CON.F.US.MES.Z26", "timeframe": "2m"},
                    )
                ).status_code == 400
                assert (
                    await client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MES.Z26",
                            "timeframe": "5m",
                            "resampleFrom": "15m",
                        },
                    )
                ).status_code == 400
                assert (
                    await client.get(
                        "/api/bars",
                        params={
                            "contractId": "CON.F.US.MGC.Z26",
                            "timeframe": "1m",
                            "rootSymbol": "MES",
                            "startTime": str(t0),
                            "endTime": str(t_end),
                        },
                    )
                ).status_code == 400

                # Offline uncached -> 503
                uncached = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "1m",
                        "startTime": str(t0),
                        "endTime": str(t_end),
                    },
                )
                assert uncached.status_code == 503

                # Connect TopstepX, backfill 1m, then query resampled 5m on mid-bucket slice
                conn = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": "key-1"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn.status_code == 200

                resampled = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "resampleFrom": "1m",
                        "startTime": str(t0 + 120),
                        "endTime": str(t0 + 240),
                    },
                )
                assert resampled.status_code == 200
                r_body = resampled.json()
                assert r_body["count"] == 1
                assert r_body["bars"][0]["time"] == t0
                assert r_body["bars"][0]["open"] == 5900.0
                assert r_body["bars"][0]["close"] == 5905.0
                assert r_body["quality"]["is_complete"] is True

                # includePartial=false vs true around deterministic active 5m bar
                partial_off = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "startTime": str(closed_5m_start),
                        "endTime": str(live_end),
                        "includePartial": "false",
                    },
                )
                assert partial_off.status_code == 200
                off_bars = partial_off.json()["bars"]
                assert [(b["time"], b["is_closed"]) for b in off_bars] == [
                    (closed_5m_start, True)
                ]

                partial_on = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "startTime": str(closed_5m_start),
                        "endTime": str(live_end),
                        "includePartial": "true",
                    },
                )
                assert partial_on.status_code == 200
                p_bars = partial_on.json()["bars"]
                assert [(b["time"], b["is_closed"]) for b in p_bars] == [
                    (closed_5m_start, True),
                    (open_5m_start, False),
                ]

                # Backfill deadline -> HTTP 504 when connected
                history_mode = "timeout"
                timeout_resp = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MGC.Z26",
                        "timeframe": "1m",
                        "startTime": str(t0),
                        "endTime": str(t_end),
                    },
                )
                assert timeout_resp.status_code == 504
                history_mode = "ok"

                # Disconnect -> cached COMPLETE 1m/5m range still serves 200 offline
                disc = await client.post("/api/disconnect", headers={"X-CSRF-Token": csrf})
                assert disc.status_code == 200
                offline_cached = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MES.Z26",
                        "timeframe": "5m",
                        "resampleFrom": "1m",
                        "startTime": str(t0),
                        "endTime": str(t0 + 240),
                    },
                )
                assert offline_cached.status_code == 200
                assert offline_cached.json()["count"] == 1

                # Reconnect, age token, and verify JWT refresh failure -> 503 on uncached /api/bars
                conn2 = await client.post(
                    "/api/connect",
                    json={"userName": "alice", "apiKey": "key-1"},
                    headers={"X-CSRF-Token": csrf},
                )
                assert conn2.status_code == 200
                login_mode = "fail"
                app.state.topstep_auth._token_acquired_at -= timedelta(hours=23, minutes=5)
                refresh_fail = await client.get(
                    "/api/bars",
                    params={
                        "contractId": "CON.F.US.MNQ.Z26",
                        "timeframe": "1m",
                        "startTime": str(t0),
                        "endTime": str(t_end),
                    },
                )
                assert refresh_fail.status_code == 503

                # Hard read-only route invariant
                route_paths = [getattr(route, "path", "").lower() for route in app.routes]
                assert not any(
                    bad in p for p in route_paths for bad in ("order", "position", "hubs/user")
                )


async def test_f04_end_to_end_healing_recovery_and_extreme_timestamp_quarantine(
    tmp_path: Path,
) -> None:
    """Contract: Clean retry heals PROVIDER_GAP/CORRUPTED; 10**30 timestamps quarantine safely.

    Defect caught: Unhealed intervals stuck incomplete forever, or OverflowError crashing backfill.
    """
    scenario = "provider_gap"
    t0 = BASE_T
    t1 = t0 + 60
    t2 = t0 + 120
    t_end = t0 + 180
    upstream_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            upstream_calls += 1
            if scenario == "provider_gap":
                return httpx.Response(200, json={"success": True, "bars": []})
            if scenario == "corrupted_extreme_ts":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bars": [
                            _wire_bar(t0, 5900.0),
                            {
                                "t": 10**30,
                                "o": 5901.0,
                                "h": 5902.0,
                                "l": 5900.0,
                                "c": 5901.5,
                                "v": 10,
                            },
                            {
                                "t": -(10**30),
                                "o": 5901.0,
                                "h": 5902.0,
                                "l": 5900.0,
                                "c": 5901.5,
                                "v": 10,
                            },
                        ],
                    },
                )
            if scenario == "clean_recover":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bars": [
                            _wire_bar(t0, 5900.0),
                            _wire_bar(t1, 5902.0),
                            _wire_bar(t2, 5904.0),
                        ],
                    },
                )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path / "data")
    sqlite_store = SqliteStore(tmp_path / "meta.db")
    sqlite_store.initialize()
    try:
        parquet_store = ParquetStore(tmp_path / "data", sqlite_store=sqlite_store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = TopstepAuthService(settings, client)
            await auth.login("alice", "key-1")
            rest = TopstepRestClient(settings, auth, client)
            svc = HistoryBackfillService(settings, rest, parquet_store, sqlite_store)

            # 1. PROVIDER_GAP -> is_complete=False
            res_gap = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=t0,
                end_ts=t_end,
                now_utc=t_end + 120,
            )
            assert res_gap.provider_gap is True
            assert res_gap.is_complete is False

            # 2. Retry returns 10**30 and -10**30 alongside valid t0 -> quarantines without crash
            scenario = "corrupted_extreme_ts"
            res_bad = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=t0,
                end_ts=t_end,
                now_utc=t_end + 120,
            )
            assert res_bad.is_complete is False
            assert res_bad.quarantined_count == 2
            assert [b.time for b in res_bad.bars] == [t0]

            # 3. Clean retry heals prior PROVIDER_GAP / CORRUPTED coverage
            scenario = "clean_recover"
            res_ok = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=t0,
                end_ts=t_end,
                now_utc=t_end + 120,
            )
            assert res_ok.is_complete is True
            assert [b.time for b in res_ok.bars] == [t0, t1, t2]

            # 4. Subsequent read uses 0 upstream calls and succeeds offline
            calls_before_cache = upstream_calls
            res_cached = await svc.sync_and_read_bars(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=t0,
                end_ts=t_end,
                now_utc=t_end + 120,
                allow_upstream=False,
            )
            assert upstream_calls == calls_before_cache
            assert res_cached.upstream_requests == 0
            assert res_cached.is_complete is True
            assert [b.time for b in res_cached.bars] == [t0, t1, t2]

            # 5. Conflicting root_symbol raises ValueError; offline uncached raises TopstepAuthError
            with pytest.raises(ValueError, match="conflicts"):
                await svc.sync_and_read_bars(
                    contract_id="CON.F.US.MGC.Z26",
                    live=False,
                    timeframe="1m",
                    start_ts=t0,
                    end_ts=t_end,
                    root_symbol="MES",
                    now_utc=t_end + 120,
                )
            with pytest.raises(TopstepAuthError):
                await svc.sync_and_read_bars(
                    contract_id="CON.F.US.MGC.Z26",
                    live=False,
                    timeframe="1m",
                    start_ts=t0,
                    end_ts=t_end,
                    now_utc=t_end + 120,
                    allow_upstream=False,
                )
    finally:
        sqlite_store.close()


async def test_f04_rest_client_deadline_non_list_and_unparseable_pages() -> None:
    """Contract: retrieve_bars_paginated enforces deadline, non-list guard, and unparseable halt.

    Defect caught: Hung pagination bypassing deadline, AttributeError on bad payload, or loops.
    """
    mode = "timeout"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"success": True, "token": "jwt-ok"})
        if request.url.path == "/api/History/retrieveBars":
            if mode == "timeout":
                await asyncio.sleep(0.3)
                return httpx.Response(200, json={"success": True, "bars": []})
            if mode == "non_list":
                return httpx.Response(200, json={"success": True, "bars": {"bad": 1}})
            if mode == "unparseable":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bars": [{"t": "not-a-timestamp", "o": 1, "h": 2, "l": 1, "c": 2, "v": 1}],
                    },
                )
        return httpx.Response(404)

    settings = Settings(history_backfill_timeout_sec=0.1, upstream_max_retries=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = TopstepAuthService(settings, client)
        await auth.login("alice", "key-1")
        rest = TopstepRestClient(settings, auth, client)

        with pytest.raises(UpstreamTimeoutError):
            await rest.retrieve_bars_paginated(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=BASE_T,
                end_ts=BASE_T + 300,
            )

        mode = "non_list"
        with pytest.raises(UpstreamHTTPError):
            await rest.retrieve_bars_paginated(
                contract_id="CON.F.US.MES.Z26",
                live=False,
                timeframe="1m",
                start_ts=BASE_T,
                end_ts=BASE_T + 300,
            )

        mode = "unparseable"
        bad_page = await rest.retrieve_bars_paginated(
            contract_id="CON.F.US.MES.Z26",
            live=False,
            timeframe="1m",
            start_ts=BASE_T,
            end_ts=BASE_T + 300,
        )
        assert bad_page.provider_gap is True
        assert bad_page.truncated is True
        assert len(bad_page.raw_bars) == 1
