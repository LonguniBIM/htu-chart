"""Unit tests for verify_app.py smoke probe fail-closed behavior (P1-006)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.scripts import verify_app


def test_cmd_smoke_fails_closed_on_unparseable_secret_bearing_status_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke fails closed when /api/status returns non-JSON or secret-bearing text.

    Defect caught: Unparseable 401 body containing raw secret passing smoke check silently.
    """

    def fake_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, "password=fake-secret-in-error"
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_request)
    out_dir = tmp_path / "smoke_fail"
    exit_code = verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir)
    assert exit_code == 1
    report = json.loads((out_dir / "smoke.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert "unparseable_status_body" in report["secret_leaks_detected"]
    assert "raw_body:password" in report["secret_leaks_detected"]


def test_cmd_smoke_fails_closed_on_plain_non_json_status_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke fails closed when /api/status returns non-JSON plain text.

    Defect caught: Skipping secret inspection when /api/status does not return valid JSON.
    """

    def fake_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, "Unauthorized"
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_request)
    out_dir = tmp_path / "smoke_non_json"
    exit_code = verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir)
    assert exit_code == 1
    report = json.loads((out_dir / "smoke.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert "unparseable_status_body" in report["secret_leaks_detected"]


def test_cmd_smoke_passes_on_clean_json_401_and_health_200(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke passes when health=200, status=401 JSON without leaks, and Order blocked.

    Defect caught: False failures on compliant F01 unauthenticated /api/status 401 JSON response.
    """

    def fake_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, '{"detail": "Authentication required"}'
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_request)
    out_dir = tmp_path / "smoke_pass"
    exit_code = verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir)
    assert exit_code == 0
    report = json.loads((out_dir / "smoke.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["health_endpoint_code"] == 200
    assert report["status_endpoint_code"] == 401
    assert report["secret_leaks_detected"] == []
    assert report["read_only_boundary_ok"] is True


def test_cmd_smoke_fails_on_json_status_with_inline_token_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke detects inline token= secrets inside valid JSON /api/status payloads.

    Defect caught: RAW_SECRET_VALUE_RE omitting 'token=' inside JSON error detail strings.
    """

    def fake_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, '{"detail": "token=raw-jwt"}'
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_request)
    out_dir = tmp_path / "smoke_inline_token"
    exit_code = verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir)
    assert exit_code == 1
    report = json.loads((out_dir / "smoke.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert "$.detail:raw_secret_pattern" in report["secret_leaks_detected"]


def test_cmd_smoke_fails_on_json_status_with_quoted_token_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke detects quoted JSON-style secret assignments inside JSON detail strings.

    Defect caught: RAW_SECRET_VALUE_RE missing closing quote in '{"token":"raw-jwt-leak"}'.
    """

    def fake_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, '{"detail": "{\\"token\\":\\"raw-jwt-leak\\"}"}'
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_request)
    out_dir = tmp_path / "smoke_quoted_token"
    exit_code = verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir)
    assert exit_code == 1
    report = json.loads((out_dir / "smoke.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert "$.detail:raw_secret_pattern" in report["secret_leaks_detected"]


def test_cmd_smoke_fails_on_access_token_and_password_hash_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: cmd_smoke fails on access_token and APP_PASSWORD_HASH JSON fields.

    Defect caught: SECRET_FIELD_NAMES missing access_token or case-insensitive env secret fields.
    """

    def fake_access_token(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, '{"access_token": "raw-jwt"}'
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_access_token)
    out_dir_1 = tmp_path / "smoke_access_token"
    assert verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir_1) == 1
    report_1 = json.loads((out_dir_1 / "smoke.json").read_text(encoding="utf-8"))
    assert report_1["passed"] is False
    assert "$.access_token" in report_1["secret_leaks_detected"]

    def fake_hash(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
        if url.endswith("/api/health"):
            return 200, '{"status": "ok"}'
        if url.endswith("/api/status"):
            return 401, '{"APP_PASSWORD_HASH": "$argon2id$fake"}'
        return 404, '{"detail": "Not Found"}'

    monkeypatch.setattr(verify_app, "_http_request", fake_hash)
    out_dir_2 = tmp_path / "smoke_pw_hash"
    assert verify_app.cmd_smoke("http://127.0.0.1:18456", out_dir_2) == 1
    report_2 = json.loads((out_dir_2 / "smoke.json").read_text(encoding="utf-8"))
    assert report_2["passed"] is False
    assert "$.APP_PASSWORD_HASH" in report_2["secret_leaks_detected"]
