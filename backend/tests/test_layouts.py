"""Value-first tests for named layout SQLite persistence and /api/layouts CRUD (F01, F06)."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher

from app.config import Settings
from app.main import create_app
from app.storage.sqlite_store import SqliteStore

TEST_PASSWORD = "layout-test-password-123"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)

# Independent expected template-to-pane-count contract from the F06 layout spec
# (never imported from app.api.routes_layout so drift in production is caught).
EXPECTED_TEMPLATE_PANE_COUNTS: dict[str, int] = {
    "1-single": 1,
    "2-cols": 2,
    "2-rows": 2,
    "3-cols": 3,
    "3-rows": 3,
    "3-left-main": 3,
    "3-right-main": 3,
    "3-top-main": 3,
    "3-bottom-main": 3,
    "4-grid-2x2": 4,
    "4-cols": 4,
    "4-rows": 4,
    "4-left-main": 4,
    "4-top-main": 4,
    "5-top2-bot3": 5,
    "5-left-main-4": 5,
    "6-grid-3x2": 6,
    "6-grid-2x3": 6,
    "7-top3-bot4": 7,
    "8-grid-4x2": 8,
    "8-grid-2x4": 8,
}


@asynccontextmanager
async def _async_client(tmp_path: Path):
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json={}))
    )
    tg_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
    )
    app = create_app(settings=settings, http_client=upstream, telegram_http_client=tg_client)
    try:
        async with app.router.lifespan_context(app):
            asgi = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
            async with httpx.AsyncClient(
                transport=asgi,
                base_url="http://127.0.0.1:8000",
            ) as client:
                yield client, app
    finally:
        await tg_client.aclose()
        await upstream.aclose()


async def _login(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    return str(resp.json()["csrf_token"])


def _valid_layout_payload(
    *,
    name: str = "MES Scalp",
    template_id: str = "2-cols",
    is_default: bool = False,
) -> dict[str, object]:
    count = EXPECTED_TEMPLATE_PANE_COUNTS[template_id]
    symbols = ["MES", "MNQ", "MYM", "MGC"]
    panes = [
        {
            "pane_id": f"pane-{i + 1}",
            "root_symbol": symbols[i % len(symbols)],
            "timeframe": "15m",
            "live_mode": False,
            "playback_mode": "live",
        }
        for i in range(count)
    ]
    return {
        "name": name,
        "template_id": template_id,
        "panes": panes,
        "sync_config": {
            "symbol": True,
            "interval": False,
            "crosshair": True,
            "time": False,
            "dateRange": True,
        },
        "is_default": is_default,
    }


async def test_layouts_require_session_and_csrf(tmp_path: Path) -> None:
    """Contract: /api/layouts enforces caller session on GET and X-CSRF-Token on mutations.

    Defect caught: Missing require_session or require_csrf dependencies on layout endpoints.
    """
    async with _async_client(tmp_path) as (client, _):
        unauth_get = await client.get("/api/layouts")
        assert unauth_get.status_code in (401, 403)

        unauth_post = await client.post("/api/layouts", json=_valid_layout_payload())
        assert unauth_post.status_code in (401, 403)

        csrf = await _login(client)
        no_csrf_post = await client.post("/api/layouts", json=_valid_layout_payload())
        assert no_csrf_post.status_code in (401, 403)

        no_csrf_put = await client.put(
            "/api/layouts/layout-4-future",
            json=_valid_layout_payload(template_id="4-grid-2x2"),
        )
        assert no_csrf_put.status_code in (401, 403)

        no_csrf_del = await client.delete("/api/layouts/layout-4-future")
        assert no_csrf_del.status_code in (401, 403)

        authed_get = await client.get("/api/layouts")
        assert authed_get.status_code == 200
        assert csrf


async def test_layouts_seeds_default_4_future_idempotently(tmp_path: Path) -> None:
    """Contract: Fresh DB seeds default '4 Future' 2x2 layout with MES/MNQ/MYM/MGC 5m panes.

    Defect caught: Missing default layout seed, wrong template_id, or duplicate seeding on GET.
    """
    async with _async_client(tmp_path) as (client, _):
        await _login(client)
        first = await client.get("/api/layouts")
        assert first.status_code == 200
        layouts = first.json()["layouts"]
        assert len(layouts) == 1
        layout = layouts[0]
        assert layout["id"] == "layout-4-future"
        assert layout["name"] == "4 Future"
        assert layout["template_id"] == "4-grid-2x2"
        assert layout["is_default"] is True
        assert [p["root_symbol"] for p in layout["panes"]] == ["MES", "MNQ", "MYM", "MGC"]
        assert all(
            p["timeframe"] == "5m"
            and p["live_mode"] is False
            and p["playback_mode"] == "live"
            for p in layout["panes"]
        )
        assert layout["sync_config"] == {
            "symbol": False,
            "interval": True,
            "crosshair": True,
            "time": False,
            "dateRange": False,
        }

        second = await client.get("/api/layouts")
        assert second.status_code == 200
        assert len(second.json()["layouts"]) == 1


async def test_layouts_crud_default_swap_promotion_and_last_layout_guard(tmp_path: Path) -> None:
    """Contract: Default swap is atomic, deleting default promotes oldest, last delete -> 409.

    Defect caught: Multiple defaults, zero-default state, or deleting the final remaining layout.
    """
    async with _async_client(tmp_path) as (client, _):
        csrf = await _login(client)
        headers = {"X-CSRF-Token": csrf}

        initial = await client.get("/api/layouts")
        assert initial.status_code == 200
        seeded_id = initial.json()["layouts"][0]["id"]

        created_resp = await client.post(
            "/api/layouts",
            json=_valid_layout_payload(name="New Default", template_id="2-cols", is_default=True),
            headers=headers,
        )
        assert created_resp.status_code == 201
        created = created_resp.json()["layout"]
        created_id = created["id"]
        assert created["is_default"] is True

        after_create = (await client.get("/api/layouts")).json()["layouts"]
        defaults_after_create = [item for item in after_create if item["is_default"]]
        assert len(defaults_after_create) == 1
        assert defaults_after_create[0]["id"] == created_id

        updated_resp = await client.put(
            f"/api/layouts/{created_id}",
            json=_valid_layout_payload(
                name="Renamed Default",
                template_id="3-left-main",
                is_default=True,
            ),
            headers=headers,
        )
        assert updated_resp.status_code == 200
        assert updated_resp.json()["layout"]["name"] == "Renamed Default"
        assert len(updated_resp.json()["layout"]["panes"]) == 3

        del_created = await client.delete(f"/api/layouts/{created_id}", headers=headers)
        assert del_created.status_code == 200
        assert del_created.json() == {"deleted": True, "id": created_id}

        remaining = (await client.get("/api/layouts")).json()["layouts"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == seeded_id
        assert remaining[0]["is_default"] is True

        delete_last = await client.delete(f"/api/layouts/{seeded_id}", headers=headers)
        assert delete_last.status_code == 409

        still_there = (await client.get("/api/layouts")).json()["layouts"]
        assert len(still_there) == 1
        assert still_there[0]["id"] == seeded_id

        missing_put = await client.put(
            "/api/layouts/nonexistent-id",
            json=_valid_layout_payload(),
            headers=headers,
        )
        assert missing_put.status_code == 404

        missing_del = await client.delete("/api/layouts/nonexistent-id", headers=headers)
        assert missing_del.status_code == 404


async def test_layouts_validation_rejects_bad_template_counts_symbols_and_timeframes(
    tmp_path: Path,
) -> None:
    """Contract: Strict schema validation rejects bad pane counts, symbols, timeframes, and names.

    Defect caught: Unvalidated layout payloads corrupting SQLite or pane rendering.
    """
    async with _async_client(tmp_path) as (client, _):
        csrf = await _login(client)
        headers = {"X-CSRF-Token": csrf}

        # Mismatched pane count (2-cols requires 2 panes, supplied 1)
        bad_count = _valid_layout_payload(template_id="2-cols")
        bad_count_panes = bad_count["panes"]
        assert isinstance(bad_count_panes, list)
        bad_count["panes"] = [bad_count_panes[0]]
        resp_count = await client.post("/api/layouts", json=bad_count, headers=headers)
        assert resp_count.status_code == 422

        # Unsupported root_symbol
        bad_symbol = _valid_layout_payload(template_id="1-single")
        bad_symbol_panes = bad_symbol["panes"]
        assert isinstance(bad_symbol_panes, list)
        bad_symbol_panes[0]["root_symbol"] = "ES"
        resp_symbol = await client.post("/api/layouts", json=bad_symbol, headers=headers)
        assert resp_symbol.status_code == 422

        # Unsupported timeframe
        bad_tf = _valid_layout_payload(template_id="1-single")
        bad_tf_panes = bad_tf["panes"]
        assert isinstance(bad_tf_panes, list)
        bad_tf_panes[0]["timeframe"] = "2m"
        resp_tf = await client.post("/api/layouts", json=bad_tf, headers=headers)
        assert resp_tf.status_code == 422

        # Blank name
        bad_name = _valid_layout_payload(name="   ", template_id="1-single")
        resp_name = await client.post("/api/layouts", json=bad_name, headers=headers)
        assert resp_name.status_code == 422

        # Normalized lowercase symbol and uppercase timeframe are accepted and canonicalized
        norm_payload = {
            "name": "  Normalized 8-Pane  ",
            "template_id": "8-grid-4x2",
            "panes": [
                {
                    "root_symbol": "mes",
                    "timeframe": "1H",
                    "live_mode": False,
                    "playback_mode": "replay",
                }
                for _ in range(8)
            ],
            "sync_config": {
                "symbol": False,
                "interval": True,
                "crosshair": True,
                "time": False,
                "dateRange": False,
            },
        }
        resp_norm = await client.post("/api/layouts", json=norm_payload, headers=headers)
        assert resp_norm.status_code == 201
        saved = resp_norm.json()["layout"]
        assert saved["name"] == "Normalized 8-Pane"
        assert saved["panes"][0]["root_symbol"] == "MES"
        assert saved["panes"][0]["timeframe"] == "1h"
        assert saved["panes"][0]["playback_mode"] == "replay"


async def test_layouts_accept_all_spec_templates_against_independent_pane_counts(
    tmp_path: Path,
) -> None:
    """Contract: Every F06 template_id enforces its exact spec pane count independently.

    Defect caught: Production LAYOUT_TEMPLATE_PANE_COUNTS drifting from the 21-template F06 spec.
    """
    async with _async_client(tmp_path) as (client, _):
        csrf = await _login(client)
        headers = {"X-CSRF-Token": csrf}

        for template_id, expected_count in EXPECTED_TEMPLATE_PANE_COUNTS.items():
            resp = await client.post(
                "/api/layouts",
                json=_valid_layout_payload(
                    name=f"Template {template_id}",
                    template_id=template_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 201, f"failed for {template_id}: {resp.text}"
            layout = resp.json()["layout"]
            assert layout["template_id"] == template_id
            assert len(layout["panes"]) == expected_count

        unknown_payload = {
            "name": "Unknown Template",
            "template_id": "9-unknown",
            "panes": _valid_layout_payload(template_id="1-single")["panes"],
            "sync_config": {
                "symbol": False,
                "interval": True,
                "crosshair": True,
                "time": False,
                "dateRange": False,
            },
        }
        unknown_resp = await client.post("/api/layouts", json=unknown_payload, headers=headers)
        assert unknown_resp.status_code == 422


async def test_layouts_delete_default_promotes_oldest_directly_in_sqlite_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract: Deleting default layout promotes oldest remaining layout atomically in SQLite.

    Defect caught: ensure_default_layout() masking missing default promotion inside delete_layout.
    """
    async with _async_client(tmp_path) as (client, app):
        timestamps = iter([1000, 2000, 3000, 4000])
        monkeypatch.setattr(app.state.sqlite_store, "_now_utc", lambda: next(timestamps))

        csrf = await _login(client)
        headers = {"X-CSRF-Token": csrf}

        # Seed layout-4-future (created_at=1000)
        initial = await client.get("/api/layouts")
        assert initial.status_code == 200
        oldest_id = initial.json()["layouts"][0]["id"]

        # Create second (non-default, created_at=2000) and third (default, created_at=3000)
        second_resp = await client.post(
            "/api/layouts",
            json=_valid_layout_payload(name="Second Desk", template_id="2-cols", is_default=False),
            headers=headers,
        )
        assert second_resp.status_code == 201
        second_id = second_resp.json()["layout"]["id"]

        third_resp = await client.post(
            "/api/layouts",
            json=_valid_layout_payload(name="Third Default", template_id="2-rows", is_default=True),
            headers=headers,
        )
        assert third_resp.status_code == 201
        third_id = third_resp.json()["layout"]["id"]

        del_resp = await client.delete(f"/api/layouts/{third_id}", headers=headers)
        assert del_resp.status_code == 200

        # Inspect SqliteStore directly BEFORE calling GET /api/layouts (which runs ensure_default)
        promoted = await app.state.sqlite_store.list_layouts()
        assert len(promoted) == 2
        assert promoted[0].id == oldest_id
        assert promoted[0].is_default is True
        assert promoted[1].id == second_id
        assert promoted[1].is_default is False


