"""Tests for Codex Room runtime sync isolation and 2-pass review consolidation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from harness.scripts import codex_room_review
from harness.scripts.codex_room_review import (
    DEFAULT_AGY_FALLBACK_MODEL,
    _build_parser,
    _detect_codex_quota_error,
    _extract_agy_structured_output,
    _run_agy_pass,
    _run_codex_pass,
    consolidate_reviews,
    validate_strict_json_schema,
)
from harness.scripts.codex_room_sync import sync_role, verify_role

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "harness" / "codex-room" / "schemas" / "review-output.schema.json"
OVERLAYS_DIR = REPO_ROOT / "harness" / "codex-room" / "overlays"


def test_sync_isolates_runtime_forces_readonly_and_never_touches_base_home(tmp_path: Path) -> None:
    base_home = tmp_path / "base_codex"
    runtime_root = tmp_path / "codex_runtime"
    base_home.mkdir()

    base_toml = "\n".join(
        [
            'model = "gpt-6-astra"',
            'sandbox_mode = "danger-full-access"',
            'approval_policy = "on-request"',
            "",
            "http_headers = {",
            '  x-openai-actor-authorization = "cockpit-tools",',
            '  x-cockpit-instance-id = ".codex",',
            "}",
            "",
            "[features]",
            "multi_agent = true",
            "multi_agent_v2 = true",
            "agent_message_board = true",
            "",
            "[agents.helper]",
            'model = "gpt-6.1-sol"',
            "",
            '[projects."D:\\\\01_DCMvn\\\\Workspace\\\\Lightchart"]',
            'trust_level = "trusted"',
            "",
            "[mcp_servers.node_repl.env]",
            """NODE_REPL_TRUSTED_SERVICES = '{"browser":"C:/a/b.mjs","sky":"@oai/sky"}'""",
        ]
    )
    (base_home / "config.toml").write_text(base_toml, encoding="utf-8")
    (base_home / "auth.json").write_text('{"token":"synthetic-auth"}\n', encoding="utf-8")

    base_cfg_sha_before = hashlib.sha256((base_home / "config.toml").read_bytes()).hexdigest()
    auth_sha_before = hashlib.sha256((base_home / "auth.json").read_bytes()).hexdigest()

    for role in ("pass1", "pass2"):
        receipt = sync_role(role=role, base_home=base_home, runtime_root=runtime_root, overlays_dir=OVERLAYS_DIR)
        assert receipt["auth_copied"] is True
        assert verify_role(role, runtime_root)["ok"] is True

        runtime_dir = Path(receipt["runtime_dir"])
        parsed = tomllib.loads((runtime_dir / "config.toml").read_text(encoding="utf-8"))
        assert parsed["sandbox_mode"] == "read-only"
        assert parsed["approval_policy"] == "never"
        assert parsed["features"]["multi_agent"] is False
        assert parsed["features"]["multi_agent_v2"] is False
        assert parsed["features"]["agent_message_board"] is False
        assert "agents" not in parsed
        assert parsed["projects"][r"D:\01_DCMvn\Workspace\Lightchart"]["trust_level"] == "trusted"
        assert parsed["http_headers"]["x-cockpit-instance-id"] == ".codex"
        assert parsed["mcp_servers"]["node_repl"]["env"]["NODE_REPL_TRUSTED_SERVICES"].startswith('{"browser"')
        assert "READ-ONLY" in parsed["developer_instructions"]
        assert (runtime_dir / "auth.json").read_text(encoding="utf-8") == '{"token":"synthetic-auth"}\n'

    assert hashlib.sha256((base_home / "config.toml").read_bytes()).hexdigest() == base_cfg_sha_before
    assert hashlib.sha256((base_home / "auth.json").read_bytes()).hexdigest() == auth_sha_before


def test_verify_role_rejects_tampered_multi_agent_flag(tmp_path: Path) -> None:
    base_home = tmp_path / "base"
    runtime_root = tmp_path / "rt"
    base_home.mkdir()
    sync_role("pass1", base_home=base_home, runtime_root=runtime_root, overlays_dir=OVERLAYS_DIR)

    cfg_path = runtime_root / "topstep-review-pass1" / "config.toml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace("multi_agent = false", "multi_agent = true"),
        encoding="utf-8",
    )
    res = verify_role("pass1", runtime_root)
    assert res["ok"] is False
    assert any("multi_agent" in err for err in res["errors"])


def test_review_schema_meets_strict_structured_outputs_rules() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert validate_strict_json_schema(schema) == []


def _sample_pass(sha: str, lens: str, verdict: str = "APPROVE", findings: list[dict] | None = None) -> dict:
    return {
        "revision_sha": sha,
        "lens": lens,
        "verdict": verdict,
        "summary": "Review completed.",
        "findings": findings or [],
        "test_quality_issues": [],
        "unverified_areas": [],
    }


def _sample_finding(fid: str, sev: str, title: str, file: str = "backend/app/security.py", line: int = 42) -> dict:
    return {
        "id": fid,
        "severity": sev,
        "category": "security",
        "file": file,
        "line_start": line,
        "line_end": line + 3,
        "title": title,
        "behavior_affected": "Auth check",
        "trigger_condition": "Missing CSRF header",
        "evidence": f"{file}:{line} skips CSRF",
        "verification_method": "Send POST without X-CSRF-Token",
        "plan_ref": "F01",
    }


def test_consolidate_blocks_on_stale_or_missing_pass() -> None:
    sha = "a" * 40
    stale = "b" * 40
    res_stale = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests"),
        _sample_pass(stale, "security_and_invariants"),
        expected_sha=sha,
    )
    assert res_stale["review_gate_status"] == "BLOCKED"
    assert any("mismatch" in e for e in res_stale["errors"])

    res_missing = consolidate_reviews(_sample_pass(sha, "correctness_and_tests"), None, expected_sha=sha)
    assert res_missing["review_gate_status"] == "BLOCKED"


def test_consolidate_dedups_matching_findings_and_blocks_single_pass_finding() -> None:
    sha = "c" * 40
    f1 = _sample_finding("P1-01", "medium", "Missing CSRF header validation")
    f2_dup = _sample_finding("P2-01", "high", "  missing csrf header validation ")
    f2_only = _sample_finding("P2-02", "critical", "Unredacted apiKey in exception", line=88)

    res = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", [f1]),
        _sample_pass(sha, "security_and_invariants", "CHANGES_REQUESTED", [f2_dup, f2_only]),
        expected_sha=sha,
    )
    assert res["review_gate_status"] == "CHANGES_REQUESTED"
    assert res["blocking_open_count"] == 2
    assert len(res["consolidated_findings"]) == 2
    deduped = next(item for item in res["consolidated_findings"] if item["line_start"] == 42)
    assert deduped["severity"] == "high"
    assert deduped["detected_by"] == ["pass1", "pass2"]
    single = next(item for item in res["consolidated_findings"] if item["line_start"] == 88)
    assert single["detected_by"] == ["pass2"]
    assert single["status"] == "OPEN"


def test_consolidate_passes_only_when_both_passes_clear_blocking_findings() -> None:
    sha = "d" * 40
    advisory = _sample_finding("P1-01", "low", "Rename helper for clarity")
    res = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "APPROVE", [advisory]),
        _sample_pass(sha, "security_and_invariants", "APPROVE", []),
        expected_sha=sha,
    )
    assert res["review_gate_status"] == "PASSED"
    assert res["blocking_open_count"] == 0
    assert res["advisory_count"] == 1


def _write_fake_codex_script(script_path: Path, body: str) -> list[str]:
    script_path.write_text(body, encoding="utf-8")
    return [sys.executable, "-u", str(script_path)]


def _init_mini_git_repo(repo_dir: Path) -> tuple[Path, str]:
    import subprocess

    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("mini repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo_dir, sha


def test_run_codex_pass_resets_idle_timeout_on_streaming_activity(tmp_path: Path) -> None:
    """When stdout streams events faster than idle_timeout_sec, total runtime > idle_timeout_sec succeeds."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    script = tmp_path / "fake_codex_active.py"
    prefix = _write_fake_codex_script(
        script,
        (
            "import json, sys, time\n"
            "args = sys.argv[1:]\n"
            "out_path = args[args.index('-o') + 1]\n"
            "_ = sys.stdin.read()\n"
            "for i in range(6):\n"
            "    print(json.dumps({'event': 'tick', 'i': i}), flush=True)\n"
            "    time.sleep(0.12)\n"
            "with open(out_path, 'w', encoding='utf-8') as fh:\n"
            "    json.dump({'revision_sha': 'a'*40, 'lens': 'correctness_and_tests', 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}, fh)\n"
        ),
    )

    result = _run_codex_pass(
        codex_prefix=prefix,
        role="pass1",
        runtime_dir=runtime_dir,
        repo_root=REPO_ROOT,
        schema_path=SCHEMA_PATH,
        prompt_text="review prompt",
        out_dir=out_dir,
        pass_name="pass1",
        model=None,
        timeout_sec=10.0,
        idle_timeout_sec=0.45,
    )
    assert result is not None
    assert result["verdict"] == "APPROVE"
    raw_lines = (out_dir / "pass1.raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 6


def test_run_codex_pass_terminates_on_idle_timeout_and_preserves_logs(tmp_path: Path) -> None:
    """When process stops emitting output past idle_timeout_sec, it is terminated cleanly with diagnostics."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    script = tmp_path / "fake_codex_hang.py"
    prefix = _write_fake_codex_script(
        script,
        (
            "import sys, time\n"
            "_ = sys.stdin.read()\n"
            "print('{\"event\":\"started\"}', flush=True)\n"
            "time.sleep(30)\n"
        ),
    )

    result = _run_codex_pass(
        codex_prefix=prefix,
        role="pass1",
        runtime_dir=runtime_dir,
        repo_root=REPO_ROOT,
        schema_path=SCHEMA_PATH,
        prompt_text="review prompt",
        out_dir=out_dir,
        pass_name="pass1",
        model=None,
        timeout_sec=10.0,
        idle_timeout_sec=0.35,
    )
    assert result is None
    assert "started" in (out_dir / "pass1.raw.jsonl").read_text(encoding="utf-8")
    assert "idle_timeout=" in (out_dir / "pass1.stderr.log").read_text(encoding="utf-8")


def test_run_codex_pass_enforces_hard_total_timeout_cap(tmp_path: Path) -> None:
    """Even with continuous stdout activity, hard timeout_sec terminates the pass cleanly."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    script = tmp_path / "fake_codex_spin.py"
    prefix = _write_fake_codex_script(
        script,
        (
            "import sys, time\n"
            "_ = sys.stdin.read()\n"
            "for i in range(200):\n"
            "    print(f'{{\"i\":{i}}}', flush=True)\n"
            "    time.sleep(0.05)\n"
        ),
    )

    result = _run_codex_pass(
        codex_prefix=prefix,
        role="pass2",
        runtime_dir=runtime_dir,
        repo_root=REPO_ROOT,
        schema_path=SCHEMA_PATH,
        prompt_text="review prompt",
        out_dir=out_dir,
        pass_name="pass2",
        model=None,
        timeout_sec=0.45,
        idle_timeout_sec=5.0,
    )
    assert result is None
    assert "total_timeout=" in (out_dir / "pass2.stderr.log").read_text(encoding="utf-8")


def test_codex_room_review_parser_defaults_enable_parallel_and_idle_watchdog() -> None:
    args = _build_parser().parse_args([])
    assert args.timeout == 1200.0
    assert args.idle_timeout == 180.0
    assert args.sequential is False
    assert args.backend == "auto"
    assert args.fallback_model is None
    assert args.resume_from is None


def test_extract_agy_structured_output_parses_stream_json_and_rejects_failed_status() -> None:
    sha = "e" * 40
    payload = _sample_pass(sha, "correctness_and_tests")
    stream_lines = [
        json.dumps({"event": "init", "init": {"model": DEFAULT_AGY_FALLBACK_MODEL}}) + "\n",
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "structured_output": payload,
                    "response": json.dumps(payload),
                },
            }
        )
        + "\n",
    ]
    assert _extract_agy_structured_output(stream_lines) == payload

    fenced_lines = [
        json.dumps(
            {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": f"```json\n{json.dumps(payload)}\n```",
                },
            }
        )
        + "\n",
    ]
    assert _extract_agy_structured_output(fenced_lines) == payload

    failed_lines = [
        json.dumps({"event": "result", "result": {"status": "ERROR", "structured_output": payload}}) + "\n",
    ]
    assert _extract_agy_structured_output(failed_lines) is None


