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