async def test_layouts_persist_across_sqlite_store_reopen(tmp_path: Path) -> None:
    """Contract: Created and updated layouts survive closing and reopening SqliteStore.

    Defect caught: Uncommitted SQLite transactions or volatile state lost across restarts.
    """
    db_path = tmp_path / "data" / "metadata.db"
    store1 = SqliteStore(db_path)
    store1.initialize()
    try:
        await store1.ensure_default_layout()
        two_panes = _valid_layout_payload(template_id="2-cols")["panes"]
        three_panes = _valid_layout_payload(template_id="3-cols")["panes"]
        assert isinstance(two_panes, list)
        assert isinstance(three_panes, list)

        created = await store1.create_layout(
            name="Swing Desk",
            template_id="2-cols",
            panes=two_panes,
            sync_config={
                "symbol": True,
                "interval": False,
                "crosshair": True,
                "time": False,
                "dateRange": False,
            },
            is_default=True,
        )
        updated = await store1.update_layout(
            created.id,
            name="Swing Desk v2",
            template_id="3-cols",
            panes=three_panes,
            sync_config={
                "symbol": False,
                "interval": True,
                "crosshair": True,
                "time": True,
                "dateRange": False,
            },
            is_default=True,
        )
        assert updated is not None
    finally:
        store1.close()

    store2 = SqliteStore(db_path)
    store2.initialize()
    try:
        all_layouts = await store2.list_layouts()
        assert len(all_layouts) == 2
        reloaded = await store2.get_layout(created.id)
        assert reloaded is not None
        assert reloaded.name == "Swing Desk v2"
        assert reloaded.template_id == "3-cols"
        assert reloaded.is_default is True
        assert len(reloaded.panes) == 3
        assert reloaded.sync_config["time"] is True

        seeded = await store2.get_layout("layout-4-future")
        assert seeded is not None
        assert seeded.is_default is False
    finally:
        store2.close()