def test_run_agy_pass_passes_readonly_flags_and_writes_schema_json(tmp_path: Path) -> None:
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    prompt_file = out_dir / "pass1.prompt.md"
    prompt_file.write_text("review prompt", encoding="utf-8")
    args_capture = tmp_path / "captured_args.json"
    sha = "f" * 40
    script = tmp_path / "fake_agy.py"
    prefix = _write_fake_codex_script(
        script,
        (
            "import json, sys\n"
            f"with open({str(args_capture)!r}, 'w', encoding='utf-8') as fh:\n"
            "    json.dump(sys.argv[1:], fh)\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': 'correctness_and_tests', 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'init', 'init': {'model': 'claude-opus-5-5-medium'}}), flush=True)\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    result = _run_agy_pass(
        agy_prefix=prefix,
        role="pass1",
        repo_root=REPO_ROOT,
        schema_path=SCHEMA_PATH,
        prompt_text="review prompt",
        prompt_file=prompt_file,
        out_dir=out_dir,
        pass_name="pass1",
        model="claude-opus-5-5-medium",
        timeout_sec=10.0,
        idle_timeout_sec=5.0,
    )
    assert result is not None
    assert result["revision_sha"] == sha
    assert json.loads((out_dir / "pass1.json").read_text(encoding="utf-8")) == result
    captured = json.loads(args_capture.read_text(encoding="utf-8"))
    assert "--sandbox" in captured
    assert "--mode" in captured and captured[captured.index("--mode") + 1] == "plan"
    assert "--model" in captured and captured[captured.index("--model") + 1] == "claude-opus-5-5-medium"
    assert "--output-format" in captured and captured[captured.index("--output-format") + 1] == "stream-json"
    assert "--json-schema" in captured


def test_auto_backend_prioritizes_codex_and_falls_back_to_agy_on_quota_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir, head_sha = _init_policy_repo(
        tmp_path / "repo_a", qualified_routes=[DEFAULT_AGY_FALLBACK_MODEL]
    )
    agy_called = tmp_path / "agy_called.flag"

    codex_ok_script = tmp_path / "fake_codex_ok.py"
    codex_ok_prefix = _write_fake_codex_script(
        codex_ok_script,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = args[args.index('-o') + 1]\n"
            "prompt = sys.stdin.read()\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'codex ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "with open(out_path, 'w', encoding='utf-8') as fh:\n"
            "    json.dump(payload, fh)\n"
        ),
    )

    codex_quota_script = tmp_path / "fake_codex_quota.py"
    codex_quota_prefix = _write_fake_codex_script(
        codex_quota_script,
        (
            "import json, sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'type': 'error', "
            "'message': 'usage_limit_reached - Codex CLI quota exhausted'}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )

    codex_crash_script = tmp_path / "fake_codex_crash.py"
    codex_crash_prefix = _write_fake_codex_script(
        codex_crash_script,
        (
            "import sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "_ = sys.stdin.read()\n"
            "sys.stderr.write('Error: unexpected internal parser failure\\n')\n"
            "sys.exit(1)\n"
        ),
    )

    agy_ok_script = tmp_path / "fake_agy_ok.py"
    agy_ok_prefix = _write_fake_codex_script(
        agy_ok_script,
        (
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(agy_called)!r}).write_text('called', encoding='utf-8')\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy opus ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []})
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_ok_prefix)

    # Case A: Codex CLI healthy -> auto prioritizes Codex CLI and never calls agy.
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_ok_prefix)
    out_a = tmp_path / "out_codex_ok"
    rc_a = codex_room_review.main(["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_a), "--backend", "auto"])
    assert rc_a == 0
    report_a = json.loads((out_a / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_a["review_backend"] == "codex"
    assert report_a["fallback_used"] is False
    assert not agy_called.exists()

    # Case B: Codex CLI hits quota -> fallback to qualified agy route and archive failed logs.
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_quota_prefix)
    out_b = tmp_path / "out_auto_fallback"
    rc_b = codex_room_review.main(["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_b), "--backend", "auto"])
    assert rc_b == 0
    assert agy_called.exists()
    assert (out_b / "pass1.codex-failed.raw.jsonl").is_file()
    report_b = json.loads((out_b / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_b["review_gate_status"] == "PASSED"
    assert report_b["review_backend"] == "agy"
    assert report_b["review_model"] == DEFAULT_AGY_FALLBACK_MODEL
    assert report_b["fallback_used"] is True
    assert "usage_limit_reached" in str(report_b["fallback_reason"])

    # Case C: --backend codex never falls back even on quota exhaustion.
    agy_called.unlink()
    out_c = tmp_path / "out_strict_codex"
    rc_c = codex_room_review.main(["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_c), "--backend", "codex"])
    assert rc_c == 2
    assert not agy_called.exists()
    assert _detect_codex_quota_error(out_c) is not None

    # Case D: --backend auto does NOT silently fall back on non-quota Codex failure.
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_crash_prefix)
    out_d = tmp_path / "out_non_quota_fail"
    rc_d = codex_room_review.main(["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_d), "--backend", "auto"])
    assert rc_d == 2
    assert not agy_called.exists()


def test_codex_partial_blocking_finding_preserved_across_agy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When one Codex pass returns a blocking finding and the other hits quota, fallback must not erase it."""
    repo_dir, head_sha = _init_policy_repo(
        tmp_path / "repo_partial", qualified_routes=["gemini-4-carbon-high"]
    )

    codex_partial_script = tmp_path / "fake_codex_partial.py"
    codex_partial_prefix = _write_fake_codex_script(
        codex_partial_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {head_sha!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'CHANGES_REQUESTED', 'summary': 'found blocking bug', "
            "'findings': [{'id': 'C-1', 'severity': 'high', 'category': 'bug', 'file': 'backend/app/main.py', "
            "'line_start': 10, 'line_end': 12, 'title': 'Blocking bug from Codex pass1', "
            "'behavior_affected': 'b', 'trigger_condition': 't', 'evidence': 'e', "
            "'verification_method': 'v', 'plan_ref': 'F03'}], "
            "'test_quality_issues': ['codex tqi'], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "    print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
            "else:\n"
            "    print(json.dumps({'type': 'error', "
            "'message': 'usage_limit_reached - Codex CLI quota exhausted'}), flush=True)\n"
            "    sys.exit(1)\n"
        ),
    )

    agy_approve_script = tmp_path / "fake_agy_approve.py"
    agy_approve_prefix = _write_fake_codex_script(
        agy_approve_script,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy approve', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []})
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_partial_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_approve_prefix)

    out_dir = tmp_path / "out_partial_fallback"
    rc = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--backend", "auto", "--fail-on-blocking"]
    )
    assert rc == 1
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "CHANGES_REQUESTED"
    assert report["blocking_open_count"] == 1
    assert any(f["id"] == "C-1" and f["status"] == "OPEN" for f in report["consolidated_findings"])
    assert "codex tqi" in report["test_quality_issues"]


def test_consolidate_blocks_incomplete_payload_swapped_lenses_and_unknown_severity() -> None:
    """NC-01 & NC-02: Incomplete schema, swapped lenses, or unknown severity must block consolidation."""
    sha = "a" * 40
    incomplete_p1 = {"revision_sha": sha, "lens": "correctness_and_tests"}
    valid_p2 = _sample_pass(sha, "security_and_invariants")
    res_incomplete = consolidate_reviews(incomplete_p1, valid_p2, expected_sha=sha)
    assert res_incomplete["review_gate_status"] == "BLOCKED"
    assert res_incomplete["errors"]

    swapped = consolidate_reviews(
        _sample_pass(sha, "security_and_invariants"),
        _sample_pass(sha, "correctness_and_tests"),
        expected_sha=sha,
    )
    assert swapped["review_gate_status"] == "BLOCKED"
    assert swapped["independent_contexts"] is False

    bad_sev_finding = _sample_finding("P1-77", "blocker", "Unknown severity label")
    res_bad_sev = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", [bad_sev_finding]),
        _sample_pass(sha, "security_and_invariants", "APPROVE", []),
        expected_sha=sha,
    )
    assert res_bad_sev["review_gate_status"] == "BLOCKED"
    assert res_bad_sev["errors"]


def test_verify_role_rejects_tampered_model_and_auto_refuses_fallback_on_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC-07: Wrong model or reasoning effort fails verify_role and blocks --backend auto without agy fallback."""
    base_home = tmp_path / "base"
    runtime_root = tmp_path / "rt"
    base_home.mkdir()
    sync_role("pass1", base_home=base_home, runtime_root=runtime_root, overlays_dir=OVERLAYS_DIR)

    cfg_path = runtime_root / "topstep-review-pass1" / "config.toml"
    raw_cfg = cfg_path.read_text(encoding="utf-8")
    if 'model = "gpt-6.1-sol"' in raw_cfg:
        tampered_model = raw_cfg.replace('model = "gpt-6.1-sol"', 'model = "gemini-3.8-flash"')
    else:
        tampered_model = 'model = "gemini-3.8-flash"\n' + raw_cfg
    cfg_path.write_text(tampered_model, encoding="utf-8")
    res_model = verify_role("pass1", runtime_root)
    assert res_model["ok"] is False
    assert any("gpt-6.1-sol" in err for err in res_model["errors"])

    sync_role("pass2", base_home=base_home, runtime_root=runtime_root, overlays_dir=OVERLAYS_DIR)
    cfg2_path = runtime_root / "topstep-review-pass2" / "config.toml"
    cfg2_path.write_text(
        cfg2_path.read_text(encoding="utf-8").replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "low"'),
        encoding="utf-8",
    )
    res_effort = verify_role("pass2", runtime_root)
    assert res_effort["ok"] is False
    assert any("model_reasoning_effort" in err for err in res_effort["errors"])

    agy_called = tmp_path / "agy_should_not_run.flag"
    agy_script = tmp_path / "fake_agy.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        f"import pathlib\npathlib.Path({str(agy_called)!r}).write_text('ran', encoding='utf-8')\n",
    )
    monkeypatch.setattr(
        codex_room_review,
        "verify_role",
        lambda role, root: {"ok": False, "errors": ["tampered model"]},
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    repo_dir, _ = _init_mini_git_repo(tmp_path / "repo_preflight")
    out_dir = tmp_path / "out_preflight_block"
    rc = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--backend", "auto"]
    )
    assert rc == 2
    assert not agy_called.exists()
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "BLOCKED"


def test_codex_room_review_inspects_frozen_commit_and_enforces_write_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC-09: Review subprocesses must see committed bytes in frozen-src/, not dirty worktree edits, and refuse overwrite."""
    import subprocess

    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    target = repo / "sample.txt"
    target.write_text("COMMITTED_V1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    committed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty working tree edit after committed_sha
    target.write_text("DIRTY_UNCOMMITTED\n", encoding="utf-8")

    observed_file = tmp_path / "observed_pass1.txt"
    codex_script = tmp_path / "fake_codex_read.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "cwd_arg = pathlib.Path(args[args.index('-C') + 1])\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            "content = (cwd_arg / 'topstep-chart-app' / 'sample.txt').read_text(encoding='utf-8').strip()\n"
            "if lens == 'correctness_and_tests':\n"
            f"    pathlib.Path({str(observed_file)!r}).write_text(content, encoding='utf-8')\n"
            f"payload = {{'revision_sha': {committed_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': content, 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    monkeypatch.setattr(codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []})
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)

    out_dir = tmp_path / "review_out"
    rc = codex_room_review.main(
        ["--commit", committed_sha, "--repo-root", str(repo), "--out-dir", str(out_dir), "--backend", "codex"]
    )
    assert rc == 0
    assert observed_file.read_text(encoding="utf-8") == "COMMITTED_V1"
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert "frozen_tree_sha256" in report and len(report["frozen_tree_sha256"]) == 64

    # Write-once protection: running again on same out_dir must fail with exit 2
    rc_dup = codex_room_review.main(
        ["--commit", committed_sha, "--repo-root", str(repo), "--out-dir", str(out_dir), "--backend", "codex"]
    )
    assert rc_dup == 2


def test_schema_utils_rejects_type_masquerading_and_extra_properties() -> None:
    """NC-01: Strict schema validator rejects string booleans, bool-as-int, extra keys, and wrong item types."""
    from harness.scripts.schema_utils import (
        is_hex_sha40,
        is_strict_bool,
        is_strict_int,
        validate_instance_against_schema,
    )

    assert is_strict_int(1) is True
    assert is_strict_int(True) is False
    assert is_strict_bool(False) is True
    assert is_strict_bool("false") is False
    assert is_hex_sha40("a" * 40) is True
    assert is_hex_sha40("A" * 40) is False
    assert is_hex_sha40("short") is False

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bad_payload = _sample_pass("a" * 40, "correctness_and_tests")
    bad_payload["extra_field"] = "unexpected"
    bad_payload["test_quality_issues"] = [123]
    bad_finding = _sample_finding("F1", "high", "Bug")
    bad_finding["line_start"] = True
    bad_payload["findings"] = [bad_finding]

    errs = validate_instance_against_schema(bad_payload, schema)
    assert any("additionalProperties" in e for e in errs)
    assert any("test_quality_issues[0]" in e for e in errs)
    assert any("findings[0].line_start" in e for e in errs)


def test_agy_backend_runs_in_frozen_src_and_detects_candidate_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC-09: Direct --backend agy executes with cwd=frozen-src and provides candidate.patch + frozen bytes."""
    import subprocess

    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    target = repo / "app.py"
    target.write_text("V = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    target.write_text("V = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty edit after cand_sha must NOT be visible in agy cwd
    target.write_text("V = 999\n", encoding="utf-8")

    agy_script = tmp_path / "fake_agy_frozen.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            "content = pathlib.Path('topstep-chart-app/app.py').read_text(encoding='utf-8').strip()\n"
            f"payload = {{'revision_sha': {cand_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': content, 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "agy_frozen_out"
    rc = codex_room_review.main(
        ["--commit", cand_sha, "--base", base_sha, "--repo-root", str(repo), "--out-dir", str(out_dir), "--backend", "agy"]
    )
    assert rc == 0
    pass1_res = json.loads((out_dir / "pass1.json").read_text(encoding="utf-8"))
    assert pass1_res["summary"] == "V = 2"
    assert (out_dir / "frozen" / "topstep-chart-app").is_dir()
    assert (out_dir / "frozen" / "candidate.patch").is_file()
    assert "V = 2" in (out_dir / "frozen" / "candidate.patch").read_text(encoding="utf-8")


def test_materialize_frozen_tree_from_gitignored_nested_subdir_and_codex_skip_git_check(
    tmp_path: Path,
) -> None:
    """Reject-fixes 1-3: gitignored nested repo_root must still archive full repo, use topstep-chart-app basename, and pass --skip-git-repo-check."""
    import subprocess

    from harness.scripts.schema_utils import materialize_frozen_tree

    repo = tmp_path / "topstep-chart-app"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text("harness/runs/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("HELLO\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    nested_repo_root = repo / "harness" / "runs" / "attempt-1" / "frozen" / "topstep-chart-app"
    nested_repo_root.mkdir(parents=True)

    info = codex_room_review.resolve_commit_info(nested_repo_root, "HEAD", None)
    assert info["candidate_sha"] == sha

    dest = tmp_path / "out" / "frozen" / "topstep-chart-app"
    mat = materialize_frozen_tree(nested_repo_root, sha, dest)
    assert (dest / "tracked.txt").read_text(encoding="utf-8") == "HELLO\n"
    assert mat["file_count"] == 2

    args_capture = tmp_path / "codex_args.json"
    codex_script = tmp_path / "fake_codex_args.py"
    prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            f"pathlib.Path({str(args_capture)!r}).write_text(json.dumps(args), encoding='utf-8')\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': 'correctness_and_tests', 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )
    run_out = tmp_path / "codex_run"
    run_out.mkdir()
    res = _run_codex_pass(
        codex_prefix=prefix,
        role="pass1",
        runtime_dir=tmp_path / "rt",
        repo_root=dest,
        schema_path=SCHEMA_PATH,
        prompt_text="review",
        out_dir=run_out,
        pass_name="pass1",
        model=None,
        timeout_sec=10.0,
        idle_timeout_sec=5.0,
    )
    assert res is not None
    captured = json.loads(args_capture.read_text(encoding="utf-8"))
    assert "--skip-git-repo-check" in captured
    assert captured[captured.index("-C") + 1] == str(dest)


def test_dry_run_refuses_completed_out_dir_without_mutating_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-03 & S2-002: --dry-run must refuse overwrite and leave existing artifacts untouched."""
    repo_dir, _ = _init_mini_git_repo(tmp_path / "repo_dry")
    out_dir = tmp_path / "completed_run"
    out_dir.mkdir()
    report_path = out_dir / "consolidated-review.json"
    prompt_path = out_dir / "pass1.prompt.md"
    report_path.write_text('{"review_gate_status": "PASSED"}\n', encoding="utf-8")
    prompt_path.write_text("original prompt\n", encoding="utf-8")
    report_bytes = report_path.read_bytes()
    prompt_bytes = prompt_path.read_bytes()

    rc = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--dry-run"]
    )
    assert rc == 2
    assert report_path.read_bytes() == report_bytes
    assert prompt_path.read_bytes() == prompt_bytes


def test_primary_reviewer_model_lock_blocks_unapproved_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-01 & S2-001: apply_safety_locks and codex backend must lock primary model to gpt-6.1-sol."""
    from harness.scripts.codex_room_sync import apply_safety_locks

    with pytest.raises(ValueError, match="gpt-6.1-sol"):
        apply_safety_locks({}, model_override="gemini-3.8-flash")

    repo_dir, _ = _init_mini_git_repo(tmp_path / "repo_lock")
    monkeypatch.setattr(codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []})
    out_dir = tmp_path / "wrong_model_out"
    rc = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--backend", "codex", "--model", "other-model"]
    )
    assert rc == 2
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "BLOCKED"
    assert any("gpt-6.1-sol" in err for err in report["errors"])


def test_consolidate_short_circuits_schema_invalid_duplicate() -> None:
    """P1-06: Schema errors must short-circuit before finding dedup so malformed field types never TypeError."""
    sha = "a" * 40
    bad_f1 = _sample_finding("P1-1", "high", "Dup title")
    bad_f1["evidence"] = 123
    bad_f2 = _sample_finding("P2-1", "high", "Dup title")
    bad_f2["evidence"] = "valid"
    res_bad = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", [bad_f1]),
        _sample_pass(sha, "security_and_invariants", "CHANGES_REQUESTED", [bad_f2]),
        expected_sha=sha,
    )
    assert res_bad["review_gate_status"] == "BLOCKED"
    assert res_bad["consolidated_findings"] == []
    assert res_bad["errors"]


def test_consolidate_blocks_contradictory_changes_requested_without_blocking_findings() -> None:
    """S2-003: CHANGES_REQUESTED verdict with 0 blocking findings must block instead of passing."""
    sha = "a" * 40
    res_contradictory = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", []),
        _sample_pass(sha, "security_and_invariants", "APPROVE", []),
        expected_sha=sha,
    )
    assert res_contradictory["review_gate_status"] == "BLOCKED"
    assert any("CHANGES_REQUESTED" in e for e in res_contradictory["errors"])


def test_agy_relative_out_dir_with_large_prompt_passes_absolute_prompt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-03: Relative --out-dir must resolve to absolute path so agy in frozen cwd can read >16k prompt file."""
    repo_dir, sha = _init_mini_git_repo(tmp_path / "repo_rel_out")
    packet_file = tmp_path / "big_packet.md"
    marker = "UNIQUE_PACKET_MARKER_P103"
    packet_file.write_text(marker + "\n" + ("X" * 18000), encoding="utf-8")

    agy_script = tmp_path / "fake_agy_read_prompt.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, re, sys\n"
            "args = sys.argv[1:]\n"
            "cli_prompt = args[args.index('-p') + 1]\n"
            "m = re.search(r'`([^`]+\\.prompt\\.md)`', cli_prompt)\n"
            "assert m is not None, f'Expected prompt file ref in: {cli_prompt[:200]}'\n"
            "p = pathlib.Path(m.group(1))\n"
            "assert p.is_absolute(), f'Prompt path must be absolute, got {p}'\n"
            "text = p.read_text(encoding='utf-8')\n"
            f"assert {marker!r} in text\n"
            "lens = 'security_and_invariants' if 'pass2' in p.name else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': str(p), 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)
    monkeypatch.chdir(tmp_path)

    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--task-packet",
            str(packet_file),
            "--out-dir",
            "rel_review_out",
            "--backend",
            "agy",
        ]
    )
    assert rc == 0


def test_codex_room_review_atomic_lock_blocks_concurrent_same_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-04 & S2-003: Atomic .review-attempt.lock must prevent concurrent runs and block before materialization."""
    import concurrent.futures
    import threading

    repo_dir, sha = _init_mini_git_repo(tmp_path / "repo_lock_race")
    out_dir = tmp_path / "shared_attempt"

    # Part A: Pre-existing lock blocks BEFORE materialize_frozen_tree is called.
    locked_dir = tmp_path / "prelocked_attempt"
    locked_dir.mkdir()
    (locked_dir / ".review-attempt.lock").write_text("existing lock\n", encoding="utf-8")

    def _forbid_materialize(*_args, **_kwargs):
        raise AssertionError("materialize_frozen_tree must not be called when lock already exists")

    monkeypatch.setattr(codex_room_review, "materialize_frozen_tree", _forbid_materialize)
    rc_pre = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(locked_dir), "--backend", "agy"]
    )
    assert rc_pre == 2
    monkeypatch.undo()

    # Part B: Two concurrent invocations racing on the exact same out_dir: exactly 1 wins (0), 1 loses (2).
    barrier = threading.Barrier(2)
    orig_resolve = codex_room_review.resolve_commit_info

    def _sync_resolve(*args, **kwargs):
        res = orig_resolve(*args, **kwargs)
        barrier.wait(timeout=5.0)
        return res

    agy_script = tmp_path / "fake_agy_race.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, sys, time\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "time.sleep(0.15)\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_commit_info", _sync_resolve)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    argv = ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--backend", "agy"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(codex_room_review.main, argv)
        f2 = pool.submit(codex_room_review.main, argv)
        results = sorted([f1.result(), f2.result()])

    assert results == [0, 2]
    assert (out_dir / ".review-attempt.lock").is_file()
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "PASSED"


def test_merge_prior_pass_findings_preserves_invalid_or_contradictory_prior_pass_across_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-003: Contradictory/invalid prior Codex pass must NOT be washed away by agy fallback."""
    repo_dir, sha = _init_policy_repo(
        tmp_path / "repo_merge_contradict", qualified_routes=["gemini-4-carbon-high"]
    )

    contradictory_p1 = _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", [])
    agy_approve_p1 = _sample_pass(sha, "correctness_and_tests", "APPROVE", [])
    preserved = codex_room_review._merge_prior_pass_findings(agy_approve_p1, contradictory_p1, sha)
    assert preserved["verdict"] == "CHANGES_REQUESTED"
    assert preserved["findings"] == []

    # End-to-end: Codex pass1 returns contradictory CHANGES_REQUESTED + [], pass2 hits quota, agy APPROVEs -> BLOCKED (rc == 2)
    codex_script = tmp_path / "fake_codex_contradict.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {sha!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'CHANGES_REQUESTED', 'summary': 'contradictory', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "else:\n"
            "    print(json.dumps({'type': 'error', "
            "'message': 'usage_limit_reached - Codex CLI quota exhausted'}), flush=True)\n"
            "    sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_approve_end.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy approve', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []})
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_contradict_fallback"
    rc = codex_room_review.main(
        ["--commit", "HEAD", "--repo-root", str(repo_dir), "--out-dir", str(out_dir), "--backend", "auto"]
    )
    assert rc == 2
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "BLOCKED"


def test_consolidate_blocks_contradictory_pass_even_when_other_pass_has_blocking_finding() -> None:
    """P1-003: Pass 1 CHANGES_REQUESTED with [] must block even when Pass 2 has a valid blocking finding."""
    sha = "a" * 40
    high_finding = _sample_finding("P2-10", "high", "Real security bug")
    res = consolidate_reviews(
        _sample_pass(sha, "correctness_and_tests", "CHANGES_REQUESTED", []),
        _sample_pass(sha, "security_and_invariants", "CHANGES_REQUESTED", [high_finding]),
        expected_sha=sha,
    )
    assert res["review_gate_status"] == "BLOCKED"
    assert any("Pass 1" in e and "CHANGES_REQUESTED" in e for e in res["errors"])


def test_agy_sandbox_paths_are_strictly_inside_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC-09b: Both candidate.patch and large prompt_file must live inside the agy/codex sandbox cwd."""
    import subprocess

    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    target = repo / "app.py"
    target.write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    target.write_text("X = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    packet_file = tmp_path / "large_packet.md"
    packet_file.write_text("LARGE_PACKET_NC09B\n" + ("P" * 18000), encoding="utf-8")

    observed_calls: list[dict[str, Path]] = []
    orig_watchdog = codex_room_review._run_subprocess_with_watchdog

    def _spy_watchdog(*args, **kwargs):
        observed_calls.append({"cwd": Path(kwargs["cwd"]).resolve()})
        return orig_watchdog(*args, **kwargs)

    orig_run_agy = codex_room_review._run_agy_pass
    observed_prompts: list[Path] = []

    def _spy_run_agy(*args, **kwargs):
        observed_prompts.append(Path(kwargs["prompt_file"]).resolve())
        return orig_run_agy(*args, **kwargs)

    agy_script = tmp_path / "fake_agy_sandbox.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, re, sys\n"
            "args = sys.argv[1:]\n"
            "cli_prompt = args[args.index('-p') + 1]\n"
            "m = re.search(r'`([^`]+\\.prompt\\.md)`', cli_prompt)\n"
            "assert m is not None\n"
            "p_path = pathlib.Path(m.group(1)).resolve()\n"
            "cwd_path = pathlib.Path.cwd().resolve()\n"
            "assert p_path.is_relative_to(cwd_path), f'{p_path} not inside {cwd_path}'\n"
            "patch_path = cwd_path / 'candidate.patch'\n"
            "assert patch_path.is_file() and patch_path.resolve().is_relative_to(cwd_path)\n"
            "lens = 'security_and_invariants' if 'pass2' in p_path.name else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {cand_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'sandbox ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(codex_room_review, "_run_subprocess_with_watchdog", _spy_watchdog)
    monkeypatch.setattr(codex_room_review, "_run_agy_pass", _spy_run_agy)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_nc09b"
    rc = codex_room_review.main(
        [
            "--commit",
            cand_sha,
            "--base",
            base_sha,
            "--repo-root",
            str(repo),
            "--task-packet",
            str(packet_file),
            "--out-dir",
            str(out_dir),
            "--backend",
            "agy",
        ]
    )
    assert rc == 0
    assert len(observed_calls) == 2
    assert len(observed_prompts) == 2
    patch_file = (out_dir / "frozen" / "candidate.patch").resolve()
    for call_meta, p_file in zip(observed_calls, observed_prompts, strict=True):
        cwd = call_meta["cwd"]
        assert patch_file.is_relative_to(cwd)
        assert p_file.is_relative_to(cwd)
        assert (cwd / "topstep-chart-app").is_dir()


def _commit_repo_change(repo_dir: Path, message: str) -> str:
    import subprocess

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_repo_model_policy(
    repo_dir: Path,
    *,
    qualified_routes: list[str] | None = None,
    quota_pools: dict[str, str] | None = None,
    max_transient_retries: int = 1,
) -> Path:
    policy_dir = repo_dir / "harness"
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy = {
        "schema_version": "2.0.0",
        "effective_date": "2026-11-24",
        "excluded_model_prefixes": [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "gemini-pro-orchestrator-argon",
            "gemini-3.7-flash",
            "gemini-2.5-pro",
        ],
        "roles": {
            "independent_reviewer_primary": {
                "backend": "codex",
                "selected_model": "gpt-6.1-sol",
                "selected_effort": "high",
            },
            "independent_reviewer_fallbacks": {
                "qualified_routes": qualified_routes if qualified_routes is not None else [],
                "quota_pools": quota_pools
                if quota_pools is not None
                else {
                    "gpt-6.1-sol": "openai_codex",
                    "claude-opus-5-5-medium": "anthropic_agy",
                    "gemini-4-carbon-high": "gemini_agy",
                    "gemini-4-neon-high": "gemini_agy",
                },
            },
        },
        "circuit_breakers": {
            "max_transient_retries_per_provider_pass": max_transient_retries,
        },
    }
    policy_path = policy_dir / "model-policy.json"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return policy_path


def _init_policy_repo(
    repo_dir: Path,
    *,
    qualified_routes: list[str] | None = None,
    quota_pools: dict[str, str] | None = None,
    max_transient_retries: int = 1,
) -> tuple[Path, str]:
    repo_path, _ = _init_mini_git_repo(repo_dir)
    _write_repo_model_policy(
        repo_path,
        qualified_routes=qualified_routes,
        quota_pools=quota_pools,
        max_transient_retries=max_transient_retries,
    )
    sha = _commit_repo_change(repo_path, "add policy")
    return repo_path, sha


def test_nc11_tool_output_with_usage_limit_does_not_trigger_quota_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NC-11: Tool/message JSONL events quoting usage_limit_reached or 429 must not fall back."""
    repo_dir, _ = _init_policy_repo(
        tmp_path / "repo_nc11", qualified_routes=["gemini-4-carbon-high"]
    )

    agy_called = tmp_path / "agy_called_nc11.flag"
    codex_script = tmp_path / "fake_codex_nc11.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'id': 'i1', "
            "'type': 'mcp_tool_call', 'output': 'test: usage_limit_reached 429'}}), flush=True)\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'id': 'i2', "
            "'type': 'agent_message', 'text': 'Quoted usage_limit_reached'}}), flush=True)\n"
            "sys.stderr.write('Error: internal parser crash\\n')\n"
            "sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_nc11.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(agy_called)!r}).write_text('called', encoding='utf-8')\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'ERROR'}}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_nc11"
    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 2
    assert not agy_called.exists()
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["fallback_used"] is False
    assert report["review_gate_status"] == "BLOCKED"


def test_auth_error_401_token_revoked_halts_with_blocked_auth_no_retry_no_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth 401/token_revoked must fail closed as BLOCKED_AUTH with no retry or fallback."""
    repo_dir, _ = _init_policy_repo(
        tmp_path / "repo_auth", qualified_routes=["gemini-4-carbon-high"]
    )

    calls_file = tmp_path / "codex_auth_calls.txt"
    agy_called = tmp_path / "agy_called_auth.flag"
    codex_script = tmp_path / "fake_codex_auth.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "_ = sys.stdin.read()\n"
            f"p = pathlib.Path({str(calls_file)!r})\n"
            "prev = p.read_text(encoding='utf-8') if p.is_file() else ''\n"
            "p.write_text(prev + 'call\\n', encoding='utf-8')\n"
            "print(json.dumps({'type': 'error', "
            "'message': '401 Unauthorized: token_revoked'}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_auth.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import pathlib\n"
            f"pathlib.Path({str(agy_called)!r}).write_text('called', encoding='utf-8')\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_auth"
    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
            "--sequential",
        ]
    )
    assert rc == 2
    assert not agy_called.exists()
    assert len(calls_file.read_text(encoding="utf-8").splitlines()) == 2
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "BLOCKED_AUTH"
    assert report["fallback_used"] is False


def test_auto_backend_blocks_infra_when_qualified_routes_empty_without_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When qualified_routes is [] and no --fallback-model is given, quota returns BLOCKED_INFRA."""
    repo_dir, _ = _init_policy_repo(tmp_path / "repo_empty_routes", qualified_routes=[])

    agy_called = tmp_path / "agy_called_empty.flag"
    codex_script = tmp_path / "fake_codex_quota_empty.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'type': 'turn.failed', "
            "'error': {'message': 'You\\u2019ve hit your usage limit'}}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_empty.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import pathlib\n"
            f"pathlib.Path({str(agy_called)!r}).write_text('called', encoding='utf-8')\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_empty_routes"
    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 2
    assert not agy_called.exists()
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "BLOCKED_INFRA"
    assert report["fallback_used"] is False
    assert "openai_codex" in report["exhausted_quota_pools"]


def test_mid_run_quota_fallback_reuses_pass1_and_runs_only_pass2_on_qualified_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 1 on Codex + Pass 2 curly-apostrophe quota must reuse Pass 1 and run ONLY Pass 2."""
    repo_dir, head_sha = _init_policy_repo(
        tmp_path / "repo_mid_fallback", qualified_routes=["gemini-4-carbon-high"]
    )

    agy_invocations = tmp_path / "agy_invocations.jsonl"
    codex_script = tmp_path / "fake_codex_p1_ok_p2_curly.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {head_sha!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'APPROVE', 'summary': 'p1 codex ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "else:\n"
            "    print(json.dumps({'type': 'turn.failed', "
            "'error': {'message': 'You\\u2019ve hit your usage limit'}}), flush=True)\n"
            "    sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_p2_only.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "model = args[args.index('--model') + 1]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"log_p = pathlib.Path({str(agy_invocations)!r})\n"
            "prev = log_p.read_text(encoding='utf-8') if log_p.is_file() else ''\n"
            "log_p.write_text(prev + json.dumps({'model': model, 'lens': lens}) + '\\n', "
            "encoding='utf-8')\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'p2 agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'init', 'init': {'model': model}}), flush=True)\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_mid_fallback"
    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 0
    calls = [json.loads(ln) for ln in agy_invocations.read_text(encoding="utf-8").splitlines()]
    assert calls == [{"model": "gemini-4-carbon-high", "lens": "security_and_invariants"}]

    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "PASSED"
    assert report["pass1_backend"] == "codex"
    assert report["pass1_model"] == "gpt-6.1-sol"
    assert report["pass2_backend"] == "agy"
    assert report["pass2_model"] == "gemini-4-carbon-high"
    assert report["reused_passes"] == []
    assert report["exhausted_quota_pools"] == ["openai_codex"]
    assert (out_dir / "pass1.provenance.json").is_file()
    assert (out_dir / "pass2.provenance.json").is_file()


def test_qualified_routes_skips_shared_exhausted_quota_pool_and_advances_across_pools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared exhausted pool must be skipped (neon_invocations == 0); distinct pool proceeds."""
    # Part A: Two gemini_agy models -> carbon hits RESOURCE_EXHAUSTED, neon must NOT be called.
    repo_a, sha_a = _init_policy_repo(
        tmp_path / "repo_pool_skip",
        qualified_routes=["gemini-4-carbon-high", "gemini-4-neon-high"],
    )

    codex_quota_script = tmp_path / "fake_codex_q_pool.py"
    codex_prefix = _write_fake_codex_script(
        codex_quota_script,
        (
            "import json, sys\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'type': 'error', 'message': 'usage_limit_reached'}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )

    agy_models_called = tmp_path / "agy_models_called.txt"
    agy_pool_script = tmp_path / "fake_agy_pool_skip.py"
    agy_prefix = _write_fake_codex_script(
        agy_pool_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "model = args[args.index('--model') + 1]\n"
            f"p = pathlib.Path({str(agy_models_called)!r})\n"
            "prev = p.read_text(encoding='utf-8') if p.is_file() else ''\n"
            "p.write_text(prev + model + '\\n', encoding='utf-8')\n"
            "if model == 'gemini-4-carbon-high':\n"
            "    err = {'error_code': 429, 'status': 'RESOURCE_EXHAUSTED', "
            "'short_error': 'quota limit reached'}\n"
            "    sys.stderr.write('AGY_ERROR: ' + json.dumps(err) + '\\n')\n"
            "    sys.exit(1)\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha_a!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_a = tmp_path / "out_pool_skip"
    rc_a = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_a),
            "--out-dir",
            str(out_a),
            "--backend",
            "auto",
        ]
    )
    assert rc_a == 2
    called_models = agy_models_called.read_text(encoding="utf-8").splitlines()
    neon_invocations = sum(1 for m in called_models if m == "gemini-4-neon-high")
    assert neon_invocations == 0
    report_a = json.loads((out_a / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_a["review_gate_status"] == "BLOCKED_INFRA"
    assert report_a["exhausted_quota_pools"] == ["openai_codex", "gemini_agy"]

    # Part B: Distinct second pool (anthropic_agy) succeeds after gemini_agy exhausted.
    agy_models_called.unlink()
    _write_repo_model_policy(
        repo_a, qualified_routes=["gemini-4-carbon-high", "claude-opus-5-5-medium"]
    )
    sha_b = _commit_repo_change(repo_a, "add opus route")
    agy_pool_script.write_text(
        agy_pool_script.read_text(encoding="utf-8").replace(sha_a, sha_b),
        encoding="utf-8",
    )
    out_b = tmp_path / "out_pool_advance"
    rc_b = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_a),
            "--out-dir",
            str(out_b),
            "--backend",
            "auto",
        ]
    )
    assert rc_b == 0
    report_b = json.loads((out_b / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_b["review_gate_status"] == "PASSED"
    assert report_b["review_model"] == "claude-opus-5-5-medium"
    assert report_b["exhausted_quota_pools"] == ["openai_codex", "gemini_agy"]


def test_transient_rate_or_transport_retries_only_failed_pass_once_then_blocks_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient error retries ONLY failed pass at most once, archiving .retry0.*."""
    repo_dir, head_sha = _init_policy_repo(
        tmp_path / "repo_transient", qualified_routes=["gemini-4-carbon-high"]
    )

    p1_calls = tmp_path / "transient_p1_calls.txt"
    p2_calls = tmp_path / "transient_p2_calls.txt"
    codex_script = tmp_path / "fake_codex_transient_recover.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            f"p1_p = pathlib.Path({str(p1_calls)!r})\n"
            f"p2_p = pathlib.Path({str(p2_calls)!r})\n"
            "target_p = p2_p if out_path.name == 'pass2.json' else p1_p\n"
            "prev = target_p.read_text(encoding='utf-8') if target_p.is_file() else ''\n"
            "seen = len(prev.splitlines())\n"
            "target_p.write_text(prev + out_path.name + '\\n', encoding='utf-8')\n"
            "if out_path.name == 'pass2.json' and seen == 0:\n"
            "    print(json.dumps({'type': 'error', 'message': 'rate_limit_exceeded', "
            "'retry_after_sec': 0.01}), flush=True)\n"
            "    sys.exit(1)\n"
            "lens = 'security_and_invariants' if out_path.name == 'pass2.json' "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'recovered', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)

    out_ok = tmp_path / "out_transient_recover"
    rc_ok = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_ok),
            "--backend",
            "auto",
        ]
    )
    assert rc_ok == 0
    assert len(p1_calls.read_text(encoding="utf-8").splitlines()) == 1
    assert len(p2_calls.read_text(encoding="utf-8").splitlines()) == 2
    assert (out_ok / "pass2.codex-gpt-6.1-sol-retry0.raw.jsonl").is_file()

    # Persistent 503 after 1 retry -> BLOCKED_INFRA (rc == 2), no agy fallback.
    agy_called = tmp_path / "agy_no_transient_fb.flag"
    codex_always_503 = tmp_path / "fake_codex_503.py"
    codex_503_prefix = _write_fake_codex_script(
        codex_always_503,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {head_sha!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'APPROVE', 'summary': 'ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "else:\n"
            "    sys.stderr.write('HTTP 503 upstream connect error\\n')\n"
            "    sys.exit(1)\n"
        ),
    )
    agy_script = tmp_path / "fake_agy_unused_503.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import pathlib\n"
            f"pathlib.Path({str(agy_called)!r}).write_text('called', encoding='utf-8')\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_503_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_fail = tmp_path / "out_transient_fail"
    rc_fail = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_fail),
            "--backend",
            "auto",
        ]
    )
    assert rc_fail == 2
    assert not agy_called.exists()
    report_fail = json.loads((out_fail / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_fail["review_gate_status"] == "BLOCKED_INFRA"
    assert (out_fail / "pass1.provenance.json").is_file()


def test_agy_typed_classifier_handles_auth_quota_and_zero_token_startup_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGY classifier distinguishes 401/UNAUTHENTICATED, quota, and 0-token startup retry."""
    d_auth = tmp_path / "agy_auth_dir"
    d_auth.mkdir()
    (d_auth / "pass1.stderr.log").write_text(
        'AGY_ERROR: {"error_code": 401, "status": "UNAUTHENTICATED"}\n',
        encoding="utf-8",
    )
    res_auth = codex_room_review.classify_agy_failure(d_auth, "pass1")
    assert res_auth.category == codex_room_review.OUTCOME_AUTH_ERROR

    d_quota = tmp_path / "agy_quota_dir"
    d_quota.mkdir()
    quota_ev = {
        "event": "result",
        "result": {"status": "ERROR", "error": "individual quota reached"},
    }
    (d_quota / "pass1.raw.jsonl").write_text(
        json.dumps(quota_ev) + "\n",
        encoding="utf-8",
    )
    res_quota = codex_room_review.classify_agy_failure(d_quota, "pass1")
    assert res_quota.category == codex_room_review.OUTCOME_CONFIRMED_QUOTA

    # End-to-end 0-token startup race recovers on single retry.
    repo_dir, head_sha = _init_mini_git_repo(tmp_path / "repo_agy_race")
    calls_dir = tmp_path / "agy_race_calls"
    calls_dir.mkdir()
    agy_script = tmp_path / "fake_agy_startup_race.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"p = pathlib.Path({str(calls_dir)!r}) / f'{{lens}}.log'\n"
            "prev = p.read_text(encoding='utf-8') if p.is_file() else ''\n"
            "seen = len(prev.splitlines())\n"
            "p.write_text(prev + lens + '\\n', encoding='utf-8')\n"
            "if lens == 'correctness_and_tests' and seen == 0:\n"
            "    err_ev = {'event': 'result', 'result': {'status': 'ERROR', "
            "'error': 'failed to start conversation', 'usage': {'total_tokens': 0}}}\n"
            "    print(json.dumps(err_ev), flush=True)\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {head_sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)
    out_dir = tmp_path / "out_agy_race"
    rc = codex_room_review.main(
        [
            "--commit",
            "HEAD",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "agy",
        ]
    )
    assert rc == 0
    assert (out_dir / f"pass1.agy-{DEFAULT_AGY_FALLBACK_MODEL}-retry0.raw.jsonl").is_file()


def test_resume_from_reuses_matching_provenance_and_rejects_stale_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--resume-from reuses pass1 iff all 7 provenance fields match; rejects stale packet/SHA."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_resume", qualified_routes=[])

    packet_a = tmp_path / "packet_a.md"
    packet_a.write_text("# Task Packet A\n", encoding="utf-8")
    packet_b = tmp_path / "packet_b.md"
    packet_b.write_text("# Task Packet B (changed)\n", encoding="utf-8")

    codex_calls_dir = tmp_path / "resume_codex_calls"
    codex_calls_dir.mkdir()
    codex_script = tmp_path / "fake_codex_resume.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            f"d = pathlib.Path({str(codex_calls_dir)!r})\n"
            "f = d / out_path.name\n"
            "prev = f.read_text(encoding='utf-8') if f.is_file() else ''\n"
            "f.write_text(prev + 'called\\n', encoding='utf-8')\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {sha1!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'APPROVE', 'summary': 'codex p1', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "else:\n"
            "    q_ev = {'type': 'error', 'message': 'usage_limit_reached'}\n"
            "    print(json.dumps(q_ev), flush=True)\n"
            "    sys.exit(1)\n"
        ),
    )
    agy_calls_dir = tmp_path / "resume_agy_calls"
    agy_calls_dir.mkdir()
    agy_script = tmp_path / "fake_agy_resume.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"d = pathlib.Path({str(agy_calls_dir)!r})\n"
            "f = d / f'{lens}.txt'\n"
            "prev = f.read_text(encoding='utf-8') if f.is_file() else ''\n"
            "f.write_text(prev + 'called\\n', encoding='utf-8')\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    # Run 1: pass1 succeeds on Codex, pass2 hits quota, qualified_routes=[] -> BLOCKED_INFRA.
    run1_dir = tmp_path / "resume_run1"
    rc1 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--repo-root",
            str(repo_dir),
            "--task-packet",
            str(packet_a),
            "--out-dir",
            str(run1_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc1 == 2
    assert (run1_dir / "pass1.provenance.json").is_file()

    # Run 2: --resume-from run1 with matching packet + SHA and --fallback-model -> reuses pass1.
    for item in codex_calls_dir.iterdir():
        item.unlink()
    for item in agy_calls_dir.iterdir():
        item.unlink()
    run2_dir = tmp_path / "resume_run2"
    rc2 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--repo-root",
            str(repo_dir),
            "--task-packet",
            str(packet_a),
            "--resume-from",
            str(run1_dir),
            "--fallback-model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(run2_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc2 == 0
    assert not (codex_calls_dir / "pass1.json").exists()
    assert (codex_calls_dir / "pass2.json").is_file()
    assert not (agy_calls_dir / "correctness_and_tests.txt").exists()
    assert (agy_calls_dir / "security_and_invariants.txt").is_file()
    report2 = json.loads((run2_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert "pass1" in report2["reused_passes"]
    assert report2["pass1_backend"] == "codex"
    assert report2["pass2_backend"] == "agy"

    # Run 3: --resume-from run1 with CHANGED packet_sha256 -> pass1 must NOT be reused!
    for item in codex_calls_dir.iterdir():
        item.unlink()
    for item in agy_calls_dir.iterdir():
        item.unlink()
    run3_dir = tmp_path / "resume_run3_stale_packet"
    rc3 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--repo-root",
            str(repo_dir),
            "--task-packet",
            str(packet_b),
            "--resume-from",
            str(run1_dir),
            "--fallback-model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(run3_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc3 == 0
    assert (codex_calls_dir / "pass1.json").is_file()
    report3 = json.loads((run3_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert "pass1" not in report3["reused_passes"]

    # Run 4: --resume-from run1 on a NEW candidate_sha -> pass1 must NOT be reused!
    (repo_dir / "extra.txt").write_text("new commit\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "commit 2")
    codex_script.write_text(
        codex_script.read_text(encoding="utf-8").replace(sha1, sha2),
        encoding="utf-8",
    )
    agy_script.write_text(
        agy_script.read_text(encoding="utf-8").replace(sha1, sha2),
        encoding="utf-8",
    )
    run4_dir = tmp_path / "resume_run4_stale_sha"
    rc4 = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--repo-root",
            str(repo_dir),
            "--task-packet",
            str(packet_a),
            "--resume-from",
            str(run1_dir),
            "--fallback-model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(run4_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc4 == 0
    report4 = json.loads((run4_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert "pass1" not in report4["reused_passes"]


def test_live_model_policy_promotes_canary_qualified_carbon_high() -> None:
    """Repo harness/model-policy.json promotes canary-qualified gemini-4-carbon-high cleanly."""
    from harness.scripts.codex_room_sync import load_model_policy

    policy, excluded, err = load_model_policy(REPO_ROOT, require_exists=True)
    assert err is None
    assert policy is not None
    routes, routes_err = codex_room_review.extract_qualified_routes(policy, excluded)
    assert routes_err is None
    assert routes == ["gemini-4-carbon-high"]
    assert codex_room_review.resolve_quota_pool("gemini-4-carbon-high", policy) == "gemini_agy"
    fb = policy["roles"]["independent_reviewer_fallbacks"]
    assert "gemini-4-carbon-high" not in fb["unqualified_candidates_requiring_canary"]
    assert (
        fb["qualification_evidence"]["gemini-4-carbon-high"]
        == "harness/evidence/canary-carbon-high/canary-report.json"
    )


def _setup_ledger_for_g4_test(
    repo_dir: Path,
    task_id: str,
    *,
    state: str,
    candidate_sha: str | None = None,
    last_g4_candidate_sha: str | None = None,
    last_g4_status: str | None = None,
    g4_rounds_total: int = 0,
    g4_rounds_in_packet: int = 0,
    g4_invocations_by_sha: dict[str, int] | None = None,
) -> Path:
    from harness.scripts import task_runner

    ledger_path = repo_dir / "harness" / "runs" / task_id / "ledger.json"
    ledger = task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id=task_id,
        base_sha="0" * 40,
        budget_usd=6.0,
    )
    ledger["state"] = state
    ledger["candidate_sha"] = candidate_sha
    ledger["last_g4_candidate_sha"] = last_g4_candidate_sha
    ledger["last_g4_status"] = last_g4_status
    ledger["g4_rounds_total"] = g4_rounds_total
    ledger["g4_rounds_in_packet"] = g4_rounds_in_packet
    ledger["g4_invocations_by_sha"] = dict(g4_invocations_by_sha or {})
    task_runner.save_ledger(ledger_path, ledger)
    return ledger_path


def _install_counting_codex_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sha: str,
    *,
    pass1_verdict: str = "APPROVE",
    pass1_findings: list[dict] | None = None,
    emit_usage: bool = False,
) -> Path:
    calls_dir = tmp_path / f"cli_calls_{sha[:8]}"
    calls_dir.mkdir(parents=True, exist_ok=True)
    script_path = tmp_path / f"fake_codex_{sha[:8]}.py"
    findings_json = json.dumps(pass1_findings or [])
    usage_line = (
        'print(json.dumps({"type": "turn.completed", "usage": '
        '{"input_tokens": 10000, "cached_input_tokens": 2000, '
        '"output_tokens": 1500}}), flush=True)\n'
        if emit_usage
        else ""
    )
    prefix = _write_fake_codex_script(
        script_path,
        (
            "import json, pathlib, sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            f"cd = pathlib.Path({str(calls_dir)!r})\n"
            "cf = cd / f'{out_path.stem}.log'\n"
            "prev = cf.read_text(encoding='utf-8') if cf.is_file() else ''\n"
            "cf.write_text(prev + out_path.name + '\\n', encoding='utf-8')\n"
            "is_p2 = 'security_and_invariants' in prompt\n"
            "lens = 'security_and_invariants' if is_p2 else 'correctness_and_tests'\n"
            f"verdict = 'APPROVE' if is_p2 else {pass1_verdict!r}\n"
            f"findings = [] if is_p2 else {findings_json}\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': lens, 'verdict': verdict, "
            "'summary': 'stub review', 'findings': findings, "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            f"{usage_line}"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: prefix)
    return calls_dir


def _count_calls(calls_dir: Path) -> int:
    if not calls_dir.is_dir():
        return 0
    total = 0
    for lf in calls_dir.glob("*.log"):
        total += len([ln for ln in lf.read_text(encoding="utf-8").splitlines() if ln.strip()])
    return total


def test_g4_hard_cap_blocks_paused_budget_and_root_ceiling_without_spawning_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAUSED_BUDGET or g4_rounds_total == 6 halts preflight with rc=2 and 0 CLI spawns."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_root_cap", qualified_routes=[])
    calls_file = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    ledger_paused = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-PAUSED",
        state="PAUSED_BUDGET",
        candidate_sha=sha1,
    )
    rc_paused = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-PAUSED",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    paused_data = json.loads(ledger_paused.read_text(encoding="utf-8"))
    assert rc_paused == 2
    assert _count_calls(calls_file) == 0
    assert paused_data["state"] == "PAUSED_BUDGET"
    assert not (repo_dir / "harness" / "runs" / "T-G4-PAUSED" / "attempts").exists()

    ledger_cap = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-ROOT-CAP",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha1,
        g4_rounds_total=6,
        g4_rounds_in_packet=1,
    )
    rc_cap = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-ROOT-CAP",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    cap_data = json.loads(ledger_cap.read_text(encoding="utf-8"))
    assert rc_cap == 2
    assert _count_calls(calls_file) == 0
    assert cap_data["state"] == "PAUSED_BUDGET"
    assert not (repo_dir / "harness" / "runs" / "T-G4-ROOT-CAP" / "attempts").exists()


def test_g4_hard_cap_blocks_packet_ceiling_and_needs_replan_without_spawning_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """g4_rounds_in_packet == 3 or state=NEEDS_REPLAN halts preflight with rc=2 and 0 CLI."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_pkt_cap", qualified_routes=[])
    calls_file = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    ledger_pkt = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-PKT-CAP",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha1,
        g4_rounds_total=3,
        g4_rounds_in_packet=3,
    )
    rc_pkt = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-PKT-CAP",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    pkt_data = json.loads(ledger_pkt.read_text(encoding="utf-8"))
    assert rc_pkt == 2
    assert _count_calls(calls_file) == 0
    assert pkt_data["state"] == "NEEDS_REPLAN"

    ledger_replan = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-REPLAN",
        state="NEEDS_REPLAN",
        candidate_sha=sha1,
        g4_rounds_total=2,
        g4_rounds_in_packet=2,
    )
    rc_replan = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-REPLAN",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    replan_data = json.loads(ledger_replan.read_text(encoding="utf-8"))
    assert rc_replan == 2
    assert _count_calls(calls_file) == 0
    assert replan_data["state"] == "NEEDS_REPLAN"


