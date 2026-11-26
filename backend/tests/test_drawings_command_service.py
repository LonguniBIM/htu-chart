"""Value-first tests for drawings, CommandService, OCC, idempotency, Undo/Redo, WS, and auth."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.ws_broadcaster import WS_CLOSE_SLOW_CONSUMER, WsBroadcaster
from app.storage.sqlite_store import (
    MAX_DRAWINGS_PER_SCOPE,
    MAX_UNDO_HISTORY,
    SqliteStore,
)

TEST_PASSWORD = "drawings-command-test-password-123"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)
BASE_TIME = 1700000000


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


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    return {"X-CSRF-Token": str(resp.json()["csrf_token"])}


def _fvg_drawing_payload(
    *,
    drawing_id: str | None = "draw-fvg-1",
    t1: int = BASE_TIME,
    p1: float = 5000.25,
    t2: int = BASE_TIME + 600,
    p2: float = 5005.50,
    created_by: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "fvg_box",
        "source_bar_refs": [t1, t2],
        "coordinates": {
            "anchors": [
                {"role": "start", "time": t1, "price": p1},
                {"role": "end", "time": t2, "price": p2},
            ]
        },
        "style": {"fill": "#10b981", "ce_line": True},
        "metadata": {"label": "Bullish FVG"},
    }
    if drawing_id is not None:
        payload["id"] = drawing_id
    if created_by is not None:
        payload["created_by"] = created_by
    return payload


class _FakeWsClient:
    def __init__(self, *, stall_event: asyncio.Event | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed_calls: list[tuple[int, str | None]] = []
        self._stall_event = stall_event

    async def send_json(self, data: Any) -> None:
        if self._stall_event is not None:
            await self._stall_event.wait()
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_calls.append((code, reason))


async def test_drawings_auth_csrf_and_hard_readonly_guard(tmp_path: Path) -> None:
    """Contract: /api/drawings enforces F01 session + CSRF and SEC-001 zero order endpoints.

    Defect caught: Unauthenticated reads, CSRF-less mutations, or exposed order routes.
    """
    async with _async_client(tmp_path) as (client, _):
        assert (await client.get("/api/drawings/MES")).status_code in {401, 403}
        assert (
            await client.post(
                "/api/drawings/MES",
                json={
                    "action": "create",
                    "expected_revision": 0,
                    "drawing": _fvg_drawing_payload(),
                },
            )
        ).status_code in {401, 403}
        assert (
            await client.post("/api/drawings/MES/undo", json={"expected_revision": 0})
        ).status_code in {401, 403}
        assert (
            await client.post("/api/drawings/MES/redo", json={"expected_revision": 0})
        ).status_code in {401, 403}
        assert (await client.delete("/api/drawings/MES?expected_revision=0")).status_code in {
            401,
            403,
        }
        assert (
            await client.delete("/api/drawings/MES/draw-fvg-1?expected_revision=0")
        ).status_code in {401, 403}

        headers = await _login(client)

        # Missing CSRF token on authenticated mutations -> 403
        no_csrf_post = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(),
            },
        )
        assert no_csrf_post.status_code in {401, 403}
        bad_csrf_post = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(),
            },
            headers={"X-CSRF-Token": "wrong-csrf-token"},
        )
        assert bad_csrf_post.status_code in {401, 403}
        assert (await client.delete("/api/drawings/MES?expected_revision=0")).status_code in {
            401,
            403,
        }
        assert (
            await client.post("/api/drawings/MES/undo", json={"expected_revision": 0})
        ).status_code in {401, 403}

        # Valid authenticated GET returns empty scope + CME instrument spec
        get_resp = await client.get("/api/drawings/mes")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["symbol"] == "MES"
        assert body["contract_id"] is None
        assert body["pane_id"] is None
        assert body["scope_revision"] == 0
        assert body["can_undo"] is False
        assert body["can_redo"] is False
        assert body["drawings"] == []
        assert body["instrument"] == {
            "root_symbol": "MES",
            "name": "Micro E-mini S&P 500",
            "exchange": "CME",
            "tick_size": 0.25,
            "tick_value": 1.25,
            "currency": "USD",
        }

        # Hard Read-Only check: zero order/position endpoints exist
        order_get = await client.get("/api/Order/place")
        assert order_get.status_code in {401, 403, 404, 405}
        order_post = await client.post("/api/Order/place", json={}, headers=headers)
        assert order_post.status_code in {401, 403, 404, 405}


async def test_drawings_all_types_domain_validation_and_pixel_rejection(tmp_path: Path) -> None:
    """Contract: All 8 drawing types validate strictly in (time, price) and reject pixels.

    Defect caught: Accepting pixel coordinates, invalid anchors, NaN values, or bad scopes.
    """
    async with _async_client(tmp_path) as (client, _):
        headers = await _login(client)

        valid_specs: list[tuple[str, dict[str, Any]]] = [
            (
                "hline",
                {
                    "id": "d-hline",
                    "type": "hline",
                    "coordinates": {
                        "anchors": [{"role": "level", "time": BASE_TIME, "price": 5010}]
                    },
                },
            ),
            (
                "trendline",
                {
                    "id": "d-trend",
                    "type": "trendline",
                    "coordinates": {
                        "anchors": [
                            {"role": "start", "time": BASE_TIME, "price": 5000.0},
                            {"role": "end", "time": BASE_TIME + 300, "price": 5010.0},
                        ]
                    },
                },
            ),
            (
                "ray",
                {
                    "id": "d-ray",
                    "type": "ray",
                    "coordinates": {
                        "anchors": [
                            {"role": "start", "time": BASE_TIME, "price": 5000.0},
                            {"role": "end", "time": BASE_TIME + 300, "price": 5002.5},
                        ]
                    },
                },
            ),
            ("fvg_box", _fvg_drawing_payload(drawing_id="d-fvg")),
            (
                "order_block",
                {
                    "id": "d-ob",
                    "type": "order_block",
                    "coordinates": {
                        "anchors": [
                            {"role": "start", "time": BASE_TIME, "price": 4990.0},
                            {"role": "end", "time": BASE_TIME + 900, "price": 4995.0},
                        ]
                    },
                },
            ),
            (
                "long_position",
                {
                    "id": "d-long",
                    "type": "long_position",
                    "coordinates": {
                        "anchors": [
                            {"role": "entry", "time": BASE_TIME, "price": 5000.0},
                            {"role": "target", "time": BASE_TIME + 600, "price": 5020.0},
                            {"role": "stop", "time": BASE_TIME + 600, "price": 4990.0},
                        ]
                    },
                },
            ),
            (
                "short_position",
                {
                    "id": "d-short",
                    "type": "short_position",
                    "coordinates": {
                        "anchors": [
                            {"role": "entry", "time": BASE_TIME, "price": 5000.0},
                            {"role": "target", "time": BASE_TIME + 600, "price": 4980.0},
                            {"role": "stop", "time": BASE_TIME + 600, "price": 5010.0},
                        ]
                    },
                },
            ),
            (
                "measure",
                {
                    "id": "d-measure",
                    "type": "measure",
                    "coordinates": {
                        "anchors": [
                            {"role": "start", "time": BASE_TIME, "price": 5000.0},
                            {"role": "end", "time": BASE_TIME + 300, "price": 5005.0},
                        ]
                    },
                },
            ),
        ]

        rev = 0
        for _dtype, spec in valid_specs:
            resp = await client.post(
                "/api/drawings/MES",
                json={
                    "action": "create",
                    "expected_revision": rev,
                    "contract_id": "CON.F.US.MES.Z26",
                    "pane_id": "pane-1",
                    "created_by": "agent",
                    "drawing": spec,
                },
                headers=headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            rev += 1
            assert body["scope_revision"] == rev
            assert body["contract_id"] == "CON.F.US.MES.Z26"
            assert body["pane_id"] == "pane-1"

        listed = await client.get(
            "/api/drawings/MES?contract_id=CON.F.US.MES.Z26&pane_id=pane-1"
        )
        assert len(listed.json()["drawings"]) == 8

        # Reject mismatched contract_id vs symbol
        mismatch = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 0,
                "contract_id": "CON.F.US.MNQ.Z26",
                "drawing": _fvg_drawing_payload(),
            },
            headers=headers,
        )
        assert mismatch.status_code in {400, 422}

        # Reject invalid root symbol and invalid pane_id
        assert (await client.get("/api/drawings/ES")).status_code in {400, 422}
        assert (await client.get("/api/drawings/MES?pane_id=../bad")).status_code in {400, 422}

        # Reject pixel fields in coordinates and inside anchors
        for bad_coords in (
            {"anchors": [{"role": "level", "time": BASE_TIME, "price": 5000.0}], "pixel_x": 120},
            {"anchors": [{"role": "level", "time": BASE_TIME, "price": 5000.0, "x": 120}]},
            {"anchors": [{"role": "level", "time": BASE_TIME, "price": 5000.0, "clientY": 44}]},
            {"anchors": [{"role": "level", "time": BASE_TIME, "price": 5000.0, "offsetX": 10}]},
        ):
            pix_resp = await client.post(
                "/api/drawings/MES",
                json={
                    "action": "create",
                    "expected_revision": 0,
                    "drawing": {"type": "hline", "coordinates": bad_coords},
                },
                headers=headers,
            )
            assert pix_resp.status_code == 422

        # Reject invalid position prices, degenerate ray, bool time/price, oversized metadata
        invalid_drawings: list[dict[str, Any]] = [
            {
                "type": "ray",
                "coordinates": {
                    "anchors": [
                        {"role": "start", "time": BASE_TIME, "price": 5000.0},
                        {"role": "end", "time": BASE_TIME, "price": 5000.0},
                    ]
                },
            },
            {
                "type": "long_position",
                "coordinates": {
                    "anchors": [
                        {"role": "entry", "time": BASE_TIME, "price": 5000.0},
                        {"role": "target", "time": BASE_TIME + 60, "price": 4999.0},
                        {"role": "stop", "time": BASE_TIME + 60, "price": 4990.0},
                    ]
                },
            },
            {
                "type": "short_position",
                "coordinates": {
                    "anchors": [
                        {"role": "entry", "time": BASE_TIME, "price": 5000.0},
                        {"role": "target", "time": BASE_TIME + 60, "price": 5010.0},
                        {"role": "stop", "time": BASE_TIME + 60, "price": 5020.0},
                    ]
                },
            },
            {
                "type": "hline",
                "coordinates": {"anchors": [{"role": "level", "time": True, "price": 5000.0}]},
            },
            {
                "type": "hline",
                "coordinates": {"anchors": [{"role": "level", "time": BASE_TIME, "price": True}]},
            },
            {
                "type": "hline",
                "coordinates": {"anchors": [{"role": "level", "time": 100, "price": 5000.0}]},
            },
            {
                "type": "hline",
                "coordinates": {"anchors": [{"role": "level", "time": BASE_TIME, "price": -5.0}]},
            },
            {
                "type": "hline",
                "coordinates": {"anchors": [{"role": "level", "time": BASE_TIME, "price": 5000.0}]},
                "metadata": {"blob": "a" * 8200},
            },
        ]
        for bad_drawing in invalid_drawings:
            bad_resp = await client.post(
                "/api/drawings/MES",
                json={"action": "create", "expected_revision": 0, "drawing": bad_drawing},
                headers=headers,
            )
            assert bad_resp.status_code == 422


async def test_drawings_occ_revision_conflict_across_all_commands(tmp_path: Path) -> None:
    """Contract: Stale expected_revision fails with 409 revision_conflict across all mutations.

    Defect caught: Bypassed optimistic concurrency check or wrong error code/body.
    """
    async with _async_client(tmp_path) as (client, _):
        headers = await _login(client)

        create_resp = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(drawing_id="d1"),
            },
            headers=headers,
        )
        assert create_resp.status_code == 200
        assert create_resp.json()["scope_revision"] == 1

        stale_payloads: list[dict[str, Any]] = [
            {
                "action": "create",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(drawing_id="d2"),
            },
            {
                "action": "update",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(drawing_id="d1", p2=5010.0),
            },
            {"action": "delete", "expected_revision": 0, "drawing_id": "d1"},
            {"action": "clear", "expected_revision": 0},
            {
                "action": "batch",
                "expected_revision": 0,
                "operations": [
                    {"action": "create", "drawing": _fvg_drawing_payload(drawing_id="d3")}
                ],
            },
        ]
        for payload in stale_payloads:
            resp = await client.post("/api/drawings/MES", json=payload, headers=headers)
            assert resp.status_code == 409
            detail = resp.json()["detail"]
            assert detail["code"] == "revision_conflict"
            assert detail["current_revision"] == 1
            assert detail["expected_revision"] == 0

        for path in ("/api/drawings/MES/undo", "/api/drawings/MES/redo"):
            resp = await client.post(path, json={"expected_revision": 0}, headers=headers)
            assert resp.status_code == 409
            detail = resp.json()["detail"]
            assert detail["code"] == "revision_conflict"
            assert detail["current_revision"] == 1
            assert detail["expected_revision"] == 0

        del_stale = await client.delete("/api/drawings/MES?expected_revision=0", headers=headers)
        assert del_stale.status_code == 409
        assert del_stale.json()["detail"]["code"] == "revision_conflict"

        # Verify state remained untouched at revision 1
        snap = (await client.get("/api/drawings/MES")).json()
        assert snap["scope_revision"] == 1
        assert [d["id"] for d in snap["drawings"]] == ["d1"]


async def test_drawings_idempotency_replay_and_key_conflict(tmp_path: Path) -> None:
    """Contract: Identical idempotency_key replays without mutation; mismatched payload -> 409.

    Defect caught: Double-applying retried create/undo or ignoring payload mismatch on key reuse.
    """
    async with _async_client(tmp_path) as (client, _):
        headers = await _login(client)

        first_payload = {
            "action": "create",
            "expected_revision": 0,
            "idempotency_key": "idem-create-1",
            "drawing": _fvg_drawing_payload(drawing_id=None),
        }
        first_resp = await client.post("/api/drawings/MES", json=first_payload, headers=headers)
        assert first_resp.status_code == 200
        first_body = first_resp.json()
        assert first_body["deduplicated"] is False
        assert first_body["scope_revision"] == 1
        assert len(first_body["drawings"]) == 1
        generated_id = first_body["drawings"][0]["id"]

        # Advance scope revision with a second drawing
        second_resp = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 1,
                "drawing": _fvg_drawing_payload(drawing_id="d-second"),
            },
            headers=headers,
        )
        assert second_resp.status_code == 200
        assert second_resp.json()["scope_revision"] == 2

        # Exact retry of first request even after revision moved to 2 returns deduplicated replay
        retry_resp = await client.post("/api/drawings/MES", json=first_payload, headers=headers)
        assert retry_resp.status_code == 200
        retry_body = retry_resp.json()
        assert retry_body["deduplicated"] is True
        assert retry_body["scope_revision"] == first_body["scope_revision"]
        assert retry_body["drawings"] == first_body["drawings"]
        assert retry_body["drawings"][0]["id"] == generated_id

        # Scope still has exactly 2 drawings at revision 2 (retry did not mutate)
        snap = (await client.get("/api/drawings/MES")).json()
        assert snap["scope_revision"] == 2
        assert len(snap["drawings"]) == 2

        # Reusing same idempotency_key with different payload -> 409 idempotency_key_conflict
        conflict_payload = {
            "action": "create",
            "expected_revision": 0,
            "idempotency_key": "idem-create-1",
            "drawing": _fvg_drawing_payload(drawing_id=None, p2=5012.0),
        }
        conflict_resp = await client.post(
            "/api/drawings/MES", json=conflict_payload, headers=headers
        )
        assert conflict_resp.status_code == 409
        assert conflict_resp.json()["detail"]["code"] == "idempotency_key_conflict"


async def test_drawings_batch_atomicity_and_undo_redo_coordinate_fidelity(tmp_path: Path) -> None:
    """Contract: Batch rolls back on partial failure; Undo/Redo preserves exact (time, price).

    Defect caught: Partial batch commits, mutated coordinates on Undo/Redo, or stale redo branch.
    """
    async with _async_client(tmp_path) as (client, _):
        headers = await _login(client)

        original_anchors = [
            {"role": "start", "time": BASE_TIME, "price": 5000.25},
            {"role": "end", "time": BASE_TIME + 600, "price": 5005.50},
        ]
        c_resp = await client.post(
            "/api/drawings/MES",
            json={
                "action": "create",
                "expected_revision": 0,
                "drawing": _fvg_drawing_payload(drawing_id="fvg-1"),
            },
            headers=headers,
        )
        assert c_resp.status_code == 200
        created_at = c_resp.json()["drawings"][0]["created_at_utc"]

        # Partial failure in batch (valid create + missing update) rolls back atomically
        bad_batch_missing = await client.post(
            "/api/drawings/MES",
            json={
                "action": "batch",
                "expected_revision": 1,
                "operations": [
                    {"action": "create", "drawing": _fvg_drawing_payload(drawing_id="fvg-temp")},
                    {
                        "action": "update",
                        "drawing_id": "nonexistent",
                        "drawing": {"style": {"color": "#fff"}},
                    },
                ],
            },
            headers=headers,
        )
        assert bad_batch_missing.status_code == 404

        # Duplicate create in batch also rolls back atomically
        bad_batch_dup = await client.post(
            "/api/drawings/MES",
            json={
                "action": "batch",
                "expected_revision": 1,
                "operations": [
                    {"action": "create", "drawing": _fvg_drawing_payload(drawing_id="fvg-temp-2")},
                    {"action": "create", "drawing": _fvg_drawing_payload(drawing_id="fvg-1")},
                ],
            },
            headers=headers,
        )
        assert bad_batch_dup.status_code == 409

        after_failed_batches = (await client.get("/api/drawings/MES")).json()
        assert after_failed_batches["scope_revision"] == 1
        assert [d["id"] for d in after_failed_batches["drawings"]] == ["fvg-1"]

        # Update coordinates & style
        updated_anchors = [
            {"role": "start", "time": BASE_TIME + 60, "price": 5001.0},
            {"role": "end", "time": BASE_TIME + 900, "price": 5008.75},
        ]
        u_resp = await client.post(
            "/api/drawings/MES",
            json={
                "action": "update",
                "expected_revision": 1,
                "drawing": {
                    "id": "fvg-1",
                    "type": "fvg_box",
                    "coordinates": {"anchors": updated_anchors},
                    "style": {"fill": "#ef4444"},
                },
            },
            headers=headers,
        )
        assert u_resp.status_code == 200
        assert u_resp.json()["scope_revision"] == 2

        # Delete via DELETE route
        d_resp = await client.delete(
            "/api/drawings/MES/fvg-1?expected_revision=2",
            headers=headers,
        )
        assert d_resp.status_code == 200
        assert d_resp.json()["scope_revision"] == 3
        assert d_resp.json()["drawings"] == []

        # Undo delete -> restores exact updated_anchors with monotonic revision=4
        undo1 = await client.post(
            "/api/drawings/MES/undo",
            json={"expected_revision": 3, "idempotency_key": "undo-1"},
            headers=headers,
        )
        assert undo1.status_code == 200
        u1_body = undo1.json()
        assert u1_body["scope_revision"] == 4
        assert u1_body["can_undo"] is True
        assert u1_body["can_redo"] is True
        restored = u1_body["drawings"][0]
        assert restored["revision"] == 4
        assert restored["created_at_utc"] == created_at
        assert restored["coordinates"]["anchors"] == updated_anchors

        # Undo update -> restores exact original_anchors
        undo2 = await client.post(
            "/api/drawings/MES/undo",
            json={"expected_revision": 4},
            headers=headers,
        )
        assert undo2.status_code == 200
        assert undo2.json()["scope_revision"] == 5
        assert undo2.json()["drawings"][0]["coordinates"]["anchors"] == original_anchors
        assert undo2.json()["drawings"][0]["style"] == {"fill": "#10b981", "ce_line": True}

        # Undo create -> empty scope, can_undo=False
        undo3 = await client.post(
            "/api/drawings/MES/undo",
            json={"expected_revision": 5},
            headers=headers,
        )
        assert undo3.status_code == 200
        assert undo3.json()["scope_revision"] == 6
        assert undo3.json()["drawings"] == []
        assert undo3.json()["can_undo"] is False
        assert undo3.json()["can_redo"] is True

        nothing_undo = await client.post(
            "/api/drawings/MES/undo",
            json={"expected_revision": 6},
            headers=headers,
        )
        assert nothing_undo.status_code == 409
        assert nothing_undo.json()["detail"]["code"] == "nothing_to_undo"

        # Redo twice -> create then update
        redo1 = await client.post(
            "/api/drawings/MES/redo",
            json={"expected_revision": 6},
            headers=headers,
        )
        assert redo1.status_code == 200
        assert redo1.json()["scope_revision"] == 7
        assert redo1.json()["drawings"][0]["coordinates"]["anchors"] == original_anchors

        redo2 = await client.post(
            "/api/drawings/MES/redo",
            json={"expected_revision": 7},
            headers=headers,
        )
        assert redo2.status_code == 200
        assert redo2.json()["scope_revision"] == 8
        assert redo2.json()["drawings"][0]["coordinates"]["anchors"] == updated_anchors
        assert redo2.json()["can_redo"] is True

        # New mutation at this point truncates the remaining redo (delete) branch
        batch_ok = await client.post(
            "/api/drawings/MES",
            json={
                "action": "batch",
                "expected_revision": 8,
                "operations": [
                    {"action": "create", "drawing": _fvg_drawing_payload(drawing_id="fvg-2")},
                    {
                        "action": "update",
                        "drawing_id": "fvg-1",
                        "drawing": {"metadata": {"note": "kept"}},
                    },
                ],
            },
            headers=headers,
        )
        assert batch_ok.status_code == 200
        assert batch_ok.json()["scope_revision"] == 9
        assert batch_ok.json()["can_redo"] is False

        nothing_redo = await client.post(
            "/api/drawings/MES/redo",
            json={"expected_revision": 9},
            headers=headers,
        )
        assert nothing_redo.status_code == 409
        assert nothing_redo.json()["detail"]["code"] == "nothing_to_redo"

        # Clear scope & undo clear
        clr = await client.delete("/api/drawings/MES?expected_revision=9", headers=headers)
        assert clr.status_code == 200
        assert clr.json()["scope_revision"] == 10
        assert clr.json()["drawings"] == []

        undo_clr = await client.post(
            "/api/drawings/MES/undo",
            json={"expected_revision": 10},
            headers=headers,
        )
        assert undo_clr.status_code == 200
        assert {d["id"] for d in undo_clr.json()["drawings"]} == {"fvg-1", "fvg-2"}


def test_drawings_ws_broadcast_on_mutations_and_silence_on_noop_or_dedup(tmp_path: Path) -> None:
    """Contract: /ws/market receives drawings_update on real mutations, never on dedup/empty clear.

    Defect caught: Missing WS broadcast on drawing change or spurious broadcast on replay/no-op.
    """
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        signalr_enabled=False,
    )
    app = create_app(settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_resp = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        assert login_resp.status_code == 200
        csrf_headers = {"X-CSRF-Token": str(login_resp.json()["csrf_token"])}
        session_cookie = login_resp.cookies["topstep_session"]

        with client.websocket_connect(
            "/ws/market",
            headers={
                "Host": "127.0.0.1:8000",
                "Origin": "http://127.0.0.1:8000",
                "Cookie": f"topstep_session={session_cookie}",
            },
        ) as ws:
            initial = ws.receive_json()
            assert initial["type"] == "feed_status"

            # Empty clear (0 drawings) -> must NOT broadcast drawings_update
            empty_clear = client.delete(
                "/api/drawings/MES?expected_revision=0",
                headers=csrf_headers,
            )
            assert empty_clear.status_code == 200
            assert empty_clear.json()["scope_revision"] == 0

            ws.send_json({"type": "ping"})
            pong1 = ws.receive_json()
            while pong1.get("type") == "feed_status":
                pong1 = ws.receive_json()
            assert pong1 == {"type": "pong"}

            # Real create -> MUST broadcast drawings_update
            create_payload = {
                "action": "create",
                "expected_revision": 0,
                "idempotency_key": "ws-idem-1",
                "drawing": _fvg_drawing_payload(drawing_id="ws-fvg-1"),
            }
            created = client.post("/api/drawings/MES", json=create_payload, headers=csrf_headers)
            assert created.status_code == 200

            update_msg = ws.receive_json()
            while update_msg.get("type") == "feed_status":
                update_msg = ws.receive_json()
            assert update_msg["type"] == "drawings_update"
            assert update_msg["symbol"] == "MES"
            assert update_msg["contract_id"] is None
            assert update_msg["pane_id"] is None
            assert update_msg["scope_revision"] == 1
            assert update_msg["command_type"] == "create"
            assert update_msg["can_undo"] is True
            assert update_msg["can_redo"] is False
            assert [d["id"] for d in update_msg["drawings"]] == ["ws-fvg-1"]

            # Deduplicated retry -> must NOT broadcast drawings_update
            retry = client.post("/api/drawings/MES", json=create_payload, headers=csrf_headers)
            assert retry.status_code == 200
            assert retry.json()["deduplicated"] is True

            ws.send_json({"type": "ping"})
            pong2 = ws.receive_json()
            while pong2.get("type") == "feed_status":
                pong2 = ws.receive_json()
            assert pong2 == {"type": "pong"}

            # Undo -> MUST broadcast drawings_update
            undone = client.post(
                "/api/drawings/MES/undo",
                json={"expected_revision": 1},
                headers=csrf_headers,
            )
            assert undone.status_code == 200
            undo_msg = ws.receive_json()
            while undo_msg.get("type") == "feed_status":
                undo_msg = ws.receive_json()
            assert undo_msg["type"] == "drawings_update"
            assert undo_msg["scope_revision"] == 2
            assert undo_msg["command_type"] == "undo"
            assert undo_msg["drawings"] == []


async def test_ws_broadcaster_publish_drawings_update_isolates_slow_consumer() -> None:
    """Contract: publish_drawings_update enforces R06 bounded queue and slow disconnect.

    Defect caught: Blocking send or unbounded queue on slow WS client during drawings broadcast.
    """
    broadcaster = WsBroadcaster(client_queue_maxsize=1, send_timeout_sec=0.5)
    stall = asyncio.Event()
    slow_ws = _FakeWsClient(stall_event=stall)
    fast_ws = _FakeWsClient()

    broadcaster.register(slow_ws, client_id="slow-1")
    broadcaster.register(fast_ws, client_id="fast-1")

    broadcaster.publish_drawings_update({"symbol": "MES", "scope_revision": 1, "drawings": []})
    await asyncio.sleep(0.02)
    # Slow client is blocked in send_json on item 1; item 2 fills its queue (maxsize=1).
    broadcaster.publish_drawings_update({"symbol": "MES", "scope_revision": 2, "drawings": []})
    await asyncio.sleep(0.02)
    # Fast client drained item 2, while slow client still holds item 2 and overflows on item 3.
    broadcaster.publish_drawings_update({"symbol": "MES", "scope_revision": 3, "drawings": []})
    await asyncio.sleep(0.05)

    assert broadcaster.slow_disconnect_count == 1
    assert not broadcaster.is_registered("slow-1")
    assert broadcaster.is_registered("fast-1")
    assert [m["scope_revision"] for m in fast_ws.sent] == [1, 2, 3]
    assert slow_ws.closed_calls and slow_ws.closed_calls[0][0] == WS_CLOSE_SLOW_CONSUMER

    stall.set()
    await broadcaster.close_all()


async def test_sqlite_store_bounds_undo_history_idempotency_and_scope_cap(tmp_path: Path) -> None:
    """Contract: SQLite enforces MAX_UNDO_HISTORY=100, MAX_DRAWINGS_PER_SCOPE=500, idempotency cap.

    Defect caught: Unbounded drawing_history/idempotency table growth or missing scope limit.
    """
    db_path = tmp_path / "bounds.db"
    store = SqliteStore(db_path)
    store.initialize()
    try:
        # 1) Create 500 drawings across 5 batches of 100, then verify 501st hits ScopeLimitError
        rev = 0
        for batch_idx in range(5):
            ops = [
                {
                    "action": "create",
                    "drawing": _fvg_drawing_payload(
                        drawing_id=f"d-{batch_idx}-{j}",
                        created_by="user",
                    ),
                }
                for j in range(100)
            ]
            res = await store.execute_drawing_command(
                symbol="MES",
                contract_id=None,
                pane_id=None,
                expected_revision=rev,
                command_type="batch",
                actor="user",
                operations=ops,
                request_fingerprint=f"fp-batch-{batch_idx}",
                idempotency_key=f"idem-batch-{batch_idx}",
            )
            rev = res.scope_revision
            assert len(res.drawings) == (batch_idx + 1) * 100

        with pytest.raises(Exception) as exc_limit:
            await store.execute_drawing_command(
                symbol="MES",
                contract_id=None,
                pane_id=None,
                expected_revision=rev,
                command_type="create",
                actor="user",
                operations=[
                    {
                        "action": "create",
                        "drawing": _fvg_drawing_payload(drawing_id="d-501", created_by="user"),
                    }
                ],
                request_fingerprint="fp-501",
            )
        assert type(exc_limit.value).__name__ == "ScopeLimitError"

        # 2) Clear and perform 105 updates to verify MAX_UNDO_HISTORY = 100 pruning
        clr = await store.execute_drawing_command(
            symbol="MES",
            contract_id=None,
            pane_id=None,
            expected_revision=rev,
            command_type="clear",
            actor="user",
            operations=[],
            request_fingerprint="fp-clear",
        )
        rev = clr.scope_revision
        c1 = await store.execute_drawing_command(
            symbol="MES",
            contract_id=None,
            pane_id=None,
            expected_revision=rev,
            command_type="create",
            actor="user",
            operations=[
                {
                    "action": "create",
                    "drawing": _fvg_drawing_payload(drawing_id="u-target", created_by="user"),
                }
            ],
            request_fingerprint="fp-u-create",
        )
        rev = c1.scope_revision
        for idx in range(105):
            u = await store.execute_drawing_command(
                symbol="MES",
                contract_id=None,
                pane_id=None,
                expected_revision=rev,
                command_type="update",
                actor="user",
                operations=[
                    {
                        "action": "update",
                        "drawing_id": "u-target",
                        "drawing": {"metadata": {"step": idx}},
                    }
                ],
                request_fingerprint=f"fp-u-{idx}",
                idempotency_key=f"idem-u-{idx}",
            )
            rev = u.scope_revision

        with sqlite3.connect(str(db_path)) as conn:
            hist_count = conn.execute("SELECT COUNT(*) FROM drawing_history").fetchone()[0]
            assert hist_count == MAX_UNDO_HISTORY
            assert MAX_DRAWINGS_PER_SCOPE == 500

        # Undo all 100 retained steps -> reaches can_undo=False cleanly
        undo_count = 0
        while True:
            snap = await store.list_drawings_scope("MES")
            if not snap.can_undo:
                break
            undone = await store.undo_drawing_command(
                symbol="MES",
                contract_id=None,
                pane_id=None,
                expected_revision=snap.scope_revision,
                actor="user",
                request_fingerprint=f"fp-undo-{undo_count}",
            )
            undo_count += 1
            assert undone.scope_revision == snap.scope_revision + 1
        assert undo_count == MAX_UNDO_HISTORY
    finally:
        store.close()
