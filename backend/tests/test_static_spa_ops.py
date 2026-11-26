"""Value-first tests for Phase 5 Part 2 SPA static serving and 24/7 Home Host ops kit."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from argon2 import PasswordHasher

from app.config import Settings
from app.main import _resolve_frontend_dist_dir, create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_PASSWORD = "spa-ops-test-password-123"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)


@asynccontextmanager
async def _spa_client(settings: Settings):
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
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
                yield client
    finally:
        await tg_client.aclose()
        await upstream.aclose()


async def test_spa_static_serving_preserves_api_404_and_blocks_traversal(
    tmp_path: Path,
) -> None:
    """Contract: Configured frontend_dist_dir serves SPA routes and assets while keeping
    /api/* 404s intact and blocking path traversal outside dist_dir.
    Defect caught: Catch-all SPA route shadowing /api/* 404 or leaking parent host files.
    """
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP_SECRET_HOST_FILE", encoding="utf-8")

    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body>SPA_INDEX_OK</body></html>",
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('spa-asset');", encoding="utf-8")

    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        frontend_dist_dir=dist_dir,
        signalr_enabled=False,
        alert_scanner_enabled=False,
        alert_dispatcher_enabled=False,
    )
    assert _resolve_frontend_dist_dir(settings) == dist_dir.resolve()

    async with _spa_client(settings) as client:
        health_resp = await client.get("/api/health")
        assert health_resp.status_code == 200
        assert health_resp.json() == {"status": "ok"}

        root_resp = await client.get("/")
        assert root_resp.status_code == 200
        assert "SPA_INDEX_OK" in root_resp.text

        deep_resp = await client.get("/workspace/replay")
        assert deep_resp.status_code == 200
        assert "SPA_INDEX_OK" in deep_resp.text

        asset_resp = await client.get("/assets/app.js")
        assert asset_resp.status_code == 200
        assert "spa-asset" in asset_resp.text

        api_missing = await client.get("/api/does-not-exist")
        assert api_missing.status_code == 404
        assert "SPA_INDEX_OK" not in api_missing.text

        ws_missing = await client.get("/ws/does-not-exist")
        assert ws_missing.status_code == 404
        assert "SPA_INDEX_OK" not in ws_missing.text

        traversal_encoded = await client.get("/%2e%2e/secret.txt")
        assert traversal_encoded.status_code == 404
        assert "TOP_SECRET_HOST_FILE" not in traversal_encoded.text

        traversal_nested = await client.get("/assets/%2e%2e/%2e%2e/secret.txt")
        assert traversal_nested.status_code == 404
        assert "TOP_SECRET_HOST_FILE" not in traversal_nested.text


def test_resolve_frontend_dist_dir_returns_none_when_missing(tmp_path: Path) -> None:
    """Contract: Missing dist directory or missing index.html disables SPA static mount.
    Defect caught: Crashing create_app() when frontend/dist has not been built yet.
    """
    missing_dir = tmp_path / "missing-dist"
    settings = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        frontend_dist_dir=missing_dir,
    )
    assert _resolve_frontend_dist_dir(settings) is None

    empty_dir = tmp_path / "empty-dist"
    empty_dir.mkdir()
    settings_empty = Settings(
        app_password_hash=TEST_PASSWORD_HASH,
        data_dir=tmp_path / "data",
        frontend_dist_dir=empty_dir,
    )
    assert _resolve_frontend_dist_dir(settings_empty) is None


def test_ops_kit_enforces_sec003_loopback_tailscale_and_single_worker() -> None:
    """Contract: Dockerfile, docker-compose.yml, .env.example, and SETUP.md contain zero
    wildcard all-interface binds and enforce single-worker APP_BIND_HOST deployment.
    Defect caught: Regression to wildcard bind or multi-worker Uvicorn config.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    setup_md = (REPO_ROOT / "SETUP.md").read_text(encoding="utf-8")

    wildcard_ipv4 = ".".join(["0", "0", "0", "0"])
    for name, content in (
        ("Dockerfile", dockerfile),
        ("docker-compose.yml", compose),
        (".env.example", env_example),
        ("SETUP.md", setup_md),
    ):
        assert wildcard_ipv4 not in content, f"{name} must never reference wildcard {wildcard_ipv4}"

    assert "APP_BIND_HOST=127.0.0.1" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/api/health" in dockerfile
    assert "app.main" in dockerfile
    assert "--workers 1" in dockerfile

    assert "network_mode: host" in compose
    assert "topstep-chart-app/Dockerfile" in compose
    assert re.search(r"env_file:\s*\n\s*-\s*\.env", compose)

    assert "APP_BIND_HOST=127.0.0.1" in env_example
    assert "BACKTEST_ENABLED=true" in env_example
    assert "BACKTEST_MAX_QUEUE_DEPTH=3" in env_example
    assert "BACKTEST_MAX_BARS=50000" in env_example

    assert "Tailscale" in setup_md
    assert "APP_BIND_HOST" in setup_md
    assert "--workers 1" in setup_md
    assert "APP_COOKIE_SECURE=true" in setup_md
    assert "BACKTEST_ENABLED=false" in setup_md