def test_g4_hard_cap_blocks_duplicate_same_sha_and_third_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject 2nd invocation on same SHA without --resume-from and 3rd even with --resume-from."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_dup_sha", qualified_routes=[])
    calls_file = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    ledger_dup = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-DUP",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha1,
    )
    rc_first = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-DUP",
            "--attempt-id",
            "att-1",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_first == 0
    assert _count_calls(calls_file) == 2

    # Force state back to REVIEW_FROZEN_G4 to verify per-SHA guard blocks re-roll independently.
    data_dup = json.loads(ledger_dup.read_text(encoding="utf-8"))
    data_dup["state"] = "REVIEW_FROZEN_G4"
    ledger_dup.write_text(json.dumps(data_dup, indent=2) + "\n", encoding="utf-8")

    rc_second = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-DUP",
            "--attempt-id",
            "att-2",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_second == 2
    assert _count_calls(calls_file) == 2

    # Resume case: 2 completed BLOCKED_INFRA attempts on sha2 -> 3rd fails on per-SHA cap.
    (repo_dir / "file2.txt").write_text("v2\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "commit sha2")
    calls_file2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    prior_dir1 = repo_dir / "harness" / "runs" / "T-G4-RES" / "attempts" / f"att-1-{sha2[:12]}"
    prior_dir2 = repo_dir / "harness" / "runs" / "T-G4-RES" / "attempts" / f"att-2-{sha2[:12]}"
    for p_dir in (prior_dir1, prior_dir2):
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": sha2,
                    "passes_completed": 1,
                    "independent_contexts": False,
                    "review_gate_status": "BLOCKED_INFRA",
                    "blocking_open_count": 0,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )
    _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-RES",
        state="BLOCKED_INFRA",
        candidate_sha=sha2,
        last_g4_candidate_sha=sha2,
        last_g4_status="BLOCKED_INFRA",
        g4_invocations_by_sha={sha2: 2},
    )
    _ = capsys.readouterr()
    rc_third = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-G4-RES",
            "--attempt-id",
            "att-3",
            "--resume-from",
            str(prior_dir2),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    err_third = capsys.readouterr().err
    assert rc_third == 2
    assert _count_calls(calls_file2) == 0
    assert "Duplicate G4 invocation" in err_third
    assert "count=2" in err_third