async def test_layouts_default_swap_rolls_back_atomically_on_mid_tx_failure(
    tmp_path: Path,
) -> None:
    """Contract: Failed create_layout/update_layout default-swap rolls back prior is_default reset.

    Defect caught: Partial transaction committing is_default=0 across all layouts on DB error.
    """
    db_path = tmp_path / "data" / "rollback.db"
    store = SqliteStore(db_path)
    store.initialize()
    try:
        seeded = await store.ensure_default_layout()
        assert seeded.is_default is True

        two_panes = _valid_layout_payload(template_id="2-cols")["panes"]
        assert isinstance(two_panes, list)
        default_sync = {
            "symbol": False,
            "interval": True,
            "crosshair": True,
            "time": False,
            "dateRange": False,
        }
        secondary = await store.create_layout(
            name="Secondary Desk",
            template_id="2-cols",
            panes=two_panes,
            sync_config=default_sync,
            is_default=False,
        )

        # Inject mid-transaction DB failure that fires AFTER `UPDATE layouts SET is_default = 0`
        with store._lock:
            conn = store._ensure_conn_locked()
            conn.executescript(
                """
                CREATE TRIGGER fail_insert_default_swap
                BEFORE INSERT ON layouts
                WHEN NEW.name = 'TriggerFailInsert'
                BEGIN
                    SELECT RAISE(ABORT, 'injected insert failure');
                END;

                CREATE TRIGGER fail_update_default_swap
                BEFORE UPDATE OF name ON layouts
                WHEN NEW.name = 'TriggerFailUpdate'
                BEGIN
                    SELECT RAISE(ABORT, 'injected update failure');
                END;
                """
            )
            conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="injected insert failure"):
            await store.create_layout(
                name="TriggerFailInsert",
                template_id="2-cols",
                panes=two_panes,
                sync_config=default_sync,
                is_default=True,
            )

        after_insert_fail = await store.list_layouts()
        assert len(after_insert_fail) == 2
        defaults_after_insert = [item.id for item in after_insert_fail if item.is_default]
        assert defaults_after_insert == [seeded.id]

        with pytest.raises(sqlite3.DatabaseError, match="injected update failure"):
            await store.update_layout(
                secondary.id,
                name="TriggerFailUpdate",
                template_id="2-cols",
                panes=two_panes,
                sync_config=default_sync,
                is_default=True,
            )

        after_update_fail = await store.list_layouts()
        assert len(after_update_fail) == 2
        defaults_after_update = [item.id for item in after_update_fail if item.is_default]
        assert defaults_after_update == [seeded.id]
        reloaded_secondary = await store.get_layout(secondary.id)
        assert reloaded_secondary is not None
        assert reloaded_secondary.name == "Secondary Desk"
        assert reloaded_secondary.is_default is False
    finally:
        store.close()


