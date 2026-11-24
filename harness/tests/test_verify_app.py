"""Unit tests for verify_app.py smoke probe fail-closed behavior (P1-006)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.scripts import codex_room_review, codex_room_sync, receipt, verify_app
from harness.scripts.schema_utils import run_managed_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SAFETY_HOOK = REPO_ROOT / "harness" / "scripts" / "safety_hook.py"
OVERLAYS_DIR = REPO_ROOT / "harness" / "codex-room" / "overlays"


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


def _seed_doctor_workspace(base: Path, *, hook_cmd: str | None = None) -> Path:
    lwc_bundle = (
        base
        / "lightweight-charts"
        / "dist"
        / "lightweight-charts.standalone.production.js"
    )
    lwc_bundle.parent.mkdir(parents=True, exist_ok=True)
    lwc_bundle.write_text("// bundle\n", encoding="utf-8")

    app_root = base / "topstep-chart-app"
    (app_root / "backend").mkdir(parents=True, exist_ok=True)
    (app_root / "backend" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (app_root / "frontend").mkdir(parents=True, exist_ok=True)
    (app_root / "frontend" / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (app_root / "harness" / "scripts").mkdir(parents=True, exist_ok=True)
    (app_root / "harness" / "scripts" / "safety_hook.py").write_text(
        REAL_SAFETY_HOOK.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    if hook_cmd is not None:
        hooks_cfg = {
            "topstep-safety-guard": {
                "PreToolUse": [
                    {
                        "matcher": (
                            "write_file|edit_file|replace_file_content|"
                            "multi_replace_file_content|write_to_file|"
                            "run_command|swarm|invoke_subagent"
                        ),
                        "hooks": [{"type": "command", "command": hook_cmd, "timeout": 10}],
                    }
                ]
            }
        }
        (app_root / ".agents").mkdir(parents=True, exist_ok=True)
        (app_root / ".agents" / "hooks.json").write_text(
            json.dumps(hooks_cfg, indent=2) + "\n",
            encoding="utf-8",
        )
    return app_root


def _valid_model_policy_dict() -> dict:
    return {
        "schema_version": "2.0.0",
        "effective_date": "2026-11-24",
        "excluded_model_prefixes": [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "gemini-pro-orchestrator-argon",
            "gemini-3.7-flash",
            "gemini-2.5-pro",
        ],
        "discontinued_peer_subagent_tiers": ["flash", "flash_lite"],
        "roles": {
            "cli_coordinator": {"selected_model": "gemini-4-neon-medium"},
            "peer_default": {"dispatch_tool": "swarm", "selected_model": "gemini-4-hydrogen-high"},
            "independent_reviewer_primary": {"backend": "codex", "selected_model": "gpt-6.1-sol"},
            "independent_reviewer_fallbacks": {"qualified_routes": []},
        },
    }


def test_doctor_validates_hooks_and_model_policy_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: verify_app doctor fails closed if hooks.json or model-policy.json is broken.

    Defect caught: Missing/corrupt hooks or model-policy passing doctor silently.
    """
    monkeypatch.setattr(verify_app, "_tool_path", lambda name: f"/stub/{name}")
    monkeypatch.setattr(
        verify_app,
        "verify_role",
        lambda role, rt, **kw: {"role": role, "ok": True, "errors": []},
    )

    app_missing = _seed_doctor_workspace(tmp_path / "case_missing")
    rep_missing = verify_app.run_doctor(app_missing, tmp_path / "rt1")
    assert rep_missing["ok"] is False
    assert rep_missing["hooks"]["ok"] is False
    assert rep_missing["model_policy"]["ok"] is False

    app_ok = _seed_doctor_workspace(
        tmp_path / "case_ok",
        hook_cmd="python ../harness/scripts/safety_hook.py",
    )
    (app_ok / "harness" / "model-policy.json").write_text(
        json.dumps(_valid_model_policy_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    rep_ok = verify_app.run_doctor(app_ok, tmp_path / "rt2")
    assert rep_ok["hooks"]["ok"] is True
    assert rep_ok["model_policy"]["ok"] is True
    assert rep_ok["ok"] is True

    # P1-004: display-name excluded model in active role fails model_policy check
    bad_display_pol = _valid_model_policy_dict()
    bad_display_pol["roles"]["peer_default"]["selected_model"] = "Gemini 4 Argon Medium"
    (app_ok / "harness" / "model-policy.json").write_text(
        json.dumps(bad_display_pol, indent=2) + "\n",
        encoding="utf-8",
    )
    rep_disp = verify_app.run_doctor(app_ok, tmp_path / "rt2")
    assert rep_disp["model_policy"]["ok"] is False
    assert rep_disp["ok"] is False

    (app_ok / "harness" / "model-policy.json").write_text("{bad json", encoding="utf-8")
    rep_bad_policy = verify_app.run_doctor(app_ok, tmp_path / "rt2")
    assert rep_bad_policy["model_policy"]["ok"] is False
    assert rep_bad_policy["ok"] is False

    # Restore valid policy, narrow matcher regex (P1-006)
    (app_ok / "harness" / "model-policy.json").write_text(
        json.dumps(_valid_model_policy_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    hooks_narrow = json.loads((app_ok / ".agents" / "hooks.json").read_text(encoding="utf-8"))
    hooks_narrow["topstep-safety-guard"]["PreToolUse"][0]["matcher"] = "write_file"
    (app_ok / ".agents" / "hooks.json").write_text(
        json.dumps(hooks_narrow, indent=2) + "\n",
        encoding="utf-8",
    )
    rep_narrow = verify_app.run_doctor(app_ok, tmp_path / "rt2")
    assert rep_narrow["hooks"]["ok"] is False
    assert rep_narrow["ok"] is False

    # Restore full matcher, break hook command so it allows /api/Order/place
    (app_ok / "harness" / "scripts" / "permissive_hook.py").write_text(
        'import json; print(json.dumps({"decision": "allow"}))\n',
        encoding="utf-8",
    )
    hooks_broken = json.loads((app_ok / ".agents" / "hooks.json").read_text(encoding="utf-8"))
    hooks_broken["topstep-safety-guard"]["PreToolUse"][0]["matcher"] = (
        "write_file|edit_file|replace_file_content|multi_replace_file_content|"
        "write_to_file|run_command|swarm|invoke_subagent"
    )
    hooks_broken["topstep-safety-guard"]["PreToolUse"][0]["hooks"][0]["command"] = (
        "python ../harness/scripts/permissive_hook.py"
    )
    (app_ok / ".agents" / "hooks.json").write_text(
        json.dumps(hooks_broken, indent=2) + "\n",
        encoding="utf-8",
    )
    rep_bad_hook = verify_app.run_doctor(app_ok, tmp_path / "rt2")
    assert rep_bad_hook["hooks"]["ok"] is False
    assert rep_bad_hook["ok"] is False


def test_doctor_on_repo_root_reports_hooks_and_model_policy_ok(tmp_path: Path) -> None:
    """Contract: Deployed repository root passes hooks and model_policy doctor checks.

    Defect caught: Missing .agents/hooks.json or harness/model-policy.json in repository.
    """
    rep = verify_app.run_doctor(REPO_ROOT, tmp_path / "rt_repo")
    assert rep["hooks"]["ok"] is True
    assert rep["model_policy"]["ok"] is True


def _init_mini_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    run_managed_cmd(["git", "init"], cwd=repo, timeout_sec=15.0)
    run_managed_cmd(["git", "config", "user.email", "t@example.com"], cwd=repo, timeout_sec=15.0)
    run_managed_cmd(["git", "config", "user.name", "Tester"], cwd=repo, timeout_sec=15.0)
    (repo / "harness").mkdir(parents=True, exist_ok=True)
    (repo / "harness" / "model-policy.json").write_text(
        json.dumps(_valid_model_policy_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    run_managed_cmd(["git", "add", "."], cwd=repo, timeout_sec=15.0)
    run_managed_cmd(["git", "commit", "-m", "init"], cwd=repo, timeout_sec=15.0)
    proc = run_managed_cmd(["git", "rev-parse", "HEAD"], cwd=repo, timeout_sec=15.0)
    return proc.stdout.strip()


def test_policy_consumers_fail_closed_on_excluded_models_and_corrupt_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Contract: codex_room_sync, codex_room_review, and receipt reject excluded models.

    Defect caught: Excluded review_model, display names, or corrupt policy allowed through gates.
    """
    repo = _init_mini_git_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"

    def _forbid_cli() -> list[str]:
        raise AssertionError("External CLI must not be invoked when policy rejects model")

    monkeypatch.setattr(codex_room_review, "resolve_agy_command", _forbid_cli)
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", _forbid_cli)

    for idx, fb_model in enumerate(("gemini-3.8-flash-high", "Gemini 4 Argon Medium")):
        rc_fb = codex_room_review.main(
            [
                "--commit",
                "HEAD",
                "--repo-root",
                str(repo_path),
                "--backend",
                "agy",
                "--fallback-model",
                fb_model,
                "--out-dir",
                str(tmp_path / f"out_fb_{idx}"),
            ]
        )
        assert rc_fb == 2

    rc_mod = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_path),
            "--model",
            "gemini-4-argon-high",
            "--out-dir",
            str(tmp_path / "out_mod"),
        ]
    )
    assert rc_mod == 2

    verifier_ok = {
        "status": "VERIFIED",
        "phase": 0,
        "git_sha": repo,
        "worktree_dirty": False,
        "steps": [
            {"name": "arch_check", "command": ["arch"], "status": "PASSED", "exit_code": 0},
            {"name": "harness_ruff", "command": ["ruff"], "status": "PASSED", "exit_code": 0},
            {"name": "harness_pytest", "command": ["pytest"], "status": "PASSED", "exit_code": 0},
        ],
    }
    for bad_rev_model in ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"):
        review_excl = {
            "revision_sha": repo,
            "passes_completed": 2,
            "independent_contexts": True,
            "review_gate_status": "PASSED",
            "review_backend": "agy",
            "review_model": bad_rev_model,
            "consolidated_findings": [],
        }
        rec = receipt.evaluate_receipt(
            task_id="T-POL",
            phase=0,
            base_sha="0" * 40,
            candidate_sha=repo,
            verifier_data=verifier_ok,
            review_data=review_excl,
            repo_root=repo_path,
        )
        assert rec["conclusion"] == "BLOCKED"

    base_home = tmp_path / "base_codex"
    rt = tmp_path / "rt"
    base_home.mkdir()
    codex_room_sync.sync_role(
        "pass1",
        base_home=base_home,
        runtime_root=rt,
        overlays_dir=OVERLAYS_DIR,
        repo_root=repo_path,
    )
    assert codex_room_sync.verify_role("pass1", rt, repo_root=repo_path)["ok"] is True

    (repo_path / "harness" / "model-policy.json").write_text("{corrupt", encoding="utf-8")
    assert codex_room_sync.verify_role("pass1", rt, repo_root=repo_path)["ok"] is False