def test_g4_hard_cap_blocks_ledgerless_attempts_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ledger.json, 6 existing attempt dirs under attempts/ block the 7th G4 call."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_nolog", qualified_routes=[])
    calls_file = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    attempts_root = repo_dir / "harness" / "runs" / "T-NOLOG" / "attempts"
    for idx in range(6):
        prior_sha = f"{idx + 1}" * 40
        att_dir = attempts_root / f"20261124T00000{idx}Z-{prior_sha[:12]}"
        att_dir.mkdir(parents=True, exist_ok=True)
        (att_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": prior_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-NOLOG",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    assert _count_calls(calls_file) == 0


def test_g4_updates_ledger_usage_rounds_and_breakers_on_changes_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHANGES_REQUESTED increments g4_rounds_total/in_packet, spend, and failure counters."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_usage", qualified_routes=[])
    finding = _sample_finding("P1-10", "high", "Missing lock around shared buffer")
    _install_counting_codex_stub(
        tmp_path,
        monkeypatch,
        sha1,
        pass1_verdict="CHANGES_REQUESTED",
        pass1_findings=[finding],
        emit_usage=True,
    )
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir,
        "T-G4-USAGE",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha1,
    )

    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-USAGE",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
            "--fail-on-blocking",
        ]
    )
    updated = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert rc == 1
    assert updated["spent_api_equiv_usd"] == pytest.approx(0.077, abs=1e-6)
    assert updated["g4_rounds_total"] == 1
    assert updated["g4_rounds_in_packet"] == 1
    assert updated["last_g4_candidate_sha"] == sha1
    assert updated["last_g4_status"] == "CHANGES_REQUESTED"
    assert updated["consecutive_failed_candidates"] == 1
    assert updated["candidate_sha"] is None
    assert updated["state"] == "ACTIVE_PEER_G1"


