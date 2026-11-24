"""Value-first tests for named layout SQLite persistence and /api/layouts CRUD (F01, F06)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from argon2 import PasswordHasher

from app.api.routes_layout import LAYOUT_TEMPLATE_PANE_COUNTS
from app.config import Settings
from app.main import create_app

TEST_PASSWORD = "layout-test-password-123"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)


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
    count = LAYOUT_TEMPLATE_PANE_COUNTS[template_id]
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
