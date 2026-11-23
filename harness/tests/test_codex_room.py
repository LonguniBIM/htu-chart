"""Tests for Codex Room runtime sync isolation and 2-pass review consolidation."""

from __future__ import annotations

import hashlib
import json
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
    assert args.fallback_model == DEFAULT_AGY_FALLBACK_MODEL


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
    head_sha = codex_room_review.resolve_commit_info(REPO_ROOT, "HEAD", None)["candidate_sha"]
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
            "import sys\n"
            "_ = sys.stdin.read()\n"
            "sys.stderr.write('Error: usage_limit_reached - Codex CLI quota exhausted\\n')\n"
            "sys.exit(1)\n"
        ),
    )

    codex_crash_script = tmp_path / "fake_codex_crash.py"
    codex_crash_prefix = _write_fake_codex_script(
        codex_crash_script,
        (
            "import sys\n"
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
    rc_a = codex_room_review.main(["--commit", "HEAD", "--out-dir", str(out_a), "--backend", "auto"])
    assert rc_a == 0
    report_a = json.loads((out_a / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_a["review_backend"] == "codex"
    assert report_a["fallback_used"] is False
    assert not agy_called.exists()

    # Case B: Codex CLI hits quota limit -> auto falls back to agy Opus 5.5 and archives failed Codex logs.
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_quota_prefix)
    out_b = tmp_path / "out_auto_fallback"
    rc_b = codex_room_review.main(["--commit", "HEAD", "--out-dir", str(out_b), "--backend", "auto"])
    assert rc_b == 0
    assert agy_called.exists()
    assert (out_b / "pass1.codex-failed.stderr.log").is_file()
    report_b = json.loads((out_b / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report_b["review_gate_status"] == "PASSED"
    assert report_b["review_backend"] == "agy"
    assert report_b["review_model"] == DEFAULT_AGY_FALLBACK_MODEL
    assert report_b["fallback_used"] is True
    assert "usage_limit_reached" in str(report_b["fallback_reason"])

    # Case C: --backend codex never falls back even on quota exhaustion.
    agy_called.unlink()
    out_c = tmp_path / "out_strict_codex"
    rc_c = codex_room_review.main(["--commit", "HEAD", "--out-dir", str(out_c), "--backend", "codex"])
    assert rc_c == 2
    assert not agy_called.exists()
    assert _detect_codex_quota_error(out_c) is not None

    # Case D: --backend auto does NOT silently fall back on non-quota Codex failure.
    monkeypatch.setattr(codex_room_review, "resolve_codex_command", lambda: codex_crash_prefix)
    out_d = tmp_path / "out_non_quota_fail"
    rc_d = codex_room_review.main(["--commit", "HEAD", "--out-dir", str(out_d), "--backend", "auto"])
    assert rc_d == 2
    assert not agy_called.exists()


def test_codex_partial_blocking_finding_preserved_across_agy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When one Codex pass returns a blocking finding and the other hits quota, fallback must not erase it."""
    head_sha = codex_room_review.resolve_commit_info(REPO_ROOT, "HEAD", None)["candidate_sha"]

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
            "    sys.stderr.write('Error: usage_limit_reached - Codex CLI quota exhausted\\n')\n"
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
        ["--commit", "HEAD", "--out-dir", str(out_dir), "--backend", "auto", "--fail-on-blocking"]
    )
    assert rc == 1
    report = json.loads((out_dir / "consolidated-review.json").read_text(encoding="utf-8"))
    assert report["review_gate_status"] == "CHANGES_REQUESTED"
    assert report["blocking_open_count"] == 1
    assert any(f["id"] == "C-1" and f["status"] == "OPEN" for f in report["consolidated_findings"])
    assert "codex tqi" in report["test_quality_issues"]