async def test_layouts_update_sole_default_to_false_preserves_default_in_sqlite_store(
    tmp_path: Path,
) -> None:
    """Contract: Updating the sole remaining layout with is_default=False preserves is_default=True.

    Defect caught: Zero-default SQLite commit masked by ensure_default_layout()
    on subsequent GET (P1-R5-02).
    """
    async with _async_client(tmp_path) as (client, app):
        csrf = await _login(client)
        headers = {"X-CSRF-Token": csrf}

        initial = await client.get("/api/layouts")
        assert initial.status_code == 200
        layouts = initial.json()["layouts"]
        assert len(layouts) == 1
        seeded_id = layouts[0]["id"]
        assert layouts[0]["is_default"] is True

        put_resp = await client.put(
            f"/api/layouts/{seeded_id}",
            json=_valid_layout_payload(
                name="Sole Desk",
                template_id="2-cols",
                is_default=False,
            ),
            headers=headers,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["layout"]["id"] == seeded_id
        assert put_resp.json()["layout"]["name"] == "Sole Desk"
        assert put_resp.json()["layout"]["is_default"] is True

        # Inspect SqliteStore directly BEFORE calling GET /api/layouts (ensure_default_layout).
        direct_rows = await app.state.sqlite_store.list_layouts()
        assert len(direct_rows) == 1
        assert direct_rows[0].id == seeded_id
        assert direct_rows[0].is_default is True

        after_get = await client.get("/api/layouts")
        assert after_get.status_code == 200
        after_layouts = after_get.json()["layouts"]
        assert len(after_layouts) == 1
        assert after_layouts[0]["id"] == seeded_id
        assert after_layouts[0]["is_default"] is True