def test_g4_require_ledger_agy_dedup_and_resume_reuse_no_double_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify --require-ledger, AGY cumulative usage dedup, and no double-charge on reuse."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_agy_led", qualified_routes=[])
    calls_file = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    # --require-ledger without ledger.json must fail closed before spawning any CLI.
    rc_req = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-NO-LEDGER-REQ",
            "--require-ledger",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_req == 2
    assert _count_calls(calls_file) == 0

    # canary-* tasks bypass ledger/attempts enforcement cleanly.
    rc_canary = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "canary-case1",
            "--out-dir",
            str(tmp_path / "canary_out"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_canary == 0

    # AGY resume-from reuse: pass1 reused, pass2 runs with cumulative usage -> no double charge.
    prior_dir = repo_dir / "harness" / "runs" / "T-AGY-DEDUP" / "attempts" / f"att1-{sha1[:12]}"
    prior_dir.mkdir(parents=True, exist_ok=True)
    frozen_meta_dir = tmp_path / "probe_frozen"
    from harness.scripts.schema_utils import materialize_frozen_tree

    c_info = codex_room_review.resolve_commit_info(repo_dir, sha1, None)
    fmeta = materialize_frozen_tree(
        repo_dir, sha1, frozen_meta_dir, base_sha=c_info["base_sha"]
    )
    policy_sha = hashlib.sha256(
        (repo_dir / "harness" / "model-policy.json").read_bytes()
    ).hexdigest()
    schema_sha = hashlib.sha256(codex_room_review.SCHEMA_PATH.read_bytes()).hexdigest()
    default_pkt_sha = hashlib.sha256(
        b"No Task Packet file supplied; review candidate commit against AGENTS.md, "
        b"implementation_plan.md, and docs/WORKSPACE_PROTOCOL.md."
    ).hexdigest()
    p1_payload = _sample_pass(sha1, "correctness_and_tests", "APPROVE", [])
    (prior_dir / "pass1.json").write_text(json.dumps(p1_payload), encoding="utf-8")
    # Put high token usage in prior_dir/pass1.raw.jsonl to prove reused pass1 is not re-billed.
    (prior_dir / "pass1.raw.jsonl").write_text(
        json.dumps(
            {
                "event": "result",
                "conversation_id": "conv-old-p1",
                "result": {
                    "status": "SUCCESS",
                    "usage": {"input_tokens": 500000, "output_tokens": 50000},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (prior_dir / "pass1.provenance.json").write_text(
        json.dumps(
            {
                "candidate_sha": sha1,
                "base_sha": c_info["base_sha"],
                "packet_sha256": default_pkt_sha,
                "policy_sha256": policy_sha,
                "schema_sha256": schema_sha,
                "lens": "correctness_and_tests",
                "frozen_tree_sha256": fmeta["frozen_tree_sha256"],
                "backend": "agy",
                "model": "gemini-4-carbon-high",
            }
        ),
        encoding="utf-8",
    )

    agy_state_file = tmp_path / "agy_dedup_calls.txt"
    agy_script = tmp_path / "fake_agy_dedup.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            f"sf = pathlib.Path({str(agy_state_file)!r})\n"
            "n = len(sf.read_text(encoding='utf-8').splitlines()) if sf.is_file() else 0\n"
            "sf.write_text(('x\\n' * (n + 1)), encoding='utf-8')\n"
            "if n == 0:\n"
            "    print(json.dumps({'event': 'result', 'conversation_id': 'conv-agy-1', "
            "'result': {'status': 'ERROR', 'conversation_id': 'conv-agy-1', "
            "'error': '503 unavailable', "
            "'usage': {'input_tokens': 100000, 'cache_read_tokens': 0, "
            "'output_tokens': 10000, 'thinking_tokens': 4000}}}), flush=True)\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': 'security_and_invariants', "
            "'verdict': 'APPROVE', 'summary': 'ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'conversation_id': 'conv-agy-1', "
            "'result': {'status': 'SUCCESS', 'conversation_id': 'conv-agy-1', "
            "'structured_output': payload, "
            "'usage': {'input_tokens': 150000, 'cache_read_tokens': 0, "
            "'output_tokens': 10000, 'thinking_tokens': 4000}}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    ledger_agy = _setup_ledger_for_g4_test(
        repo_dir,
        "T-AGY-DEDUP",
        state="BLOCKED_INFRA",
        candidate_sha=sha1,
        last_g4_candidate_sha=sha1,
        last_g4_status="BLOCKED_INFRA",
        g4_invocations_by_sha={sha1: 1},
    )
    (prior_dir / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": sha1,
                "passes_completed": 1,
                "independent_contexts": False,
                "review_gate_status": "BLOCKED_INFRA",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    rc_agy = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-AGY-DEDUP",
            "--attempt-id",
            "att2",
            "--resume-from",
            str(prior_dir),
            "--ledger",
            str(ledger_agy),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "agy",
            "--model",
            "gemini-4-carbon-high",
        ]
    )
    assert rc_agy == 0
    att2_report = json.loads(
        (
            repo_dir / "harness" / "runs" / "T-AGY-DEDUP" / "attempts" / f"att2-{sha1[:12]}"
            / "consolidated-review.json"
        ).read_text(encoding="utf-8")
    )
    assert att2_report["reused_passes"] == ["pass1"]
    led_after = json.loads(ledger_agy.read_text(encoding="utf-8"))
    assert led_after["state"] == "READY_FOR_ACCEPTANCE"
    assert led_after["g4_rounds_total"] == 1
    assert led_after["last_g4_status"] == "PASSED"
    # Retry reported 100k and final reported 150k on conv-agy-1 -> high-water 150k ($0.375) +
    # 10k out ($0.15) = $0.525 (no 250k double-count, no thinking dup, no pass1 reuse charge).
    assert led_after["spent_api_equiv_usd"] == pytest.approx(0.525, abs=1e-6)


def test_g4_archived_retry_missing_fallback_usage_and_budget_pause_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bill archived transient retries + primary before missing fallback, and exit 2 on PAUSED."""
    # 1. Codex pass1 hits transient error after emitting usage, retries & succeeds;
    #    pass2 hits quota; fallback agy CLI missing -> primary + retry usage billed, rc == 2.
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_retry_bill", qualified_routes=["gemini-4-carbon-high"]
    )
    retry_p1 = tmp_path / "p1_retried.flag"
    retry_p2 = tmp_path / "p2_retried.flag"
    codex_script = tmp_path / "fake_codex_retry_bill.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            f"rf1 = pathlib.Path({str(retry_p1)!r})\n"
            f"rf2 = pathlib.Path({str(retry_p2)!r})\n"
            "if out_path.name == 'pass1.json' and not rf1.is_file():\n"
            "    rf1.write_text('1', encoding='utf-8')\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "    sys.stderr.write('503 service unavailable\\n')\n"
            "    sys.exit(1)\n"
            "if out_path.name == 'pass2.json' and not rf2.is_file():\n"
            "    rf2.write_text('1', encoding='utf-8')\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "    sys.stderr.write('503 service unavailable\\n')\n"
            "    sys.exit(1)\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {sha1!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'APPROVE', 'summary': 'ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "else:\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "    print(json.dumps({'type': 'error', 'message': 'usage_limit_reached'}), "
            "flush=True)\n"
            "    sys.exit(1)\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(
        codex_room_review,
        "resolve_agy_command",
        lambda: (_ for _ in ()).throw(FileNotFoundError("agy missing")),
    )

    ledger_retry = _setup_ledger_for_g4_test(
        repo_dir, "T-G4-RETRY-BILL", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    rc_retry = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-G4-RETRY-BILL",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc_retry == 2
    led_retry = json.loads(ledger_retry.read_text(encoding="utf-8"))
    assert led_retry["state"] == "BLOCKED_INFRA"
    assert led_retry["active_g4_reservations"] == {}
    # 4 turn.completed logs (pass1.retry0 + pass1 + pass2.retry0 + pass2.codex-failed):
    # each = 10k * $2.50/1M ($0.025) + 1k * $12/1M ($0.012) = $0.037 -> 4 * 0.037 = $0.148
    assert led_retry["spent_api_equiv_usd"] == pytest.approx(0.148, abs=1e-6)

    # 2. Both passes APPROVE (PASSED), yet G4 spend exceeds budget -> PAUSED_BUDGET & rc=2.
    repo2, sha2 = _init_policy_repo(tmp_path / "repo_tight_budget", qualified_routes=[])
    _install_counting_codex_stub(tmp_path, monkeypatch, sha2, emit_usage=True)
    from harness.scripts import task_runner

    ledger_tight = repo2 / "harness" / "runs" / "T-G4-TIGHT" / "ledger.json"
    lt = task_runner.init_or_load_ledger(
        ledger_tight, root_task_id="T-G4-TIGHT", base_sha="0" * 40, budget_usd=0.05
    )
    lt["state"] = "REVIEW_FROZEN_G4"
    lt["candidate_sha"] = sha2
    task_runner.save_ledger(ledger_tight, lt)

    rc_tight = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-G4-TIGHT",
            "--repo-root",
            str(repo2),
            "--backend",
            "codex",
        ]
    )
    lt_after = json.loads(ledger_tight.read_text(encoding="utf-8"))
    assert lt_after["spent_api_equiv_usd"] == pytest.approx(0.077, abs=1e-6)
    assert lt_after["state"] == "PAUSED_BUDGET"
    assert rc_tight == 2


def test_g4_custom_out_dir_bypass_and_concurrent_race_at_cap_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block ledgerless custom --out-dir bypass and serialize concurrent threads at cap=6."""
    import threading

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_custom_out", qualified_routes=[])
    calls_dir = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    # 1. Ledgerless task with custom --out-dir outside attempts/: 2nd call on same SHA -> rc=2.
    rc1 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-CUSTOM-OUT",
            "--out-dir",
            str(tmp_path / "custom_1"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc1 == 0
    assert _count_calls(calls_dir) == 2

    rc_dup = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-CUSTOM-OUT",
            "--out-dir",
            str(tmp_path / "custom_2"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_dup == 2
    assert _count_calls(calls_dir) == 2
    assert not (tmp_path / "custom_2" / "frozen").exists()

    # 2. Seed tracker to 5 completed rounds on T-RACE-CAP and race 2 threads on custom --out-dir.
    from harness.scripts import task_runner

    (repo_dir / "r5.txt").write_text("r5\n", encoding="utf-8")
    sha_a = _commit_repo_change(repo_dir, "race sha a")
    (repo_dir / "r6.txt").write_text("r6\n", encoding="utf-8")
    sha_b = _commit_repo_change(repo_dir, "race sha b")

    tracker_path = task_runner.default_g4_tracker_path("T-RACE-CAP", repo_root=repo_dir)
    tracker = task_runner.load_g4_tracker(tracker_path, task_id="T-RACE-CAP")
    tracker["completed_rounds"] = 5
    task_runner.save_g4_tracker(tracker_path, tracker)

    race_calls = tmp_path / "race_calls"
    race_calls.mkdir()
    race_script = tmp_path / "fake_codex_race.py"
    race_prefix = _write_fake_codex_script(
        race_script,
        (
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            "time.sleep(0.15)\n"
            f"rcd = pathlib.Path({str(race_calls)!r})\n"
            "(rcd / f'{out_path.parent.name}_{out_path.stem}.log').write_text('1', "
            "encoding='utf-8')\n"
            "is_p2 = 'security_and_invariants' in prompt\n"
            "lens = 'security_and_invariants' if is_p2 else 'correctness_and_tests'\n"
            "sha_val = None\n"
            "for ln in prompt.splitlines():\n"
            "    if ln.strip().startswith('- **Candidate SHA**:'):\n"
            "        sha_val = ln.split('`')[1]\n"
            "        break\n"
            "payload = {'revision_sha': sha_val, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: race_prefix)

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    out_a = tmp_path / "race_out_a"
    out_b = tmp_path / "race_out_b"

    def _worker(tag: str, target_sha: str, target_out: Path) -> None:
        barrier.wait(timeout=5.0)
        results[tag] = codex_room_review.main(
            [
                "--commit",
                target_sha,
                "--task-id",
                "T-RACE-CAP",
                "--out-dir",
                str(target_out),
                "--repo-root",
                str(repo_dir),
                "--backend",
                "codex",
            ]
        )

    t1 = threading.Thread(target=_worker, args=("a", sha_a, out_a))
    t2 = threading.Thread(target=_worker, args=("b", sha_b, out_b))
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)

    assert sorted(results.values()) == [0, 2]
    loser_out = out_a if results["a"] == 2 else out_b
    assert not (loser_out / "frozen").exists()
    assert not (loser_out / ".review-attempt.lock").exists()
    tracker_after = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert tracker_after["completed_rounds"] == 6
    assert tracker_after["in_flight"] == {}

    # 7th distinct-SHA call with custom --out-dir must also exit 2.
    (repo_dir / "r7.txt").write_text("r7\n", encoding="utf-8")
    sha_c = _commit_repo_change(repo_dir, "race sha c")
    out_c = tmp_path / "race_out_c"
    rc7 = codex_room_review.main(
        [
            "--commit",
            sha_c,
            "--task-id",
            "T-RACE-CAP",
            "--out-dir",
            str(out_c),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc7 == 2
    assert not (out_c / "frozen").exists()


def test_g4_union_counts_legacy_attempts_and_custom_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight counts exact union of legacy attempts/ dirs and custom --out-dir runs."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_union_g4", qualified_routes=[])
    attempts_root = repo_dir / "harness" / "runs" / "T-UNION-G4" / "attempts"
    for idx in range(5):
        p_sha = f"{idx + 1}" * 40
        p_dir = attempts_root / f"20261124T00000{idx}Z-{p_sha[:12]}"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    calls1 = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    custom_out_6 = tmp_path / "custom_union_6"
    rc6 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-UNION-G4",
            "--out-dir",
            str(custom_out_6),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc6 == 0
    assert _count_calls(calls1) == 2

    (repo_dir / "u7.txt").write_text("u7\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "union commit 7")
    calls2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    custom_out_7 = tmp_path / "custom_union_7"
    rc7 = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-UNION-G4",
            "--out-dir",
            str(custom_out_7),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc7 == 2
    assert _count_calls(calls2) == 0
    assert not (custom_out_7 / "frozen").exists()


def test_g4_blocks_inflight_resume_and_non_infra_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject --resume-from when active in-flight exists or prior verdict != BLOCKED_INFRA."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_res_guard", qualified_routes=[])
    finding = _sample_finding("P1-99", "high", "Blocking finding")
    calls_dir = _install_counting_codex_stub(
        tmp_path,
        monkeypatch,
        sha1,
        pass1_verdict="CHANGES_REQUESTED",
        pass1_findings=[finding],
    )
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-RES-GUARD", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    out1 = repo_dir / "harness" / "runs" / "T-RES-GUARD" / "attempts" / f"att1-{sha1[:12]}"
    rc1 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-GUARD",
            "--out-dir",
            str(out1),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
            "--fail-on-blocking",
        ]
    )
    assert rc1 == 1
    assert _count_calls(calls_dir) == 2

    # Sequential --resume-from after CHANGES_REQUESTED must fail closed even if state tampered.
    led_tamper = task_runner.load_ledger(ledger_path)
    led_tamper["state"] = "BLOCKED_INFRA"
    task_runner.save_ledger(ledger_path, led_tamper)
    out2 = tmp_path / "res_after_cr"
    rc_after_cr = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-GUARD",
            "--resume-from",
            str(out1),
            "--out-dir",
            str(out2),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_after_cr == 2
    assert _count_calls(calls_dir) == 2

    # Concurrent in-flight reservation must block --resume-from on BLOCKED_INFRA ledger.
    (repo_dir / "res2.txt").write_text("r2\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "inflight resume commit")
    calls2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    prior_inf = (
        repo_dir / "harness" / "runs" / "T-RES-INFLIGHT" / "attempts" / f"att1-{sha2[:12]}"
    )
    prior_inf.mkdir(parents=True, exist_ok=True)
    (prior_inf / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": sha2,
                "review_gate_status": "BLOCKED_INFRA",
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    led_inf = _setup_ledger_for_g4_test(
        repo_dir,
        "T-RES-INFLIGHT",
        state="BLOCKED_INFRA",
        candidate_sha=sha2,
        last_g4_candidate_sha=sha2,
        g4_invocations_by_sha={sha2: 1},
    )
    t_inf_path = task_runner.default_g4_tracker_path("T-RES-INFLIGHT", repo_root=repo_dir)
    t_inf = task_runner.load_g4_tracker(t_inf_path, task_id="T-RES-INFLIGHT")
    t_inf["in_flight"]["active-1"] = {
        "candidate_sha": sha2,
        "out_dir": task_runner._norm_attempt_dir(prior_inf),
    }
    task_runner.save_g4_tracker(t_inf_path, t_inf)
    ld_inf = task_runner.load_ledger(led_inf)
    ld_inf["active_g4_reservations"] = {"active-1": {"candidate_sha": sha2}}
    ld_inf["last_g4_status"] = "BLOCKED_INFRA"
    task_runner.save_ledger(led_inf, ld_inf)

    rc_inf = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-RES-INFLIGHT",
            "--resume-from",
            str(prior_inf),
            "--out-dir",
            str(tmp_path / "res_inflight_out"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_inf == 2
    assert _count_calls(calls2) == 0

    # Finalization guard: prior CHANGES_REQUESTED on sha1 must never become READY_FOR_ACCEPTANCE.
    led_final = task_runner.load_ledger(ledger_path)
    led_final["state"] = "REVIEW_FROZEN_G4"
    led_final["candidate_sha"] = sha1
    task_runner.save_ledger(ledger_path, led_final)
    t_guard_path = task_runner.default_g4_tracker_path("T-RES-GUARD", repo_root=repo_dir)
    p1_state = codex_room_review.PassState(
        pass_name="pass1", lens="correctness_and_tests", backend="codex", model="gpt-6.1-sol"
    )
    p2_state = codex_room_review.PassState(
        pass_name="pass2",
        lens="security_and_invariants",
        backend="codex",
        model="gpt-6.1-sol",
    )
    final_state = codex_room_review._record_g4_outcome(
        t_guard_path,
        ledger_path,
        None,
        task_id="T-RES-GUARD",
        git_top=repo_dir,
        out_dir=tmp_path / "res_finalize_probe",
        candidate_sha=sha1,
        report={"review_gate_status": "PASSED", "errors": []},
        states={"pass1": p1_state, "pass2": p2_state},
        reused_passes=["pass1", "pass2"],
        active_backend="codex",
        active_model="gpt-6.1-sol",
        primary_model="gpt-6.1-sol",
        route_models_by_tag={},
    )
    assert final_state == "BLOCKED"
    led_checked = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_checked["state"] == "BLOCKED"
    assert led_checked["candidate_sha"] is None


def test_g4_codex_and_agy_retries_preserve_all_archives_and_bill_accurately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex retry + quota -> AGY fallback retry preserves all archives and bills both."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_dual_retry", qualified_routes=["gemini-4-carbon-high"]
    )
    codex_calls = tmp_path / "dual_codex_count.txt"
    codex_script = tmp_path / "fake_codex_dual_retry.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            "if out_path.name == 'pass1.json':\n"
            f"    payload = {{'revision_sha': {sha1!r}, 'lens': 'correctness_and_tests', "
            "'verdict': 'APPROVE', 'summary': 'ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "    out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "    sys.exit(0)\n"
            f"cf = pathlib.Path({str(codex_calls)!r})\n"
            "n = len(cf.read_text(encoding='utf-8').splitlines()) if cf.is_file() else 0\n"
            "cf.write_text('x\\n' * (n + 1), encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
            "if n == 0:\n"
            "    sys.stderr.write('503 service unavailable\\n')\n"
            "    sys.exit(1)\n"
            "print(json.dumps({'type': 'error', 'message': 'usage_limit_reached'}), flush=True)\n"
            "sys.exit(1)\n"
        ),
    )
    agy_calls = tmp_path / "dual_agy_count.txt"
    agy_script = tmp_path / "fake_agy_dual_retry.py"
    agy_prefix = _write_fake_codex_script(
        agy_script,
        (
            "import json, pathlib, sys\n"
            f"af = pathlib.Path({str(agy_calls)!r})\n"
            "n = len(af.read_text(encoding='utf-8').splitlines()) if af.is_file() else 0\n"
            "af.write_text('x\\n' * (n + 1), encoding='utf-8')\n"
            "if n == 0:\n"
            "    print(json.dumps({'event': 'result', 'conversation_id': 'conv-dual-p2', "
            "'result': {'status': 'ERROR', 'conversation_id': 'conv-dual-p2', "
            "'error': '503 service unavailable', "
            "'usage': {'input_tokens': 100000, 'cache_read_tokens': 0, "
            "'output_tokens': 10000, 'thinking_tokens': 2000}}}), flush=True)\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': 'security_and_invariants', "
            "'verdict': 'APPROVE', 'summary': 'ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'conversation_id': 'conv-dual-p2', "
            "'result': {'status': 'SUCCESS', 'conversation_id': 'conv-dual-p2', "
            "'structured_output': payload, "
            "'usage': {'input_tokens': 150000, 'cache_read_tokens': 0, "
            "'output_tokens': 10000, 'thinking_tokens': 2000}}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-DUAL-RETRY", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    out_dir = repo_dir / "harness" / "runs" / "T-DUAL-RETRY" / "attempts" / f"att1-{sha1[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-DUAL-RETRY",
            "--out-dir",
            str(out_dir),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 0
    assert (out_dir / "pass2.codex-gpt-6.1-sol-retry0.raw.jsonl").is_file()
    assert (out_dir / "pass2.codex-failed.raw.jsonl").is_file()
    assert (out_dir / "pass2.agy-gemini-4-carbon-high-retry0.raw.jsonl").is_file()
    assert (out_dir / "pass2.raw.jsonl").is_file()

    # Wait: pass1 ($0.037) + codex p2 retry0 ($0.037) + codex p2 failed ($0.037) +
    # agy high-water 150k/10k ($0.525) = $0.636.
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["spent_api_equiv_usd"] == pytest.approx(0.636, abs=1e-6)
    assert led_after["state"] == "READY_FOR_ACCEPTANCE"


def test_g4_cross_process_os_lock_race_for_sixth_slot(tmp_path: Path) -> None:
    """Two real OS child processes racing for the 6th G4 slot serialize with rc {0, 2}."""
    repo_dir, _ = _init_policy_repo(tmp_path / "repo_proc_race", qualified_routes=[])
    (repo_dir / "pr5.txt").write_text("p5\n", encoding="utf-8")
    sha_a = _commit_repo_change(repo_dir, "proc race sha a")
    (repo_dir / "pr6.txt").write_text("p6\n", encoding="utf-8")
    sha_b = _commit_repo_change(repo_dir, "proc race sha b")

    attempts_root = repo_dir / "harness" / "runs" / "T-PROC-RACE" / "attempts"
    for idx in range(5):
        p_sha = f"{idx + 1}" * 40
        p_dir = attempts_root / f"20261124T00000{idx}Z-{p_sha[:12]}"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    fake_codex = tmp_path / "proc_fake_codex.py"
    _write_fake_codex_script(
        fake_codex,
        (
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            "time.sleep(0.25)\n"
            "is_p2 = 'security_and_invariants' in prompt\n"
            "lens = 'security_and_invariants' if is_p2 else 'correctness_and_tests'\n"
            "sha_val = None\n"
            "for ln in prompt.splitlines():\n"
            "    if ln.strip().startswith('- **Candidate SHA**:'):\n"
            "        sha_val = ln.split('`')[1]\n"
            "        break\n"
            "payload = {'revision_sha': sha_val, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    go_flag = tmp_path / "proc_go.flag"
    driver = tmp_path / "proc_driver.py"
    driver.write_text(
        (
            "import pathlib, sys, time\n"
            "from harness.scripts import codex_room_review\n"
            "repo_root, sha, out_dir, ready_f, go_f, codex_sc = sys.argv[1:7]\n"
            "codex_room_review.verify_role = lambda role, root: {'ok': True, 'errors': []}\n"
            "codex_room_review.resolve_codex_command = lambda: [sys.executable, codex_sc]\n"
            "pathlib.Path(ready_f).write_text('1', encoding='utf-8')\n"
            "deadline = time.monotonic() + 15.0\n"
            "while not pathlib.Path(go_f).is_file() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "rc = codex_room_review.main([\n"
            "    '--commit', sha, '--task-id', 'T-PROC-RACE',\n"
            "    '--out-dir', out_dir, '--repo-root', repo_root, '--backend', 'codex',\n"
            "])\n"
            "sys.exit(rc)\n"
        ),
        encoding="utf-8",
    )

    ready_a = tmp_path / "ready_a.flag"
    ready_b = tmp_path / "ready_b.flag"
    out_a = tmp_path / "proc_out_a"
    out_b = tmp_path / "proc_out_b"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)

    proc_a = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(repo_dir),
            sha_a,
            str(out_a),
            str(ready_a),
            str(go_flag),
            str(fake_codex),
        ],
        env=env,
        cwd=str(REPO_ROOT),
    )
    proc_b = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(repo_dir),
            sha_b,
            str(out_b),
            str(ready_b),
            str(go_flag),
            str(fake_codex),
        ],
        env=env,
        cwd=str(REPO_ROOT),
    )
    import time

    deadline = time.monotonic() + 15.0
    while (not ready_a.is_file() or not ready_b.is_file()) and time.monotonic() < deadline:
        time.sleep(0.02)
    go_flag.write_text("go\n", encoding="utf-8")
    rc_a = proc_a.wait(timeout=30)
    rc_b = proc_b.wait(timeout=30)

    assert sorted([rc_a, rc_b]) == [0, 2]
    loser_out = out_a if rc_a == 2 else out_b
    assert not (loser_out / "frozen").exists()
    assert not (loser_out / ".review-attempt.lock").exists()
    from harness.scripts import task_runner

    t_path = task_runner.default_g4_tracker_path("T-PROC-RACE", repo_root=repo_dir)
    t_after = json.loads(t_path.read_text(encoding="utf-8"))
    assert t_after["completed_rounds"] == 1
    assert t_after["in_flight"] == {}
    assert len(t_after["completed_attempts"]) == 6


def test_g4_persists_non_canonical_attempts_across_out_dir_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Custom X/attempts/* discovered on call 6 are persisted in tracker across out-dir switch."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_persist_ext", qualified_routes=[])
    custom_x_attempts = tmp_path / "custom_x" / "attempts"
    for idx in range(5):
        p_sha = f"{idx + 1}" * 40
        p_dir = custom_x_attempts / f"a{idx + 1}-{p_sha[:12]}"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    calls1 = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    out6 = custom_x_attempts / f"a6-{sha1[:12]}"
    rc6 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-EXT-PERSIST",
            "--out-dir",
            str(out6),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc6 == 0
    assert _count_calls(calls1) == 2

    t_path = task_runner.default_g4_tracker_path("T-EXT-PERSIST", repo_root=repo_dir)
    t_data = task_runner.load_g4_tracker(t_path, task_id="T-EXT-PERSIST")
    canon_attempts = repo_dir / "harness" / "runs" / "T-EXT-PERSIST" / "attempts"
    union_check = task_runner.collect_g4_attempt_union([canon_attempts], t_data)
    assert union_check["total_rounds"] == 6

    (repo_dir / "ext7.txt").write_text("ext7\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "ext switch commit 7")
    calls2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    out7 = tmp_path / "custom_y" / "a7"
    rc7 = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-EXT-PERSIST",
            "--out-dir",
            str(out7),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc7 == 2
    assert _count_calls(calls2) == 0

    # G5 receipt.py also observes >= 6 rounds from g4-tracker.json (and blocks at 7).
    extra_canon = canon_attempts / f"a_extra-{sha2[:12]}"
    extra_canon.mkdir(parents=True, exist_ok=True)
    (extra_canon / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": sha2,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "PASSED",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    v_path = tmp_path / "v_ext.json"
    r_path = tmp_path / "r_ext.json"
    v_path.write_text(
        json.dumps({"status": "VERIFIED", "phase": 1, "git_sha": sha2, "steps": []}),
        encoding="utf-8",
    )
    r_path.write_text(
        json.dumps(
            {
                "revision_sha": sha2,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "PASSED",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    rec_eval = receipt_mod.evaluate_receipt(
        task_id="T-EXT-PERSIST",
        phase=1,
        base_sha="0" * 40,
        candidate_sha=sha2,
        verifier_data=json.loads(v_path.read_text(encoding="utf-8")),
        review_data=json.loads(r_path.read_text(encoding="utf-8")),
        repo_root=repo_dir,
    )
    assert rec_eval["conclusion"] == "BLOCKED"
    assert any("7" in b and "max_g4_rounds_per_root_task" in b for b in rec_eval["blockers"])


def test_g4_rejects_resume_from_missing_consolidated_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--resume-from to a directory missing consolidated-review.json fails closed (rc == 2)."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_missing_rev", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    empty_dir = tmp_path / "empty_prior_dir"
    empty_dir.mkdir()
    _setup_ledger_for_g4_test(
        repo_dir,
        "T-RES-EMPTY-LED",
        state="BLOCKED_INFRA",
        candidate_sha=sha1,
        last_g4_candidate_sha=sha1,
        last_g4_status="BLOCKED_INFRA",
        g4_invocations_by_sha={sha1: 1},
    )
    rc_led = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-EMPTY-LED",
            "--resume-from",
            str(empty_dir),
            "--out-dir",
            str(tmp_path / "out_res_empty_led"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_led == 2
    assert _count_calls(calls) == 0

    # Also fail closed in ledgerless mode when sha1 was already invoked once.
    t_path = task_runner.default_g4_tracker_path("T-RES-EMPTY-NOLOG", repo_root=repo_dir)
    t_nolog = task_runner.load_g4_tracker(t_path, task_id="T-RES-EMPTY-NOLOG")
    t_nolog["in_flight"]["res-abort"] = {
        "candidate_sha": sha1,
        "out_dir": task_runner._norm_attempt_dir(empty_dir),
    }
    t_nolog["sha_invocations"][sha1] = 1
    task_runner.save_g4_tracker(t_path, t_nolog)
    codex_room_review._release_g4_reservation_on_abort(
        t_path, None, "res-abort", git_top=repo_dir, candidate_sha=sha1
    )
    t_after_abort = task_runner.load_g4_tracker(t_path, task_id="T-RES-EMPTY-NOLOG")
    norm_empty = task_runner._norm_attempt_dir(empty_dir)
    assert t_after_abort["completed_attempts"][norm_empty]["status"] == "ABORTED"

    rc_nolog = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-EMPTY-NOLOG",
            "--resume-from",
            str(empty_dir),
            "--out-dir",
            str(tmp_path / "out_res_empty_nolog"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_nolog == 2
    assert _count_calls(calls) == 0


def test_g4_persists_lock_only_attempts_across_out_dir_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 lock-only attempts under custom_x/attempts persist in tracker across --out-dir switch."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_lock_ext", qualified_routes=[])
    custom_x_attempts = tmp_path / "custom_lock_x" / "attempts"
    for idx in range(5):
        p_sha = f"{idx + 1}" * 40
        p_dir = custom_x_attempts / f"a{idx + 1}-{p_sha[:12]}"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / ".review-attempt.lock").write_text(
            json.dumps({"candidate_sha": p_sha, "task_id": "T-EXT-LOCK"}),
            encoding="utf-8",
        )

    calls1 = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    out6 = custom_x_attempts / f"a6-{sha1[:12]}"
    rc6 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-EXT-LOCK",
            "--out-dir",
            str(out6),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc6 == 0
    assert _count_calls(calls1) == 2

    (repo_dir / "l7.txt").write_text("l7\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "lock ext commit 7")
    calls2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    rc7 = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "T-EXT-LOCK",
            "--out-dir",
            str(tmp_path / "custom_lock_y" / "a7"),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc7 == 2
    assert _count_calls(calls2) == 0

    t_path = task_runner.default_g4_tracker_path("T-EXT-LOCK", repo_root=repo_dir)
    t_data = task_runner.load_g4_tracker(t_path, task_id="T-EXT-LOCK")
    canon_attempts = repo_dir / "harness" / "runs" / "T-EXT-LOCK" / "attempts"
    assert task_runner.collect_g4_attempt_union([canon_attempts], t_data)["total_rounds"] >= 6

    extra = canon_attempts / f"att-extra-{sha2[:12]}"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": sha2,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "PASSED",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    rec = receipt_mod.evaluate_receipt(
        task_id="T-EXT-LOCK",
        phase=1,
        base_sha="0" * 40,
        candidate_sha=sha2,
        verifier_data={"status": "VERIFIED", "phase": 1, "git_sha": sha2, "steps": []},
        review_data={
            "revision_sha": sha2,
            "passes_completed": 2,
            "independent_contexts": True,
            "review_gate_status": "PASSED",
            "blocking_open_count": 0,
            "consolidated_findings": [],
        },
        repo_root=repo_dir,
    )
    assert rec["conclusion"] == "BLOCKED"
    assert any("7" in b and "max_g4_rounds_per_root_task" in b for b in rec["blockers"])


def test_g4_pauses_budget_when_committed_spend_with_active_reservations_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spent + active_reservations > limit pauses G4 at finalize and blocks G5 receipt."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_committed_bud", qualified_routes=[])
    _install_counting_codex_stub(tmp_path, monkeypatch, sha1, emit_usage=True)

    ledger_path = repo_dir / "harness" / "runs" / "T-COMMITTED-BUD" / "ledger.json"
    led = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-COMMITTED-BUD", base_sha="0" * 40, budget_usd=10.0
    )
    led["state"] = "REVIEW_FROZEN_G4"
    led["candidate_sha"] = sha1
    led["spent_api_equiv_usd"] = 9.0
    led["active_reservations"] = {"res-1": 0.99}
    task_runner.save_ledger(ledger_path, led)

    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-COMMITTED-BUD",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["spent_api_equiv_usd"] == pytest.approx(9.077, abs=1e-6)
    assert led_after["state"] == "PAUSED_BUDGET"

    # Even if state were coerced back to READY_FOR_ACCEPTANCE, G5 still blocks on committed > limit.
    led_tamper = task_runner.load_ledger(ledger_path)
    led_tamper["state"] = "READY_FOR_ACCEPTANCE"
    led_tamper["candidate_sha"] = sha1
    task_runner.save_ledger(ledger_path, led_tamper)
    v_path = tmp_path / "v_comm.json"
    r_path = tmp_path / "r_comm.json"
    v_path.write_text(
        json.dumps(
            {
                "status": "VERIFIED",
                "phase": 1,
                "git_sha": sha1,
                "worktree_dirty": False,
                "steps": [
                    {"name": "arch_check", "status": "PASSED", "exit_code": 0},
                    {"name": "harness_ruff", "status": "PASSED", "exit_code": 0},
                    {"name": "harness_pytest", "status": "PASSED", "exit_code": 0},
                    {"name": "backend_ruff", "status": "PASSED", "exit_code": 0},
                    {"name": "backend_pytest", "status": "PASSED", "exit_code": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    r_path.write_text(
        json.dumps(
            {
                "revision_sha": sha1,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "PASSED",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    out_g5 = tmp_path / "out_g5_comm"
    rc_g5 = receipt_mod.main(
        [
            "--task-id",
            "T-COMMITTED-BUD",
            "--phase",
            "1",
            "--base-sha",
            "0" * 40,
            "--candidate-sha",
            sha1,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_g5),
        ]
    )
    assert rc_g5 == 2
    rec_g5 = json.loads((out_g5 / "receipt.json").read_text(encoding="utf-8"))
    assert rec_g5["conclusion"] == "BLOCKED"
    assert any("committed spend" in b for b in rec_g5["blockers"])


def test_g4_root_discovery_foreign_ledger_monotonic_counter_and_reused_billing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify root discovery, foreign ledger rejection, monotonic legacy_untracked_rounds, reuse."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_a6_cov", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    # 1. Root ledger ROOT paused while child task_id=CHILD-V2 runs without --ledger.
    root_ledger = _setup_ledger_for_g4_test(
        repo_dir, "ROOT", state="PAUSED_BUDGET", candidate_sha=sha1
    )
    r_data = task_runner.load_ledger(root_ledger)
    r_data["task_id"] = "CHILD-V2"
    task_runner.save_ledger(root_ledger, r_data)
    rc_root = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "CHILD-V2",
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_root == 2
    assert _count_calls(calls) == 0

    # 2. Foreign --ledger OTHER/ledger.json with --task-id PAUSED-TASK must be rejected (rc == 2).
    _setup_ledger_for_g4_test(
        repo_dir, "PAUSED-TASK", state="PAUSED_BUDGET", candidate_sha=sha1
    )
    other_ledger = _setup_ledger_for_g4_test(
        repo_dir, "OTHER", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    rc_foreign = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "PAUSED-TASK",
            "--ledger",
            str(other_ledger),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc_foreign == 2
    assert _count_calls(calls) == 0

    # 3. Counter-only tracker (completed_rounds=5) + 1 filesystem dir across 2 consecutive calls.
    mono_tracker_path = task_runner.default_g4_tracker_path("T-MONO-CNT", repo_root=repo_dir)
    mono_tracker = task_runner.load_g4_tracker(mono_tracker_path, task_id="T-MONO-CNT")
    mono_tracker["completed_rounds"] = 5
    mono_tracker.pop("legacy_untracked_rounds", None)
    task_runner.save_g4_tracker(mono_tracker_path, mono_tracker)

    mono_att1 = (
        repo_dir / "harness" / "runs" / "T-MONO-CNT" / "attempts" / f"att1-{sha1[:12]}"
    )
    mono_att1.mkdir(parents=True, exist_ok=True)
    (mono_att1 / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": sha1,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "CHANGES_REQUESTED",
                "blocking_open_count": 1,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )

    (repo_dir / "mono2.txt").write_text("m2\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "mono commit 2")
    calls2 = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    for idx in range(2):
        rc_m = codex_room_review.main(
            [
                "--commit",
                sha2,
                "--task-id",
                "T-MONO-CNT",
                "--out-dir",
                str(tmp_path / f"mono_out_{idx}"),
                "--repo-root",
                str(repo_dir),
                "--backend",
                "codex",
            ]
        )
        assert rc_m == 2
        assert _count_calls(calls2) == 0
        t_cur = task_runner.load_g4_tracker(mono_tracker_path, task_id="T-MONO-CNT")
        u_cur = task_runner.collect_g4_attempt_union(
            [repo_dir / "harness" / "runs" / "T-MONO-CNT" / "attempts"], t_cur
        )
        assert u_cur["total_rounds"] == 6

    # 4. Direct _bill_pass_raw_logs(..., reused_passes=["pass1"]) skips billing pass1.raw.jsonl.
    bill_dir = tmp_path / "direct_bill_dir"
    bill_dir.mkdir()
    (bill_dir / "pass1.raw.jsonl").write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10000, "cached_input_tokens": 0, "output_tokens": 1000},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    p1 = codex_room_review.PassState(
        pass_name="pass1", lens="correctness_and_tests", backend="codex", model="gpt-6.1-sol"
    )
    p2 = codex_room_review.PassState(
        pass_name="pass2", lens="security_and_invariants", backend="codex", model="gpt-6.1-sol"
    )
    led_reused = _setup_ledger_for_g4_test(
        repo_dir, "T-BILL-REUSE", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    codex_room_review._bill_pass_raw_logs(
        led_reused,
        out_dir=bill_dir,
        states={"pass1": p1, "pass2": p2},
        reused_passes=["pass1"],
        active_backend="codex",
        active_model="gpt-6.1-sol",
        primary_model="gpt-6.1-sol",
        route_models_by_tag={},
    )
    assert json.loads(led_reused.read_text(encoding="utf-8"))["spent_api_equiv_usd"] == 0.0
    codex_room_review._bill_pass_raw_logs(
        led_reused,
        out_dir=bill_dir,
        states={"pass1": p1, "pass2": p2},
        reused_passes=[],
        active_backend="codex",
        active_model="gpt-6.1-sol",
        primary_model="gpt-6.1-sol",
        route_models_by_tag={},
    )
    assert json.loads(led_reused.read_text(encoding="utf-8"))["spent_api_equiv_usd"] > 0.0


def test_g4_ledgerless_root_packet_blocks_child_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ledger.json, CHILD-V2 declaring Root Task ID=ROOT shares ROOT's 6 attempts."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_nolog_root", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)

    root_attempts = repo_dir / "harness" / "runs" / "ROOT" / "attempts"
    for idx in range(6):
        p_sha = f"{idx + 1}" * 40
        p_dir = root_attempts / f"att-{idx}-{p_sha[:12]}"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    pkt = tmp_path / "child_v2_packet.md"
    pkt.write_text(
        "# Task Packet\n\n- **Root Task ID**: `ROOT`\n- **Task ID**: `CHILD-V2`\n",
        encoding="utf-8",
    )
    child_out = tmp_path / "child_v2_out"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "CHILD-V2",
            "--task-packet",
            str(pkt),
            "--out-dir",
            str(child_out),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    assert _count_calls(calls) == 0
    assert not (child_out / "frozen").exists()


def test_g4_resume_preserves_blocking_findings_when_provenance_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance-mismatched --resume-from preserves prior CHANGES_REQUESTED findings."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_res_preserve", qualified_routes=[])
    finding = _sample_finding("P1-77", "high", "Critical unhandled exception")
    findings_json = json.dumps([finding])

    p2_fail_flag = tmp_path / "p2_fail_once.flag"
    codex_script = tmp_path / "fake_codex_res_preserve.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "_ = sys.stdin.read()\n"
            f"flag = pathlib.Path({str(p2_fail_flag)!r})\n"
            "if not flag.is_file():\n"
            "    if out_path.name == 'pass1.json':\n"
            f"        payload = {{'revision_sha': {sha1!r}, 'lens': 'correctness_and_tests', "
            f"'verdict': 'CHANGES_REQUESTED', 'summary': 'bad', 'findings': {findings_json}, "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "        out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "        sys.exit(0)\n"
            "    print(json.dumps({'type': 'error', 'message': 'usage_limit_reached'}), "
            "flush=True)\n"
            "    sys.exit(1)\n"
            "lens = 'security_and_invariants' if out_path.name == 'pass2.json' "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'new clean pass', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)

    pkt = tmp_path / "pkt_res_1.md"
    pkt.write_text("# Task Packet v1\n", encoding="utf-8")
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-RES-PRESERVE", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    att1 = repo_dir / "harness" / "runs" / "T-RES-PRESERVE" / "attempts" / f"att1-{sha1[:12]}"
    rc1 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-PRESERVE",
            "--task-packet",
            str(pkt),
            "--out-dir",
            str(att1),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc1 == 2

    # Invalidate 7-tuple provenance by editing packet, and return APPROVE on resumed passes.
    pkt.write_text("# Task Packet v1 amended\n", encoding="utf-8")
    p2_fail_flag.write_text("1", encoding="utf-8")
    att2 = repo_dir / "harness" / "runs" / "T-RES-PRESERVE" / "attempts" / f"att2-{sha1[:12]}"
    rc2 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-RES-PRESERVE",
            "--task-packet",
            str(pkt),
            "--resume-from",
            str(att1),
            "--out-dir",
            str(att2),
            "--repo-root",
            str(repo_dir),
            "--backend",
            "codex",
            "--fail-on-blocking",
        ]
    )
    assert rc2 == 1
    report2 = json.loads((att2 / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report2["review_gate_status"] == "CHANGES_REQUESTED"
    assert report2["blocking_open_count"] == 1
    assert any(f.get("id") == "P1-77" for f in report2["consolidated_findings"])
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["state"] != "READY_FOR_ACCEPTANCE"
    assert led_after["candidate_sha"] is None


def test_g4_stale_reservation_ignored_after_replan_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale G4 completion after packet_version replan bills spend without clobbering state."""
    import threading

    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_stale_res", qualified_routes=[])
    entered_flag = tmp_path / "codex_entered.flag"
    release_flag = tmp_path / "codex_release.flag"

    codex_script = tmp_path / "fake_codex_barrier.py"
    codex_prefix = _write_fake_codex_script(
        codex_script,
        (
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            f"entered = pathlib.Path({str(entered_flag)!r})\n"
            f"release = pathlib.Path({str(release_flag)!r})\n"
            "entered.write_text('1', encoding='utf-8')\n"
            "deadline = time.monotonic() + 15.0\n"
            "while not release.is_file() and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n"
            "lens = 'security_and_invariants' if out_path.name == 'pass2.json' "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], 'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 0, 'output_tokens': 1000}}), "
            "flush=True)\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)

    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-STALE-RES", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    rc_box: list[int] = []

    def _run_g4() -> None:
        rc_box.append(
            codex_room_review.main(
                [
                    "--commit",
                    sha1,
                    "--task-id",
                    "T-STALE-RES",
                    "--out-dir",
                    str(tmp_path / "stale_res_out"),
                    "--repo-root",
                    str(repo_dir),
                    "--backend",
                    "codex",
                ]
            )
        )

    th = threading.Thread(target=_run_g4)
    th.start()
    import time

    deadline = time.monotonic() + 15.0
    while not entered_flag.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert entered_flag.is_file()

    # Simulate packet_version advance while stale G4 is blocked inside reviewer subprocess.
    with task_runner._ledger_lock(ledger_path):
        cur = task_runner.load_ledger(ledger_path)
        cur["packet_version"] = 2
        cur["task_id"] = "T-STALE-RES-V2"
        cur["candidate_sha"] = None
        cur["g4_rounds_in_packet"] = 0
        cur["state"] = "ACTIVE_PEER_G1"
        task_runner.save_ledger(ledger_path, cur)

    release_flag.write_text("go\n", encoding="utf-8")
    th.join(timeout=20.0)

    assert rc_box == [2]
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["state"] == "ACTIVE_PEER_G1"
    assert led_after["candidate_sha"] is None
    assert led_after["g4_rounds_in_packet"] == 0
    assert led_after["g4_rounds_total"] == 1
    assert led_after["spent_api_equiv_usd"] == pytest.approx(0.074, abs=1e-6)
    assert any(ev.get("kind") == "G4_STALE_RESERVATION_IGNORED" for ev in led_after["events"])


def test_codex_5h_window_exhausted_preflight_triggers_agy_fallback_and_strips_pii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real JSON-RPC app-server probe detects 5h window 100%, skips codex exec, strips PII."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_5h_guard", qualified_routes=["gemini-4-carbon-high"]
    )
    rt_root = tmp_path / "codex_rt_5h"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    exec_calls = tmp_path / "codex_exec_5h_calls"
    exec_calls.mkdir()
    pct_file = tmp_path / "codex_window_pct.txt"
    fake_codex = tmp_path / "fake_codex_app_server.py"
    codex_prefix = _write_fake_codex_script(
        fake_codex,
        (
            "import json, os, pathlib, sys\n"
            f"pct_p = pathlib.Path({str(pct_file)!r})\n"
            f"exec_d = pathlib.Path({str(exec_calls)!r})\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'app-server':\n"
            "    assert pathlib.Path(os.environ['CODEX_HOME']).name == 'topstep-review-pass1'\n"
            "    pct = float(pct_p.read_text(encoding='utf-8').strip())\n"
            "    for raw in sys.stdin:\n"
            "        raw = raw.strip()\n"
            "        if not raw:\n"
            "            continue\n"
            "        msg = json.loads(raw)\n"
            "        mid = msg.get('id')\n"
            "        method = msg.get('method')\n"
            "        if method == 'initialize':\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': "
            "{'userAgent': 'codex-test'}}), flush=True)\n"
            "        elif method == 'account/rateLimits/read':\n"
            "            res = {'rateLimits': {'limitId': 'codex', "
            "'accountId': 'acct-secret-pii-999', "
            "'primary': {'usedPercent': pct, 'windowDurationMins': 300, 'resetsAt': 1795651200}, "
            "'secondary': {'usedPercent': 12.0, 'windowDurationMins': 10080, "
            "'resetsAt': 1796000000}, "
            "'credits': {'hasCredits': True, 'unlimited': False, 'balance': '50.0'}, "
            "'ordinaryUsageAllowed': True, 'spendControlReached': False}}\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': res}), "
            "flush=True)\n"
            "    sys.exit(0)\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "(exec_d / f'{out_path.stem}.log').write_text('exec\\n', encoding='utf-8')\n"
            "lens = 'security_and_invariants' if out_path.name == 'pass2.json' "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'codex ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    agy_calls = tmp_path / "agy_5h_calls"
    agy_calls.mkdir()
    fake_agy = tmp_path / "fake_agy_5h.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, pathlib, sys\n"
            f"ad = pathlib.Path({str(agy_calls)!r})\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            "(ad / f'{lens}.log').write_text('agy\\n', encoding='utf-8')\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    # Case 1: primary.usedPercent == 90.0 under --backend auto -> 0 codex exec calls, agy fallback.
    pct_file.write_text("90.0", encoding="utf-8")
    out_auto = tmp_path / "out_5h_auto"
    rc_auto = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_auto),
            "--backend",
            "auto",
        ]
    )
    assert rc_auto == 0
    assert _count_calls(exec_calls) == 0
    assert _count_calls(agy_calls) == 2
    rep_auto = json.loads((out_auto / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep_auto["review_backend"] == "agy"
    assert rep_auto["fallback_used"] is True
    assert "codex_5h_window_exhausted" in rep_auto["fallback_reason"]
    assert "openai_codex" in rep_auto["exhausted_quota_pools"]
    rl_snap_text = (out_auto / "codex-ratelimits-preflight.json").read_text(encoding="utf-8")
    assert "accountId" not in rl_snap_text
    assert "acct-secret-pii-999" not in rl_snap_text

    # Case 2: primary.usedPercent == 90.0 under --backend codex -> 0 calls -> BLOCKED_INFRA.
    for old_f in agy_calls.iterdir():
        old_f.unlink()
    out_codex_only = tmp_path / "out_5h_codex_only"
    rc_only = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_codex_only),
            "--backend",
            "codex",
        ]
    )
    assert rc_only == 2
    assert _count_calls(exec_calls) == 0
    assert _count_calls(agy_calls) == 0
    rep_only = json.loads(
        (out_codex_only / "consolidated-review.json").read_text(encoding="utf-8")
    )
    assert rep_only["review_gate_status"] == "BLOCKED_INFRA"

    # Case 3: primary.usedPercent == 89.0 (< 90) under --backend auto -> codex exec runs (2 calls).
    pct_file.write_text("89.0", encoding="utf-8")
    out_ok = tmp_path / "out_5h_ok"
    rc_ok = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_ok),
            "--backend",
            "auto",
        ]
    )
    assert rc_ok == 0
    assert _count_calls(exec_calls) == 2
    assert _count_calls(agy_calls) == 0
    rep_ok = json.loads((out_ok / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep_ok["review_backend"] == "codex"
    assert rep_ok["fallback_used"] is False

    # Case 4: Top-level ordinaryUsageAllowed=False triggers codex_usage_disallowed cleanly.
    disallowed, d_reason, d_snap = codex_room_review.evaluate_codex_window_exhaustion(
        {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 20}}}
    )
    assert disallowed is True
    assert d_reason is not None and "codex_usage_disallowed" in d_reason
    assert isinstance(d_snap, dict) and d_snap.get("ordinaryUsageAllowed") is False


def test_codex_window_guard_secondary_snake_case_invalid_config_and_probe_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5h guard handles secondary 100%, snake_case fields, invalid config, and probe timeout."""
    sec_hit, sec_reason, _ = codex_room_review.evaluate_codex_window_exhaustion(
        {"rateLimits": {"primary": {"usedPercent": 10.0}, "secondary": {"usedPercent": 100.0}}}
    )
    assert sec_hit is True
    assert sec_reason is not None and "secondary_window_exhausted" in sec_reason

    snake_hit, snake_reason, _ = codex_room_review.evaluate_codex_window_exhaustion(
        {"rate_limits": {"primary": {"used_percent": 90.0}}}
    )
    assert snake_hit is True
    assert snake_reason is not None and "codex_5h_window_exhausted" in snake_reason

    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_bad_cfg", qualified_routes=["gemini-4-carbon-high"]
    )
    pol_path = repo_dir / "harness" / "model-policy.json"
    pol = json.loads(pol_path.read_text(encoding="utf-8"))
    pol["roles"]["independent_reviewer_primary"]["primary_window_exhausted_percent"] = "ninety"
    pol_path.write_text(json.dumps(pol, indent=2), encoding="utf-8")
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    rt_root = tmp_path / "rt_bad_cfg"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    out_dir = tmp_path / "out_bad_cfg"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 2
    assert _count_calls(calls) == 0

    pid_file = tmp_path / "hung_probe.pid"
    hanging_script = tmp_path / "fake_codex_hang_probe.py"
    hang_prefix = _write_fake_codex_script(
        hanging_script,
        (
            "import os, pathlib, time\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(60)\n"
        ),
    )
    res = codex_room_review.probe_codex_rate_limits(
        hang_prefix,
        rt_root / "topstep-review-pass1",
        init_timeout_sec=0.25,
        read_timeout_sec=0.25,
    )
    assert res is None
    hung_pid = int(pid_file.read_text(encoding="utf-8").strip())
    import time as _time

    deadline = _time.monotonic() + 1.5
    while _is_pid_alive(hung_pid) and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert not _is_pid_alive(hung_pid)


def test_g4_canary_prefix_with_attempts_or_packet_enforces_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """canary-* task_id cannot bypass G4 caps when attempts/ or Root Task ID is present."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_canary_cap", qualified_routes=[])
    root_attempts = repo_dir / "harness" / "runs" / "ROOT" / "attempts"
    for idx in range(6):
        p_sha = f"{idx + 1}" * 40
        att = root_attempts / f"att-{idx}-{p_sha[:12]}"
        att.mkdir(parents=True, exist_ok=True)
        (att / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )
    pkt = tmp_path / "canary_packet.md"
    pkt.write_text("# Task Packet\n\n- **Root Task ID**: `ROOT`\n", encoding="utf-8")
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    out_dir = repo_dir / "harness" / "runs" / "canary-bypass" / "attempts" / f"r1-{sha1[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "canary-bypass",
            "--task-packet",
            str(pkt),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    assert _count_calls(calls) == 0
    assert not out_dir.exists()


def test_g4_sibling_child_runs_union_persists_tracker_and_fails_closed_on_corrupt_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling child attempts count toward root cap, persist, and corrupt tracker blocks."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_sib_union", qualified_routes=[])
    root_attempts = repo_dir / "harness" / "runs" / "ROOT" / "attempts"
    for idx in range(5):
        p_sha = f"{idx + 1}" * 40
        att = root_attempts / f"att-{idx}-{p_sha[:12]}"
        att.mkdir(parents=True, exist_ok=True)
        (att / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": p_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                    "blocking_open_count": 1,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )

    c1_dir = repo_dir / "harness" / "runs" / "CHILD-V1"
    c1_dir.mkdir(parents=True, exist_ok=True)
    (c1_dir / "packet.md").write_text(
        "# Packet\n\n- **Root Task ID**: `ROOT`\n- **Packet Version**: `1`\n",
        encoding="utf-8",
    )
    c1_sha = "6" * 40
    c1_att = c1_dir / "attempts" / f"att-6-{c1_sha[:12]}"
    c1_att.mkdir(parents=True, exist_ok=True)
    (c1_att / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": c1_sha,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "CHANGES_REQUESTED",
                "blocking_open_count": 1,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )
    c2_pkt = tmp_path / "child_v2_packet.md"
    c2_pkt.write_text(
        "# Packet\n\n- **Root Task ID**: `ROOT`\n- **Packet Version**: `2`\n",
        encoding="utf-8",
    )

    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    rt_root = tmp_path / "rt_sib"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    out_c2 = repo_dir / "harness" / "runs" / "CHILD-V2" / "attempts" / f"r7-{sha1[:12]}"
    rc_c2 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "CHILD-V2",
            "--task-packet",
            str(c2_pkt),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_c2),
            "--backend",
            "codex",
        ]
    )
    assert rc_c2 == 2
    assert _count_calls(calls) == 0
    assert not out_c2.exists()
    root_tracker = json.loads(
        (repo_dir / "harness" / "runs" / "ROOT" / "g4-tracker.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(root_tracker["completed_attempts"]) == 6

    # Corrupt root g4-tracker.json fails closed (rc=2, no CLI calls).
    (repo_dir / "harness" / "runs" / "ROOT" / "g4-tracker.json").write_text(
        "{corrupt-json", encoding="utf-8"
    )
    out_bad = tmp_path / "out_corrupt_trk"
    rc_bad = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "CHILD-V2",
            "--task-packet",
            str(c2_pkt),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_bad),
            "--backend",
            "codex",
        ]
    )
    assert rc_bad == 2
    assert _count_calls(calls) == 0
    assert not out_bad.exists()


def test_g4_ledgerless_packet_epoch_cap_blocks_fourth_round_same_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledgerless G4 stops on 4th round for same packet.md and resets after packet bump."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_pkt_epoch", qualified_routes=[])
    pkt_path = repo_dir / "harness" / "runs" / "T-PKT-EP" / "packet.md"
    pkt_path.parent.mkdir(parents=True, exist_ok=True)
    pkt_path.write_text(
        "# Packet\n\n- **Root Task ID**: `T-PKT-EP`\n- **Packet Version**: `1`\n",
        encoding="utf-8",
    )
    rt_root = tmp_path / "rt_pkt_ep"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    (rt_root / "topstep-review-pass2").mkdir(parents=True)

    shas = [sha1]
    for idx in range(2, 6):
        (repo_dir / "app.py").write_text(f"x = {idx}\n", encoding="utf-8")
        shas.append(_commit_repo_change(repo_dir, f"c{idx}"))

    for idx in range(3):
        calls = _install_counting_codex_stub(tmp_path, monkeypatch, shas[idx])
        out_r = pkt_path.parent / "attempts" / f"r{idx + 1}-{shas[idx][:12]}"
        rc = codex_room_review.main(
            [
                "--commit",
                shas[idx],
                "--task-id",
                "T-PKT-EP",
                "--task-packet",
                str(pkt_path),
                "--runtime-root",
                str(rt_root),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_r),
                "--backend",
                "codex",
            ]
        )
        assert rc == 0
        assert _count_calls(calls) == 2

    calls4 = _install_counting_codex_stub(tmp_path, monkeypatch, shas[3])
    out_4 = pkt_path.parent / "attempts" / f"r4-{shas[3][:12]}"
    rc4 = codex_room_review.main(
        [
            "--commit",
            shas[3],
            "--task-id",
            "T-PKT-EP",
            "--task-packet",
            str(pkt_path),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_4),
            "--backend",
            "codex",
        ]
    )
    assert rc4 == 2
    assert _count_calls(calls4) == 0
    assert not out_4.exists()

    # Bump Packet Version -> new epoch allows round 4/6.
    pkt_path.write_text(
        "# Packet\n\n- **Root Task ID**: `T-PKT-EP`\n- **Packet Version**: `2`\n",
        encoding="utf-8",
    )
    calls5 = _install_counting_codex_stub(tmp_path, monkeypatch, shas[3])
    rc5 = codex_room_review.main(
        [
            "--commit",
            shas[3],
            "--task-id",
            "T-PKT-EP",
            "--task-packet",
            str(pkt_path),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_4),
            "--backend",
            "codex",
        ]
    )
    assert rc5 == 0
    assert _count_calls(calls5) == 2


def test_g4_preflight_blocks_when_spent_plus_reserved_exceeds_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 preflight blocks PAUSED_BUDGET when spent + active_reservations exceeds limit."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_preflight_res", qualified_routes=[])
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir,
        "T-PRE-BUD",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha1,
    )
    ledger = task_runner.load_ledger(ledger_path, repo_root=repo_dir)
    ledger["budget_limit_usd"] = 1.0
    ledger["spent_api_equiv_usd"] = 0.7
    ledger["active_reservations"] = {"peer-1": 0.4}
    task_runner.save_ledger(ledger_path, ledger)

    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    out_dir = repo_dir / "harness" / "runs" / "T-PRE-BUD" / "attempts" / f"r1-{sha1[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-PRE-BUD",
            "--ledger",
            str(ledger_path),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    assert _count_calls(calls) == 0
    assert not out_dir.exists()
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["state"] == "PAUSED_BUDGET"
    assert any(ev.get("kind") == "BUDGET_EXCEEDED_G4_PREFLIGHT" for ev in led_after["events"])


def _is_pid_alive(pid: int) -> bool:
    import ctypes

    if os.name == "nt":
        process_query_limited = 0x1000
        still_active = 259
        error_access_denied = 5
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_codex_5h_allowlist_strips_nested_oauth_cookies_and_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex-ratelimits-preflight.json allows only safe primitives and strips OAuth secrets."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_allowlist", qualified_routes=["gemini-4-carbon-high"]
    )
    rt_root = tmp_path / "rt_allowlist"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    fake_codex = tmp_path / "fake_codex_oauth_probe.py"
    codex_prefix = _write_fake_codex_script(
        fake_codex,
        (
            "import json, sys\n"
            "if sys.argv[1:2] == ['app-server']:\n"
            "    for raw in sys.stdin:\n"
            "        raw = raw.strip()\n"
            "        if not raw: continue\n"
            "        msg = json.loads(raw)\n"
            "        mid, method = msg.get('id'), msg.get('method')\n"
            "        if method == 'initialize':\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {}}), "
            "flush=True)\n"
            "        elif method == 'account/rateLimits/read':\n"
            "            res = {\n"
            "                'access_token': 'secret-top-at',\n"
            "                'cookies': 'secret-cookie-1',\n"
            "                'rateLimits': {\n"
            "                    'limitId': 'codex',\n"
            "                    'accountId': 'secret-acct',\n"
            "                    'accessToken': 'secret-camel-at',\n"
            "                    'refresh_token': 'secret-rt',\n"
            "                    'oauthToken': 'secret-oauth',\n"
            "                    'primary': {'usedPercent': 92.5, 'resetsAt': 1795651200, "
            "'window_minutes': 300, 'refreshToken': 'secret-nested-rt'},\n"
            "                    'secondary': {'usedPercent': 10.0, 'resetsAt': 1796000000},\n"
            "                    'credits': {'hasCredits': True, 'unlimited': False, "
            "'balance': '99', 'sessionKey': 'secret-cred'},\n"
            "                },\n"
            "            }\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': res}), "
            "flush=True)\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        ),
    )
    fake_agy = tmp_path / "fake_agy_allowlist.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_dir = tmp_path / "out_allowlist"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "auto",
        ]
    )
    assert rc == 0
    snap_raw = (out_dir / "codex-ratelimits-preflight.json").read_text(encoding="utf-8")
    assert "secret-" not in snap_raw
    for bad_key in (
        "access_token",
        "accessToken",
        "refresh_token",
        "refreshToken",
        "oauthToken",
        "cookies",
        "accountId",
        "sessionKey",
        "balance",
    ):
        assert bad_key not in snap_raw
    snap = json.loads(snap_raw)
    assert set(snap.keys()) == {
        "limitId",
        "planType",
        "ordinaryUsageAllowed",
        "spendControlReached",
        "rateLimitReachedType",
        "primary",
        "secondary",
        "credits",
    }
    assert set(snap["primary"].keys()) == {"usedPercent", "windowDurationMins", "resetsAt"}
    assert snap["primary"]["windowDurationMins"] == 300
    assert set(snap["credits"].keys()) == {"hasCredits", "unlimited"}
    _, _, snake_snap = codex_room_review.evaluate_codex_window_exhaustion(
        {
            "rate_limits": {
                "primary": {"used_percent": 50.0, "window_minutes": 300, "resets_at": 1795530291}
            }
        }
    )
    assert isinstance(snake_snap, dict)
    assert snake_snap["primary"]["windowDurationMins"] == 300


def test_g4_hybrid_packet_cap_blocks_when_ledger_packet_lower_than_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hybrid G4 blocks NEEDS_REPLAN when tracker packet rounds == 3 even if ledger has 0."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_hybrid_pkt", qualified_routes=[])
    pkt = repo_dir / "harness" / "runs" / "T-HYB-PKT" / "packet.md"
    pkt.parent.mkdir(parents=True, exist_ok=True)
    pkt.write_text("# Packet\n\n- **Root Task ID**: `T-HYB-PKT`\n- **Packet Version**: `1`\n")
    epoch = task_runner.extract_packet_epoch(pkt)
    assert epoch is not None

    trk_path = task_runner.default_g4_tracker_path("T-HYB-PKT", repo_root=repo_dir)
    trk = task_runner.load_g4_tracker(trk_path, task_id="T-HYB-PKT")
    trk["completed_rounds"] = 3
    trk["packet_rounds"][epoch] = 3
    task_runner.save_g4_tracker(trk_path, trk)

    (repo_dir / "app.py").write_text("x = 99\n", encoding="utf-8")
    sha4 = _commit_repo_change(repo_dir, "cand4")
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir,
        "T-HYB-PKT",
        state="REVIEW_FROZEN_G4",
        candidate_sha=sha4,
        g4_rounds_total=3,
        g4_rounds_in_packet=0,
    )
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha4)
    out_dir = pkt.parent / "attempts" / f"r4-{sha4[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha4,
            "--task-id",
            "T-HYB-PKT",
            "--task-packet",
            str(pkt),
            "--ledger",
            str(ledger_path),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    assert _count_calls(calls) == 0
    assert not out_dir.exists()
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["state"] == "NEEDS_REPLAN"
    assert any(ev.get("kind") == "G4_PACKET_CAP_REPLAN" for ev in led_after["events"])


def test_g4_counter_only_sha_invocations_sum_across_trackers_and_block_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct counter-only sha_invocations sum across root/sibling and stay idempotent."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_sha_gcounter", qualified_routes=[])
    root_trk_path = task_runner.default_g4_tracker_path("ROOT-SHA", repo_root=repo_dir)
    root_trk = task_runner.load_g4_tracker(root_trk_path, task_id="ROOT-SHA")
    root_trk["completed_rounds"] = 1
    root_trk["sha_invocations"][sha1] = 1
    task_runner.save_g4_tracker(root_trk_path, root_trk)

    c1_dir = repo_dir / "harness" / "runs" / "CHILD-S1"
    c1_dir.mkdir(parents=True, exist_ok=True)
    (c1_dir / "packet.md").write_text(
        "# P\n\n- **Root Task ID**: `ROOT-SHA`\n", encoding="utf-8"
    )
    c1_trk = task_runner.load_g4_tracker(c1_dir / "g4-tracker.json", task_id="CHILD-S1")
    c1_trk["completed_rounds"] = 1
    c1_trk["sha_invocations"][sha1] = 1
    task_runner.save_g4_tracker(c1_dir / "g4-tracker.json", c1_trk)

    resume_dir = tmp_path / "prior_infra"
    resume_dir.mkdir()
    (resume_dir / "consolidated-review.json").write_text(
        json.dumps({"revision_sha": sha1, "review_gate_status": "BLOCKED_INFRA"}),
        encoding="utf-8",
    )
    c2_pkt = tmp_path / "c2_sha.md"
    c2_pkt.write_text("# P\n\n- **Root Task ID**: `ROOT-SHA`\n", encoding="utf-8")

    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    for idx in (1, 2):
        out_dir = (
            repo_dir / "harness" / "runs" / "CHILD-S2" / "attempts" / f"r{idx}-{sha1[:12]}"
        )
        rc = codex_room_review.main(
            [
                "--commit",
                sha1,
                "--task-id",
                "CHILD-S2",
                "--task-packet",
                str(c2_pkt),
                "--resume-from",
                str(resume_dir),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_dir),
                "--backend",
                "codex",
            ]
        )
        assert rc == 2
        assert _count_calls(calls) == 0
        assert not out_dir.exists()

    persisted = json.loads(root_trk_path.read_text(encoding="utf-8"))
    assert persisted["sha_invocations"][sha1] == 2
    assert persisted["completed_rounds"] == 2


def test_g4_valid_json_wrong_field_type_tracker_fails_closed_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid-JSON tracker with malformed field types blocks G4 before creating artifacts."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_bad_type_trk", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    root_trk = repo_dir / "harness" / "runs" / "ROOT-BT" / "g4-tracker.json"
    root_trk.parent.mkdir(parents=True, exist_ok=True)
    root_trk.write_text(
        json.dumps({"schema_version": 1, "task_id": "ROOT-BT", "in_flight": ["x"]}),
        encoding="utf-8",
    )
    out1 = repo_dir / "harness" / "runs" / "ROOT-BT" / "attempts" / f"r1-{sha1[:12]}"
    rc1 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "ROOT-BT",
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out1),
            "--backend",
            "codex",
        ]
    )
    assert rc1 == 2
    assert _count_calls(calls) == 0
    assert not out1.exists()

    # Valid root tracker + wrong-type sibling tracker also fails closed.
    root_trk.write_text(
        json.dumps({"schema_version": 1, "task_id": "ROOT-BT", "completed_rounds": 0}),
        encoding="utf-8",
    )
    sib_dir = repo_dir / "harness" / "runs" / "SIB-BT"
    sib_dir.mkdir(parents=True, exist_ok=True)
    (sib_dir / "packet.md").write_text("# P\n\n- **Root Task ID**: `ROOT-BT`\n")
    (sib_dir / "g4-tracker.json").write_text(
        json.dumps({"schema_version": 1, "task_id": "SIB-BT", "packet_rounds": []}),
        encoding="utf-8",
    )
    child_pkt = tmp_path / "child_bt.md"
    child_pkt.write_text("# P\n\n- **Root Task ID**: `ROOT-BT`\n")
    out2 = repo_dir / "harness" / "runs" / "CHILD-BT" / "attempts" / f"r1-{sha1[:12]}"
    rc2 = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "CHILD-BT",
            "--task-packet",
            str(child_pkt),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out2),
            "--backend",
            "codex",
        ]
    )
    assert rc2 == 2
    assert _count_calls(calls) == 0
    assert not out2.exists()


def test_g4_child_in_flight_does_not_persist_zombie_lock_in_root_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting a sibling while CHILD-V1 is in_flight must not leave a zombie lock in ROOT."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_zombie_lock", qualified_routes=[])
    rt_root = tmp_path / "rt_zombie"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    (rt_root / "topstep-review-pass2").mkdir(parents=True)

    c1_dir = repo_dir / "harness" / "runs" / "CHILD-V1"
    c1_dir.mkdir(parents=True, exist_ok=True)
    (c1_dir / "packet.md").write_text(
        "# P\n\n- **Root Task ID**: `ROOT-ZF`\n", encoding="utf-8"
    )
    c1_trk_path = c1_dir / "g4-tracker.json"
    c1_trk = task_runner.load_g4_tracker(c1_trk_path, task_id="CHILD-V1")
    c1_trk["in_flight"]["live-c1"] = {"candidate_sha": sha1, "out_dir": "custom_c1_out"}
    task_runner.save_g4_tracker(c1_trk_path, c1_trk)

    c2_pkt = tmp_path / "c2_zf.md"
    c2_pkt.write_text("# P\n\n- **Root Task ID**: `ROOT-ZF`\n", encoding="utf-8")
    (repo_dir / "app.py").write_text("x = 2\n", encoding="utf-8")
    sha2 = _commit_repo_change(repo_dir, "cand2")

    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha2)
    out_c2 = repo_dir / "harness" / "runs" / "CHILD-V2" / "attempts" / f"r1-{sha2[:12]}"
    rc_blocked = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "CHILD-V2",
            "--task-packet",
            str(c2_pkt),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_c2),
            "--backend",
            "codex",
        ]
    )
    assert rc_blocked == 2
    assert _count_calls(calls) == 0
    assert not out_c2.exists()
    root_trk_path = task_runner.default_g4_tracker_path("ROOT-ZF", repo_root=repo_dir)
    if root_trk_path.is_file():
        assert json.loads(root_trk_path.read_text(encoding="utf-8")).get("in_flight") == {}

    # Simulate CHILD-V1 finishing and clearing its own in_flight slot; CHILD-V2 must succeed.
    c1_trk["in_flight"] = {}
    task_runner.save_g4_tracker(c1_trk_path, c1_trk)
    rc_ok = codex_room_review.main(
        [
            "--commit",
            sha2,
            "--task-id",
            "CHILD-V2",
            "--task-packet",
            str(c2_pkt),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_c2),
            "--backend",
            "codex",
        ]
    )
    assert rc_ok == 0
    assert _count_calls(calls) == 2
    assert json.loads(root_trk_path.read_text(encoding="utf-8")).get("in_flight") == {}


def test_g4_counter_only_sha_plus_fs_attempt_stays_blocked_across_two_preflights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-only SHA residual + 1 FS attempt stays sha_count=2 across repeated preflights."""
    from harness.scripts import task_runner

    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_mix_sha", qualified_routes=[])
    trk_path = task_runner.default_g4_tracker_path("T-MIX-SHA", repo_root=repo_dir)
    trk = task_runner.load_g4_tracker(trk_path, task_id="T-MIX-SHA")
    trk["completed_rounds"] = 1
    trk["sha_invocations"][sha1] = 1
    task_runner.save_g4_tracker(trk_path, trk)

    r1_dir = repo_dir / "harness" / "runs" / "T-MIX-SHA" / "attempts" / f"r1-{sha1[:12]}"
    r1_dir.mkdir(parents=True, exist_ok=True)
    (r1_dir / "consolidated-review.json").write_text(
        json.dumps({"revision_sha": sha1, "review_gate_status": "BLOCKED_INFRA"}),
        encoding="utf-8",
    )

    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    for idx in (2, 3):
        out_dir = (
            repo_dir / "harness" / "runs" / "T-MIX-SHA" / "attempts" / f"r{idx}-{sha1[:12]}"
        )
        rc = codex_room_review.main(
            [
                "--commit",
                sha1,
                "--task-id",
                "T-MIX-SHA",
                "--resume-from",
                str(r1_dir),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_dir),
                "--backend",
                "codex",
            ]
        )
        assert rc == 2
        assert _count_calls(calls) == 0
        assert not out_dir.exists()

    persisted = json.loads(trk_path.read_text(encoding="utf-8"))
    assert persisted["sha_invocations"][sha1] == 2
    assert persisted["imported_legacy_sha"]["T-MIX-SHA"][sha1] == 1


def test_g4_malformed_ledger_map_types_fail_closed_without_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong-typed ledger.json map fields fail closed in G4 preflight before spawning CLI."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_bad_led_map", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    for idx, bad_field in enumerate(
        (
            {"active_g4_reservations": ["live"]},
            {"g4_invocations_by_sha": []},
        ),
        start=1,
    ):
        task_id = f"T-BAD-LED-{idx}"
        ledger_path = _setup_ledger_for_g4_test(
            repo_dir, task_id, state="REVIEW_FROZEN_G4", candidate_sha=sha1
        )
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        raw.update(bad_field)
        ledger_path.write_text(json.dumps(raw), encoding="utf-8")

        out_dir = repo_dir / "harness" / "runs" / task_id / "attempts" / f"r1-{sha1[:12]}"
        rc = codex_room_review.main(
            [
                "--commit",
                sha1,
                "--task-id",
                task_id,
                "--ledger",
                str(ledger_path),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_dir),
                "--backend",
                "codex",
            ]
        )
        assert rc == 2
        assert _count_calls(calls) == 0
        assert not out_dir.exists()


def test_g4_mid_run_tracker_corruption_bills_tokens_and_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-run corrupt g4-tracker.json still bills token usage and persists BLOCKED."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_mid_corr", qualified_routes=[])
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-MID-CORR", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    trk_path = repo_dir / "harness" / "runs" / "T-MID-CORR" / "g4-tracker.json"
    fake_codex = tmp_path / "fake_codex_corrupt_trk.py"
    prefix = _write_fake_codex_script(
        fake_codex,
        (
            "import json, pathlib, sys\n"
            "if sys.argv[1:2] == ['app-server']: sys.exit(1)\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "prompt = sys.stdin.read()\n"
            "is_p2 = 'security_and_invariants' in prompt\n"
            "lens = 'security_and_invariants' if is_p2 else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 10000, 'cached_input_tokens': 2000, 'output_tokens': 1500}}), "
            "flush=True)\n"
            "if is_p2:\n"
            f"    pathlib.Path({str(trk_path)!r}).write_text('{{corrupt', encoding='utf-8')\n"
        ),
    )
    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: prefix)

    out_dir = repo_dir / "harness" / "runs" / "T-MID-CORR" / "attempts" / f"r1-{sha1[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-MID-CORR",
            "--ledger",
            str(ledger_path),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "codex",
        ]
    )
    assert rc == 2
    rep = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep["review_gate_status"] == "BLOCKED"
    assert any("Corrupt g4-tracker.json" in err for err in rep["errors"])
    led_after = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led_after["state"] == "BLOCKED"
    assert led_after["spent_api_equiv_usd"] > 0.0
    assert any(ev.get("kind") == "G4_TRACKER_CORRUPT_BLOCKED" for ev in led_after["events"])


def test_codex_window_allowlist_coerces_wrong_typed_allowlisted_fields() -> None:
    """Wrong-typed values inside allowlisted fields coerce cleanly to defaults/None."""
    exhausted, reason, snap = codex_room_review.evaluate_codex_window_exhaustion(
        {
            "rateLimits": {
                "limitId": 123,
                "planType": ["pro"],
                "ordinaryUsageAllowed": "yes",
                "spendControlReached": {"flag": True},
                "rateLimitReachedType": 42,
                "primary": {
                    "usedPercent": True,
                    "windowDurationMins": True,
                    "resetsAt": "soon",
                },
                "secondary": ["bad"],
                "credits": {"hasCredits": "true", "unlimited": 1},
            }
        }
    )
    assert exhausted is False
    assert reason is None
    assert snap == {
        "limitId": "codex",
        "planType": None,
        "ordinaryUsageAllowed": None,
        "spendControlReached": None,
        "rateLimitReachedType": None,
        "primary": {"usedPercent": None, "windowDurationMins": None, "resetsAt": None},
        "secondary": None,
        "credits": {"hasCredits": None, "unlimited": None},
    }


def test_agy_auto_fallback_serializes_passes_while_codex_stays_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy fallback serializes passes by default while codex runs in parallel by default."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_agy_seq", qualified_routes=["gemini-4-carbon-high"]
    )
    rt_root = tmp_path / "rt_agy_seq"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    active_dir = tmp_path / "active_agy"
    active_dir.mkdir()
    pct_file = tmp_path / "pct_seq.txt"
    pct_file.write_text("94.0", encoding="utf-8")

    fake_codex = tmp_path / "fake_codex_seq.py"
    codex_prefix = _write_fake_codex_script(
        fake_codex,
        (
            "import json, pathlib, sys\n"
            f"pct_p = pathlib.Path({str(pct_file)!r})\n"
            "if sys.argv[1:2] == ['app-server']:\n"
            "    pct = float(pct_p.read_text(encoding='utf-8').strip())\n"
            "    for raw in sys.stdin:\n"
            "        raw = raw.strip()\n"
            "        if not raw: continue\n"
            "        msg = json.loads(raw)\n"
            "        mid, method = msg.get('id'), msg.get('method')\n"
            "        if method == 'initialize':\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {}}), "
            "flush=True)\n"
            "        elif method == 'account/rateLimits/read':\n"
            "            res = {'rateLimits': {'primary': {'usedPercent': pct}}}\n"
            "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': res}), "
            "flush=True)\n"
            "    sys.exit(0)\n"
            "args = sys.argv[1:]\n"
            "out_path = pathlib.Path(args[args.index('-o') + 1])\n"
            "lens = 'security_and_invariants' if out_path.name == 'pass2.json' "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'codex ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "out_path.write_text(json.dumps(payload), encoding='utf-8')\n"
        ),
    )

    max_file = tmp_path / "max_active.txt"
    fake_agy = tmp_path / "fake_agy_seq.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, pathlib, sys, time\n"
            f"ad = pathlib.Path({str(active_dir)!r})\n"
            f"mf = pathlib.Path({str(max_file)!r})\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            "lock = ad / f'{lens}.active'\n"
            "lock.write_text('1', encoding='utf-8')\n"
            "try:\n"
            "    cur = len(list(ad.glob('*.active')))\n"
            "    prev = int(mf.read_text(encoding='utf-8')) if mf.is_file() else 0\n"
            "    mf.write_text(str(max(prev, cur)), encoding='utf-8')\n"
            "    time.sleep(0.08)\n"
            "finally:\n"
            "    lock.unlink(missing_ok=True)\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )

    seq_calls: list[bool] = []
    orig_exec = codex_room_review._execute_two_passes

    def _spy_exec(fn, k1, k2, *, sequential=False):
        seq_calls.append(bool(sequential))
        return orig_exec(fn, k1, k2, sequential=sequential)

    monkeypatch.setattr(
        codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
    )
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_prefix)
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)
    monkeypatch.setattr(codex_room_review, "_execute_two_passes", _spy_exec)

    rc_auto = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(tmp_path / "out_auto_seq"),
            "--backend",
            "auto",
        ]
    )
    assert rc_auto == 0
    assert int(max_file.read_text(encoding="utf-8")) == 1
    assert seq_calls[-1] is True

    pct_file.write_text("10.0", encoding="utf-8")
    rc_codex = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(tmp_path / "out_codex_par"),
            "--backend",
            "codex",
        ]
    )
    assert rc_codex == 0
    assert seq_calls[-1] is False


def test_agy_plain_text_stderr_zero_token_startup_race_retries_archives_and_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain-text 0-token agy stderr race retries once, archives stderr, or BLOCKED_INFRA."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_agy_stderr_race", qualified_routes=["gemini-4-carbon-high"]
    )
    fail_forever = tmp_path / "fail_forever.flag"
    calls_dir = tmp_path / "agy_stderr_calls"
    calls_dir.mkdir()
    fake_agy = tmp_path / "fake_agy_stderr_race.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, pathlib, sys\n"
            f"cd = pathlib.Path({str(calls_dir)!r})\n"
            f"ff = pathlib.Path({str(fail_forever)!r})\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            "lf = cd / f'{lens}.log'\n"
            "prev = lf.read_text(encoding='utf-8') if lf.is_file() else ''\n"
            "seen = len(prev.splitlines())\n"
            "lf.write_text(prev + 'call\\n', encoding='utf-8')\n"
            "if ff.is_file() or (lens == 'correctness_and_tests' and seen == 0):\n"
            "    sys.stderr.write("
            "'error: failed to start conversation: backend returned an empty conversation id\\n')\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', "
            "'result': {'status': 'SUCCESS', 'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    out_ok = tmp_path / "out_stderr_recover"
    rc_ok = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_ok),
            "--backend",
            "agy",
        ]
    )
    assert rc_ok == 0
    retry_logs = list(out_ok.glob("pass1.*retry0*.stderr.log"))
    assert len(retry_logs) == 1
    assert "empty conversation id" in retry_logs[0].read_text(encoding="utf-8")

    fail_forever.write_text("1", encoding="utf-8")
    (calls_dir / "correctness_and_tests.log").unlink()
    out_inf = tmp_path / "out_stderr_blocked_infra"
    rc_inf = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_inf),
            "--backend",
            "agy",
        ]
    )
    assert rc_inf == 2
    rep_inf = json.loads((out_inf / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep_inf["review_gate_status"] == "BLOCKED_INFRA"
    assert len(list(out_inf.glob("pass1.*retry0*.stderr.log"))) == 1
    p1_lines = (calls_dir / "correctness_and_tests.log").read_text(encoding="utf-8").splitlines()
    assert len(p1_lines) == 2


def test_classify_agy_failure_rejects_nonzero_or_bool_tokens_with_startup_stderr(
    tmp_path: Path,
) -> None:
    """Non-zero or bool token counts with startup stderr classify as SCHEMA_OR_POLICY."""
    stderr_msg = (
        "error: failed to start conversation: backend returned an empty conversation id\n"
    )
    for idx, raw_content in enumerate(
        (
            json.dumps({"usage": {"input_tokens": 12}}) + "\n",
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "failed to start conversation",
                        "usage": {"total_tokens": 42},
                    },
                }
            )
            + "\n",
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "failed to start conversation",
                        "usage": {"total_tokens": False},
                    },
                }
            )
            + "\n",
            json.dumps({"usage": {"input_tokens": "invalid"}}) + "\n",
            json.dumps({"usage": {"cache_read_tokens": {}, "output_tokens": []}}) + "\n",
        ),
        start=1,
    ):
        d = tmp_path / f"neg_tok_{idx}"
        d.mkdir()
        (d / "pass1.raw.jsonl").write_text(raw_content, encoding="utf-8")
        (d / "pass1.stderr.log").write_text(stderr_msg, encoding="utf-8")
        failure = codex_room_review.classify_agy_failure(d, "pass1", returncode=1)
        assert failure.category == codex_room_review.OUTCOME_SCHEMA_OR_POLICY

    # Structured JSON error on stderr with 0 tokens still triggers OUTCOME_TRANSIENT.
    d_json_err = tmp_path / "json_stderr_startup"
    d_json_err.mkdir()
    (d_json_err / "pass1.raw.jsonl").write_text("", encoding="utf-8")
    (d_json_err / "pass1.stderr.log").write_text(
        json.dumps(
            {
                "error_code": 500,
                "short_error": "failed to start conversation: empty conversation id",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    f_json = codex_room_review.classify_agy_failure(d_json_err, "pass1", returncode=1)
    assert f_json.category == codex_room_review.OUTCOME_TRANSIENT


def test_g4_rejects_malformed_circuit_breakers_and_missing_spent_without_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed ledger circuit_breakers or missing spent_api_equiv_usd fails closed preflight."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_bad_cb", qualified_routes=[])
    calls = _install_counting_codex_stub(tmp_path, monkeypatch, sha1)
    bad_cases = (
        {"circuit_breakers": {"max_g4_rounds_per_root_task": "100"}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": True}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": None}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": 0}},
        {"__drop_spent__": True},
    )
    for idx, case in enumerate(bad_cases, start=1):
        task_id = f"T-BAD-CB-{idx}"
        ledger_path = _setup_ledger_for_g4_test(
            repo_dir, task_id, state="REVIEW_FROZEN_G4", candidate_sha=sha1
        )
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        if case.get("__drop_spent__"):
            raw.pop("spent_api_equiv_usd", None)
        else:
            raw.update(case)
        ledger_path.write_text(json.dumps(raw), encoding="utf-8")

        out_dir = repo_dir / "harness" / "runs" / task_id / "attempts" / f"r1-{sha1[:12]}"
        rc = codex_room_review.main(
            [
                "--commit",
                sha1,
                "--task-id",
                task_id,
                "--ledger",
                str(ledger_path),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_dir),
                "--backend",
                "codex",
            ]
        )
        assert rc == 2
        assert _count_calls(calls) == 0
        assert not (out_dir / "frozen").exists()
        assert not out_dir.exists()


def test_g4_agy_malformed_pass1_usage_classifies_blocked_and_bills_pass2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed usage in pass1 yields BLOCKED report without crashing and still bills pass2."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_malformed_usage", qualified_routes=["gemini-4-carbon-high"]
    )
    ledger_path = _setup_ledger_for_g4_test(
        repo_dir, "T-MAL-USAGE", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    fake_agy = tmp_path / "fake_agy_mal_usage.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "is_p2 = 'security_and_invariants' in prompt\n"
            "if not is_p2:\n"
            "    print(json.dumps({'usage': {'input_tokens': 'invalid', 'output_tokens': {}}}), "
            "flush=True)\n"
            "    sys.stderr.write('error: failed to start conversation\\n')\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': 'security_and_invariants', "
            "'verdict': 'APPROVE', 'summary': 'p2 ok', 'findings': [], "
            "'test_quality_issues': [], 'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', "
            "'model': 'gemini-4-carbon-high', "
            "'usage': {'input_tokens': 5000, 'output_tokens': 1000}, "
            "'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)
    out_dir = repo_dir / "harness" / "runs" / "T-MAL-USAGE" / "attempts" / f"r1-{sha1[:12]}"
    rc = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-MAL-USAGE",
            "--ledger",
            str(ledger_path),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_dir),
            "--backend",
            "agy",
        ]
    )
    assert rc == 2
    rep = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep["review_gate_status"] == "BLOCKED"
    led = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert led["state"] == "BLOCKED"
    assert led["spent_api_equiv_usd"] > 0.0


def test_g4_agy_alias_model_bills_via_route_and_unpriceable_model_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown AGY event model alias falls back to route pricing; unpriceable usage blocks."""
    repo_dir, sha1 = _init_policy_repo(
        tmp_path / "repo_agy_alias_bill", qualified_routes=["gemini-4-carbon-high"]
    )
    fake_agy = tmp_path / "fake_agy_alias.py"
    agy_prefix = _write_fake_codex_script(
        fake_agy,
        (
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"payload = {{'revision_sha': {sha1!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', "
            "'model': 'agy-custom-alias', "
            "'usage': {'input_tokens': 4000, 'output_tokens': 800}, "
            "'structured_output': payload}}), flush=True)\n"
        ),
    )
    monkeypatch.setattr(codex_room_review, "resolve_agy_command", lambda: agy_prefix)

    led_ok = _setup_ledger_for_g4_test(
        repo_dir, "T-AGY-ALIAS-OK", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    out_ok = repo_dir / "harness" / "runs" / "T-AGY-ALIAS-OK" / "attempts" / f"r1-{sha1[:12]}"
    rc_ok = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-AGY-ALIAS-OK",
            "--ledger",
            str(led_ok),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_ok),
            "--backend",
            "agy",
        ]
    )
    assert rc_ok == 0
    assert json.loads(led_ok.read_text(encoding="utf-8"))["spent_api_equiv_usd"] > 0.0

    # Unpriceable event + route model fails closed (rc=2, BLOCKED).
    led_bad = _setup_ledger_for_g4_test(
        repo_dir, "T-AGY-UNPRICED", state="REVIEW_FROZEN_G4", candidate_sha=sha1
    )
    out_bad = repo_dir / "harness" / "runs" / "T-AGY-UNPRICED" / "attempts" / f"r1-{sha1[:12]}"
    rc_bad = codex_room_review.main(
        [
            "--commit",
            sha1,
            "--task-id",
            "T-AGY-UNPRICED",
            "--ledger",
            str(led_bad),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_bad),
            "--backend",
            "agy",
            "--fallback-model",
            "unpriced-route-model",
        ]
    )
    assert rc_bad == 2
    rep_bad = json.loads((out_bad / "consolidated-review.json").read_text(encoding="utf-8"))
    assert rep_bad["review_gate_status"] == "BLOCKED"
    assert json.loads(led_bad.read_text(encoding="utf-8"))["state"] == "BLOCKED"


def test_g4_ledgerless_edited_packet_same_version_stays_blocked_until_version_bumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing packet.md body/whitespace without bumping Packet Version still blocks round 4."""
    repo_dir, sha1 = _init_policy_repo(tmp_path / "repo_pkt_bucket", qualified_routes=[])
    pkt_path = repo_dir / "harness" / "runs" / "T-PKT-BUCKET" / "packet.md"
    pkt_path.parent.mkdir(parents=True, exist_ok=True)
    pkt_path.write_text(
        "# Packet\n\n- **Root Task ID**: `T-PKT-BUCKET`\n- **Packet Version**: `1`\nBody A\n",
        encoding="utf-8",
    )
    rt_root = tmp_path / "rt_pkt_bkt"
    (rt_root / "topstep-review-pass1").mkdir(parents=True)
    (rt_root / "topstep-review-pass2").mkdir(parents=True)

    shas = [sha1]
    for idx in range(2, 6):
        (repo_dir / "app.py").write_text(f"x = {idx}\n", encoding="utf-8")
        shas.append(_commit_repo_change(repo_dir, f"c{idx}"))

    for idx in range(3):
        calls = _install_counting_codex_stub(tmp_path, monkeypatch, shas[idx])
        out_r = pkt_path.parent / "attempts" / f"r{idx + 1}-{shas[idx][:12]}"
        rc = codex_room_review.main(
            [
                "--commit",
                shas[idx],
                "--task-id",
                "T-PKT-BUCKET",
                "--task-packet",
                str(pkt_path),
                "--runtime-root",
                str(rt_root),
                "--repo-root",
                str(repo_dir),
                "--out-dir",
                str(out_r),
                "--backend",
                "codex",
            ]
        )
        assert rc == 0
        assert _count_calls(calls) == 2

    # Edit packet body while keeping Packet Version: 1 -> must STILL block 4th round!
    pkt_path.write_text(
        "# Packet\n\n- **Root Task ID**: `T-PKT-BUCKET`\n- **Packet Version**: `1`\nBody B\n\n",
        encoding="utf-8",
    )
    calls4 = _install_counting_codex_stub(tmp_path, monkeypatch, shas[3])
    out_4 = pkt_path.parent / "attempts" / f"r4-{shas[3][:12]}"
    rc4 = codex_room_review.main(
        [
            "--commit",
            shas[3],
            "--task-id",
            "T-PKT-BUCKET",
            "--task-packet",
            str(pkt_path),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_4),
            "--backend",
            "codex",
        ]
    )
    assert rc4 == 2
    assert _count_calls(calls4) == 0
    assert not out_4.exists()

    # Bump Packet Version: 2 -> opens new bucket and allows round 4.
    pkt_path.write_text(
        "# Packet\n\n- **Root Task ID**: `T-PKT-BUCKET`\n- **Packet Version**: `2`\nBody B\n",
        encoding="utf-8",
    )
    calls5 = _install_counting_codex_stub(tmp_path, monkeypatch, shas[3])
    rc5 = codex_room_review.main(
        [
            "--commit",
            shas[3],
            "--task-id",
            "T-PKT-BUCKET",
            "--task-packet",
            str(pkt_path),
            "--runtime-root",
            str(rt_root),
            "--repo-root",
            str(repo_dir),
            "--out-dir",
            str(out_4),
            "--backend",
            "codex",
        ]
    )
    assert rc5 == 0
    assert _count_calls(calls5) == 2

















