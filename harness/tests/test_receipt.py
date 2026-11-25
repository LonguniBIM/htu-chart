"""Tests for receipt.py gate adjudication (VERIFIED / FAILED / BLOCKED)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.scripts.receipt import evaluate_receipt, render_receipt_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]


def _verifier(sha: str, status: str = "VERIFIED", phase: int = 1, frozen: bool = False) -> dict:
    step_ok = status == "VERIFIED"
    step_status = "PASSED" if step_ok else "FAILED"
    step_exit = 0 if step_ok else 1
    steps = [
        {"name": "arch_check", "command": ["python", "harness/scripts/arch_check.py"], "status": step_status, "exit_code": step_exit},
        {"name": "harness_ruff", "command": ["ruff", "check", "harness"], "status": step_status, "exit_code": step_exit},
        {"name": "harness_pytest", "command": ["pytest", "harness/tests", "-q"], "status": step_status, "exit_code": step_exit},
    ]
    if frozen:
        steps.append({"name": "upstream_fork_clean", "command": ["git", "status"], "status": "PASSED", "exit_code": 0})
    if phase >= 1:
        steps.extend(
            [
                {"name": "backend_ruff", "command": ["ruff", "check", "backend"], "status": step_status, "exit_code": step_exit},
                {"name": "backend_pytest", "command": ["pytest", "backend/tests", "-q"], "status": step_status, "exit_code": step_exit},
            ]
        )
    if phase >= 3:
        steps.extend(
            [
                {"name": "frontend_typecheck", "command": ["pnpm", "--dir", "frontend", "typecheck"], "status": step_status, "exit_code": step_exit},
                {"name": "frontend_build", "command": ["pnpm", "--dir", "frontend", "build"], "status": step_status, "exit_code": step_exit},
            ]
        )
    payload: dict = {
        "status": status,
        "phase": phase,
        "git_sha": sha,
        "worktree_dirty": False,
        "steps": steps,
    }
    if frozen:
        payload["source_mode"] = "frozen_commit_archive"
    return payload


def _review(sha: str, passes: int = 2, independent: bool = True, findings: list[dict] | None = None) -> dict:
    return {
        "revision_sha": sha,
        "passes_completed": passes,
        "independent_contexts": independent,
        "review_gate_status": "CHANGES_REQUESTED" if findings else "PASSED",
        "consolidated_findings": findings or [],
    }


def test_bugfix_without_valid_red_proof_is_blocked() -> None:
    base = "1" * 40
    cand = "2" * 40

    missing = evaluate_receipt(
        task_id="BUG-01",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof=None,
    )
    assert missing["conclusion"] == "BLOCKED"

    import_err = evaluate_receipt(
        task_id="BUG-01",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "ModuleNotFoundError: No module named 'app'",
            "green_sha": cand,
            "green_exit_code": 0,
        },
    )
    assert import_err["conclusion"] == "BLOCKED"


def test_single_pass_or_stale_sha_review_is_blocked() -> None:
    base = "1" * 40
    cand = "2" * 40
    stale = "3" * 40

    one_pass = evaluate_receipt(
        task_id="P1-01",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand, passes=1),
    )
    assert one_pass["conclusion"] == "BLOCKED"

    stale_sha = evaluate_receipt(
        task_id="P1-01",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(stale, passes=2),
    )
    assert stale_sha["conclusion"] == "BLOCKED"


def test_open_blocking_finding_or_unreviewed_fix_claim_fails() -> None:
    base = "1" * 40
    cand = "2" * 40
    finding = {"id": "P2-01", "severity": "high", "blocking": True, "title": "Bypassable CSRF"}

    open_res = evaluate_receipt(
        task_id="P1-02",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand, findings=[finding]),
    )
    assert open_res["conclusion"] == "FAILED"

    claimed_fixed = evaluate_receipt(
        task_id="P1-02",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand, findings=[finding]),
        dispositions={"dispositions": [{"id": "P2-01", "status": "FIXED", "rationale": "fixed"}]},
    )
    assert claimed_fixed["conclusion"] == "FAILED"


def test_verified_when_all_gates_pass_and_cli_writes_markdown_table(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sec_file = repo / "backend" / "app" / "security.py"
    sec_file.parent.mkdir(parents=True, exist_ok=True)
    sec_file.write_text("\n".join(f"# line {i}" for i in range(1, 61)) + "\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    sec_file.write_text("\n".join(f"# cand line {i}" for i in range(1, 61)) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "verifier.json"
    r_path = tmp_path / "review.json"
    red_path = tmp_path / "red.json"
    disp_path = tmp_path / "disp.json"
    out_dir = tmp_path / "out"

    v_path.write_text(json.dumps(_verifier(cand)), encoding="utf-8")
    r_path.write_text(
        json.dumps(
            _review(
                cand,
                findings=[{"id": "P1-99", "severity": "medium", "blocking": True, "title": "False positive"}],
            )
        ),
        encoding="utf-8",
    )
    red_path.write_text(
        json.dumps(
            {
                "red_sha": base,
                "red_exit_code": 1,
                "red_failure_reason": "AssertionError: expected 403 on missing CSRF token, got 200",
                "green_sha": cand,
                "green_exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    disp_path.write_text(
        json.dumps(
            {
                "dispositions": [
                    {
                        "id": "P1-99",
                        "status": "REFUTED_WITH_EVIDENCE",
                        "rationale": "GET route is read-only; CSRF only applies to mutations.",
                        "evidence_ref": "backend/app/security.py:44",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "BUG-42",
            "--phase",
            "1",
            "--base-sha",
            base,
            "--candidate-sha",
            cand,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--bugfix",
            "--red-proof-json",
            str(red_path),
            "--dispositions-json",
            str(disp_path),
            "--repo-root",
            str(repo),
            "--behavior",
            "Reject mutating requests without valid X-CSRF-Token.",
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    receipt_json = json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt_json["conclusion"] == "VERIFIED"
    assert len(receipt_json["artifact_hashes"]["verifier_json_sha256"]) == 64

    md_text = (out_dir / "receipt.md").read_text(encoding="utf-8")
    for required_row in (
        "Hành vi được bảo vệ",
        "RED trước sửa",
        "GREEN sau sửa",
        "Kiểm tra liên quan",
        "Review gate",
        "Verifier gate",
        "Kết luận",
    ):
        assert required_row in md_text


def test_receipt_records_agy_fallback_provenance_in_json_and_markdown() -> None:
    base = "1" * 40
    cand = "2" * 40
    review = _review(cand)
    review.update(
        {
            "review_backend": "agy",
            "review_model": "claude-opus-5-5-medium",
            "fallback_used": True,
            "fallback_reason": "codex_quota_exhausted (pass1.stderr.log: matched 'usage_limit_reached')",
        }
    )
    receipt = evaluate_receipt(
        task_id="P3-01",
        phase=3,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand, phase=3),
        review_data=review,
    )
    assert receipt["conclusion"] == "VERIFIED"
    assert receipt["review_summary"]["review_backend"] == "agy"
    assert receipt["review_summary"]["review_model"] == "claude-opus-5-5-medium"
    assert receipt["review_summary"]["fallback_used"] is True
    md = render_receipt_markdown(receipt)
    assert "agy/claude-opus-5-5-medium, fallback" in md


def test_receipt_blocks_non_bool_independent_contexts_failed_gate_and_nonblocking_critical() -> None:
    """NC-03: String 'false' independent_contexts, FAILED gate status, or critical+blocking=False must never verify."""
    base = "1" * 40
    cand = "2" * 40

    str_false_review = _review(cand)
    str_false_review["independent_contexts"] = "false"
    res_str = evaluate_receipt(
        task_id="NC-03A",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=str_false_review,
    )
    assert res_str["conclusion"] == "BLOCKED"

    failed_gate_review = _review(cand)
    failed_gate_review["review_gate_status"] = "FAILED"
    res_failed_gate = evaluate_receipt(
        task_id="NC-03B",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=failed_gate_review,
    )
    assert res_failed_gate["conclusion"] == "BLOCKED"

    critical_unblocked_review = _review(
        cand,
        findings=[{"id": "P2-99", "severity": "critical", "blocking": False, "title": "Critical bypass"}],
    )
    res_crit = evaluate_receipt(
        task_id="NC-03C",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=critical_unblocked_review,
    )
    assert res_crit["conclusion"] == "FAILED"


def test_receipt_blocks_dirty_worktree_empty_steps_and_failed_step_despite_verified_status() -> None:
    """NC-04: Contradictory verifier payloads (dirty worktree, empty steps, failed step) must block receipt."""
    base = "1" * 40
    cand = "2" * 40

    dirty_verifier = _verifier(cand)
    dirty_verifier["worktree_dirty"] = True
    res_dirty = evaluate_receipt(
        task_id="NC-04A",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=dirty_verifier,
        review_data=_review(cand),
    )
    assert res_dirty["conclusion"] == "BLOCKED"

    empty_steps = _verifier(cand)
    empty_steps["steps"] = []
    res_empty = evaluate_receipt(
        task_id="NC-04B",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=empty_steps,
        review_data=_review(cand),
    )
    assert res_empty["conclusion"] == "BLOCKED"

    failed_step = _verifier(cand)
    failed_step["steps"] = [{"command": ["pytest"], "status": "FAILED", "exit_code": 1}]
    res_step = evaluate_receipt(
        task_id="NC-04C",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=failed_step,
        review_data=_review(cand),
    )
    assert res_step["conclusion"] == "FAILED"


def test_receipt_rejects_refuted_disposition_with_nonexistent_file_or_line(tmp_path: Path) -> None:
    """NC-05: REFUTED_WITH_EVIDENCE must point to an existing blob and valid line range in candidate_sha."""
    repo = tmp_path / "git_nc05"
    sec_file = repo / "backend" / "app" / "security.py"
    sec_file.parent.mkdir(parents=True, exist_ok=True)
    sec_file.write_text("# base\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    sec_file.write_text("\n".join(f"# line {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    finding = {"id": "P1-55", "severity": "high", "blocking": True, "title": "Real bug"}

    bad_file_res = evaluate_receipt(
        task_id="NC-05A",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand, findings=[finding]),
        dispositions={
            "dispositions": [
                {
                    "id": "P1-55",
                    "status": "REFUTED_WITH_EVIDENCE",
                    "rationale": "Claimed false positive",
                    "evidence_ref": "nonexistent/file.py:999999",
                }
            ]
        },
        repo_root=repo,
    )
    assert bad_file_res["conclusion"] == "FAILED"

    bad_line_res = evaluate_receipt(
        task_id="NC-05B",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand, findings=[finding]),
        dispositions={
            "dispositions": [
                {
                    "id": "P1-55",
                    "status": "REFUTED_WITH_EVIDENCE",
                    "rationale": "Line out of range",
                    "evidence_ref": "backend/app/security.py:999999",
                }
            ]
        },
        repo_root=repo,
    )
    assert bad_line_res["conclusion"] == "FAILED"
    assert any("exceeds" in msg for msg in bad_line_res["failures"])


def test_receipt_blocks_invalid_red_proof_shas_and_boolean_exit_codes() -> None:
    """NC-06: red_sha must equal base_sha (40-hex), green_sha must equal candidate_sha, and exit codes must be strict ints."""
    base = "1" * 40
    cand = "2" * 40

    bad_red_sha = evaluate_receipt(
        task_id="NC-06A",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": "banana",
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": cand,
            "green_exit_code": 0,
        },
    )
    assert bad_red_sha["conclusion"] == "BLOCKED"

    mismatched_red_sha = evaluate_receipt(
        task_id="NC-06B",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": "3" * 40,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": cand,
            "green_exit_code": 0,
        },
    )
    assert mismatched_red_sha["conclusion"] == "BLOCKED"

    empty_green_sha = evaluate_receipt(
        task_id="NC-06C",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": "",
            "green_exit_code": 0,
        },
    )
    assert empty_green_sha["conclusion"] == "BLOCKED"

def test_receipt_blocks_bool_red_exit_code_with_valid_green() -> None:
    """NC-06: Boolean red_exit_code=True must be rejected even when green_exit_code=0."""
    base = "1" * 40
    cand = "2" * 40
    res = evaluate_receipt(
        task_id="NC-06D",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": True,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": cand,
            "green_exit_code": 0,
        },
    )
    assert res["conclusion"] == "BLOCKED"


def test_receipt_blocks_bool_green_exit_code_with_valid_red() -> None:
    """NC-06: Boolean green_exit_code=False must be rejected even when red_exit_code=1."""
    base = "1" * 40
    cand = "2" * 40
    res = evaluate_receipt(
        task_id="NC-06E",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": cand,
            "green_exit_code": False,
        },
    )
    assert res["conclusion"] == "BLOCKED"


def test_receipt_blocks_mismatched_pytest_k_selector() -> None:
    """P1-04: red_test_command and green_test_command differing only by -k selector must block."""
    base = "1" * 40
    cand = "2" * 40

    k_mismatch = evaluate_receipt(
        task_id="P1-04-K",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: broken",
            "green_sha": cand,
            "green_exit_code": 0,
            "red_test_command": "pytest harness/tests/test_x.py -k broken -q",
            "green_test_command": "pytest harness/tests/test_x.py -k healthy -q",
        },
    )
    assert k_mismatch["conclusion"] == "BLOCKED"


def test_receipt_blocks_pytest_addopts_mismatch_in_red_green_commands() -> None:
    """P1-01: Test-affecting env vars such as PYTEST_ADDOPTS must NOT be stripped during target comparison."""
    base = "1" * 40
    cand = "2" * 40

    addopts_mismatch = evaluate_receipt(
        task_id="P1-01-ADDOPTS",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: real bug",
            "green_sha": cand,
            "green_exit_code": 0,
            "red_test_command": "PYTHONPATH=. pytest harness/tests/test_x.py -q",
            "green_test_command": "PYTEST_ADDOPTS=--ignore=harness/tests/test_x.py pytest harness/tests/test_x.py -q",
        },
    )
    assert addopts_mismatch["conclusion"] == "BLOCKED"


def test_receipt_blocks_uv_directory_mismatch_in_red_green_commands() -> None:
    """P1-002: uv run --directory backend vs --directory frontend must not collapse to the same target."""
    base = "1" * 40
    cand = "2" * 40

    dir_mismatch = evaluate_receipt(
        task_id="P1-002-UVDIR",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: backend failed",
            "green_sha": cand,
            "green_exit_code": 0,
            "red_test_command": "uv run --directory backend pytest tests -q",
            "green_test_command": "uv run --directory frontend pytest tests -q",
        },
    )
    assert dir_mismatch["conclusion"] == "BLOCKED"


def test_receipt_rejects_refuted_evidence_when_commit_missing_in_git_even_if_file_exists_on_disk(
    tmp_path: Path,
) -> None:
    """P1-003 & S2-001: REFUTED_WITH_EVIDENCE must fail closed if candidate_sha is missing in git, never falling back to disk."""
    base = "1" * 40
    missing_cand = "f" * 40
    disk_file = tmp_path / "backend" / "app" / "security.py"
    disk_file.parent.mkdir(parents=True, exist_ok=True)
    disk_file.write_text("\n".join(f"# line {i}" for i in range(1, 60)) + "\n", encoding="utf-8")

    finding = {"id": "P1-DISK", "severity": "high", "blocking": True, "title": "Bug"}
    res = evaluate_receipt(
        task_id="P1-003-NODISK",
        phase=1,
        base_sha=base,
        candidate_sha=missing_cand,
        verifier_data=_verifier(missing_cand),
        review_data=_review(missing_cand, findings=[finding]),
        dispositions={
            "dispositions": [
                {
                    "id": "P1-DISK",
                    "status": "REFUTED_WITH_EVIDENCE",
                    "rationale": "File exists on disk, not in missing git commit",
                    "evidence_ref": "backend/app/security.py:10",
                }
            ]
        },
        repo_root=tmp_path,
    )
    assert res["conclusion"] != "VERIFIED"


def test_receipt_blocks_verifier_phase_mismatch_and_missing_phase_steps() -> None:
    """P1-C01: phase=0 verifier_data or phase=3 verifier lacking frontend step must block phase=3 receipt."""
    base = "1" * 40
    cand = "2" * 40

    phase0_v = _verifier(cand, phase=0)
    res_mismatch = evaluate_receipt(
        task_id="P1-C01-PHASE",
        phase=3,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase0_v,
        review_data=_review(cand),
    )
    assert res_mismatch["conclusion"] == "BLOCKED"

    phase3_no_fe = _verifier(cand, phase=1)
    phase3_no_fe["phase"] = 3
    res_no_fe = evaluate_receipt(
        task_id="P1-C01-NOFE",
        phase=3,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase3_no_fe,
        review_data=_review(cand),
    )
    assert res_no_fe["conclusion"] == "BLOCKED"


def test_receipt_preserves_root_and_drive_root_directory_targets() -> None:
    """P1-C02: --directory / vs . and --directory=C:/ vs C: must stay distinct and block."""
    base = "1" * 40
    cand = "2" * 40

    root_vs_dot = evaluate_receipt(
        task_id="P1-C02-ROOT",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: root",
            "green_sha": cand,
            "green_exit_code": 0,
            "red_test_command": "uv run --directory / pytest tests -q",
            "green_test_command": "uv run --directory . pytest tests -q",
        },
    )
    assert root_vs_dot["conclusion"] == "BLOCKED"

    drive_root_vs_rel = evaluate_receipt(
        task_id="P1-C02-DRIVE",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=_verifier(cand),
        review_data=_review(cand),
        is_bugfix=True,
        red_proof={
            "red_sha": base,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: drive",
            "green_sha": cand,
            "green_exit_code": 0,
            "red_test_command": "uv run --directory=C:/ pytest tests -q",
            "green_test_command": "uv run --directory=C: pytest tests -q",
        },
    )
    assert drive_root_vs_rel["conclusion"] == "BLOCKED"


def test_receipt_blocks_when_mandatory_phase_steps_are_missing() -> None:
    """P1-002: Missing mandatory step names for the given phase must return BLOCKED."""
    base = "1" * 40
    cand = "2" * 40

    phase0_only_arch = _verifier(cand, phase=0)
    phase0_only_arch["steps"] = [s for s in phase0_only_arch["steps"] if s["name"] == "arch_check"]
    res_p0 = evaluate_receipt(
        task_id="P1-002-P0",
        phase=0,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase0_only_arch,
        review_data=_review(cand),
    )
    assert res_p0["conclusion"] == "BLOCKED"
    assert any("harness_ruff" in b or "harness_pytest" in b for b in res_p0["blockers"])

    phase1_missing_be_pytest = _verifier(cand, phase=1)
    phase1_missing_be_pytest["steps"] = [
        s for s in phase1_missing_be_pytest["steps"] if s["name"] != "backend_pytest"
    ]
    res_p1 = evaluate_receipt(
        task_id="P1-002-P1",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase1_missing_be_pytest,
        review_data=_review(cand),
    )
    assert res_p1["conclusion"] == "BLOCKED"
    assert any("backend_pytest" in b for b in res_p1["blockers"])

    phase3_missing_be = _verifier(cand, phase=3)
    phase3_missing_be["steps"] = [s for s in phase3_missing_be["steps"] if s["name"] != "backend_pytest"]
    res_p3 = evaluate_receipt(
        task_id="P1-002-P3",
        phase=3,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase3_missing_be,
        review_data=_review(cand),
    )
    assert res_p3["conclusion"] == "BLOCKED"
    assert any("backend_pytest" in b for b in res_p3["blockers"])

    phase5_missing_fe = _verifier(cand, phase=5)
    phase5_missing_fe["steps"] = [s for s in phase5_missing_fe["steps"] if s["name"] != "frontend_build"]
    res_p5 = evaluate_receipt(
        task_id="P1-002-P5",
        phase=5,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=phase5_missing_fe,
        review_data=_review(cand),
    )
    assert res_p5["conclusion"] == "BLOCKED"
    assert any("frontend_build" in b for b in res_p5["blockers"])


def test_receipt_require_frozen_blocks_missing_or_none_review_tree_hash() -> None:
    """S2-001: require_frozen=True must block when review_data omits frozen_tree_sha256 or sets it to None."""
    base = "1" * 40
    cand = "2" * 40
    frozen_v = _verifier(cand, phase=1, frozen=True)
    frozen_v["frozen_tree_sha256"] = "a" * 64

    review_missing = _review(cand)
    res_missing = evaluate_receipt(
        task_id="S2-001-MISSING",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=frozen_v,
        review_data=review_missing,
        require_frozen=True,
    )
    assert res_missing["conclusion"] == "BLOCKED"

    review_none = _review(cand)
    review_none["frozen_tree_sha256"] = None
    res_none = evaluate_receipt(
        task_id="S2-001-NONE",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=frozen_v,
        review_data=review_none,
        require_frozen=True,
    )
    assert res_none["conclusion"] == "BLOCKED"


def test_receipt_blocks_none_verifier_steps_without_crashing() -> None:
    """P1-07: verifier_data with steps=None must return BLOCKED without TypeError."""
    base = "1" * 40
    cand = "2" * 40
    none_steps_v = _verifier(cand)
    none_steps_v["steps"] = None
    none_steps_res = evaluate_receipt(
        task_id="P1-07-NONE",
        phase=1,
        base_sha=base,
        candidate_sha=cand,
        verifier_data=none_steps_v,
        review_data=_review(cand),
    )
    assert none_steps_res["conclusion"] == "BLOCKED"


def test_receipt_rejects_git_tree_object_in_evidence_ref(tmp_path: Path) -> None:
    """P1-02: REFUTED_WITH_EVIDENCE pointing to a git directory tree (e.g. backend:1) must fail."""
    base = "1" * 40
    repo = tmp_path / "git_tree_ref"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    pkg = repo / "backend"
    pkg.mkdir()
    (pkg / "mod.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    real_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    finding = {"id": "P1-TREE", "severity": "high", "blocking": True, "title": "Bug"}
    tree_ref_res = evaluate_receipt(
        task_id="P1-02-TREE",
        phase=1,
        base_sha=base,
        candidate_sha=real_sha,
        verifier_data=_verifier(real_sha),
        review_data=_review(real_sha, findings=[finding]),
        dispositions={
            "dispositions": [
                {
                    "id": "P1-TREE",
                    "status": "REFUTED_WITH_EVIDENCE",
                    "rationale": "Citing directory instead of file blob",
                    "evidence_ref": "backend:1",
                }
            ]
        },
        repo_root=repo,
    )
    assert tree_ref_res["conclusion"] == "FAILED"


def test_receipt_cli_refuses_overwrite_write_once(tmp_path: Path) -> None:
    """Write-once receipt output: re-running receipt.py into an existing out_dir must fail with exit 2."""
    base = "1" * 40
    cand = "2" * 40
    v_path = tmp_path / "verifier.json"
    r_path = tmp_path / "review.json"
    out_dir = tmp_path / "out_once"
    v_path.write_text(json.dumps(_verifier(cand)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand)), encoding="utf-8")

    cmd = [
        sys.executable,
        str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
        "--task-id",
        "P1-ONCE",
        "--phase",
        "1",
        "--base-sha",
        base,
        "--candidate-sha",
        cand,
        "--verifier-json",
        str(v_path),
        "--review-json",
        str(r_path),
        "--out-dir",
        str(out_dir),
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert first.returncode == 0
    original_bytes = (out_dir / "receipt.json").read_bytes()

    second = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert second.returncode == 2
    assert (out_dir / "receipt.json").read_bytes() == original_bytes


def test_receipt_task_manifest_require_frozen_and_git_commit_evidence(tmp_path: Path) -> None:
    """Verify --task-manifest enforcement, --require-frozen hash check, and git-blob evidence_ref check."""
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    src = repo / "mod.py"
    src.write_text("line1 = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    src.write_text("line1 = 1\nline2 = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Add an untracked dirty file after cand_sha; REFUTED_WITH_EVIDENCE citing it must fail against cand_sha blob
    (repo / "dirty_only.py").write_text("x = 1\n", encoding="utf-8")
    finding = {"id": "P1-88", "severity": "high", "blocking": True, "title": "Bug"}
    dirty_ref_res = evaluate_receipt(
        task_id="NC-05-GIT",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha, findings=[finding]),
        dispositions={
            "dispositions": [
                {
                    "id": "P1-88",
                    "status": "REFUTED_WITH_EVIDENCE",
                    "rationale": "Claims dirty file proves innocence",
                    "evidence_ref": "dirty_only.py:1",
                }
            ]
        },
        repo_root=repo,
    )
    assert dirty_ref_res["conclusion"] == "FAILED"

    # Mismatched red_test_command / green_test_command must block
    mismatch_cmd_res = evaluate_receipt(
        task_id="NC-06-CMD",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha),
        is_bugfix=True,
        red_proof={
            "red_sha": base_sha,
            "red_exit_code": 1,
            "red_failure_reason": "AssertionError: boom",
            "green_sha": cand_sha,
            "green_exit_code": 0,
            "red_test_command": "pytest harness/tests/test_a.py",
            "green_test_command": "pytest harness/tests/test_b.py",
        },
        repo_root=repo,
    )
    assert mismatch_cmd_res["conclusion"] == "BLOCKED"

    # --require-frozen rejects worktree mode and mismatched hashes
    frozen_v = _verifier(cand_sha, frozen=True)
    frozen_v["frozen_tree_sha256"] = "a" * 64
    frozen_r = _review(cand_sha)
    frozen_r["frozen_tree_sha256"] = "b" * 64
    mismatch_frozen = evaluate_receipt(
        task_id="NC-09-HASH",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=frozen_v,
        review_data=frozen_r,
        require_frozen=True,
    )
    assert mismatch_frozen["conclusion"] == "BLOCKED"

    # --task-manifest prevents omitting --bugfix
    v_path = tmp_path / "v.json"
    r_path = tmp_path / "r.json"
    m_path = tmp_path / "manifest.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")
    m_path.write_text(
        json.dumps({"task_id": "BUG-MAN", "is_bugfix": True, "base_sha": base_sha}),
        encoding="utf-8",
    )
    proc_omit_bugfix = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "BUG-MAN",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--task-manifest",
            str(m_path),
            "--out-dir",
            str(tmp_path / "out_man"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_omit_bugfix.returncode == 2


def test_g5_receipt_blocks_paused_halted_or_over_cap_ledger_and_attempts(
    tmp_path: Path,
) -> None:
    """G5 receipt.py refuses VERIFIED (BLOCKED, rc=2) for halted/over-cap ledger or attempts."""
    from harness.scripts import task_runner

    repo = tmp_path / "repo_g5_cap"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "v_g5.json"
    r_path = tmp_path / "r_g5.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    # 1. Ledger in PAUSED_BUDGET -> receipt must BLOCK (rc=2, conclusion=BLOCKED).
    paused_ledger = repo / "harness" / "runs" / "T-G5-PAUSED" / "ledger.json"
    l_paused = task_runner.init_or_load_ledger(
        paused_ledger, root_task_id="T-G5-PAUSED", base_sha=base_sha, budget_usd=6.0
    )
    l_paused["state"] = "PAUSED_BUDGET"
    l_paused["candidate_sha"] = cand_sha
    task_runner.save_ledger(paused_ledger, l_paused)

    out_paused = tmp_path / "out_g5_paused"
    proc_paused = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-PAUSED",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_paused),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    receipt_paused = json.loads((out_paused / "receipt.json").read_text(encoding="utf-8"))
    assert proc_paused.returncode == 2
    assert receipt_paused["conclusion"] == "BLOCKED"

    # 2. Ledgerless task whose attempts/ dir exceeds max_g4_rounds_per_root_task (7 > 6).
    attempts_root = repo / "harness" / "runs" / "T-G5-ATTEMPTS" / "attempts"
    for idx in range(7):
        att = attempts_root / f"20261124T00000{idx}Z-{cand_sha[:12]}"
        att.mkdir(parents=True, exist_ok=True)
        (att / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": cand_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": "CHANGES_REQUESTED",
                }
            ),
            encoding="utf-8",
        )

    out_att = tmp_path / "out_g5_attempts"
    proc_att = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-ATTEMPTS",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_att),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    receipt_att = json.loads((out_att / "receipt.json").read_text(encoding="utf-8"))
    assert proc_att.returncode == 2
    assert receipt_att["conclusion"] == "BLOCKED"

    # 3. --require-ledger without ledger.json blocks; skipped G4 (ACTIVE_PEER_G1 w/o G4) blocks.
    out_req = tmp_path / "out_g5_req"
    proc_req = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-NO-LEDGER",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--require-ledger",
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_req),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_req.returncode == 2
    rec_req = json.loads((out_req / "receipt.json").read_text(encoding="utf-8"))
    assert rec_req["conclusion"] == "BLOCKED"

    skipped_ledger = repo / "harness" / "runs" / "T-G5-SKIP" / "ledger.json"
    l_skip = task_runner.init_or_load_ledger(
        skipped_ledger, root_task_id="T-G5-SKIP", base_sha=base_sha, budget_usd=6.0
    )
    l_skip["state"] = "ACTIVE_PEER_G1"
    l_skip["candidate_sha"] = cand_sha
    task_runner.save_ledger(skipped_ledger, l_skip)
    out_skip = tmp_path / "out_g5_skip"
    proc_skip = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-SKIP",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--ledger",
            str(skipped_ledger),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_skip),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_skip.returncode == 2
    rec_skip = json.loads((out_skip / "receipt.json").read_text(encoding="utf-8"))
    assert rec_skip["conclusion"] == "BLOCKED"

    # 4. Happy path READY_FOR_ACCEPTANCE transitions ledger -> VERIFIED and hashes ledger.json.
    ok_ledger = repo / "harness" / "runs" / "T-G5-OK" / "ledger.json"
    l_ok = task_runner.init_or_load_ledger(
        ok_ledger, root_task_id="T-G5-OK", base_sha=base_sha, budget_usd=6.0
    )
    l_ok["state"] = "READY_FOR_ACCEPTANCE"
    l_ok["candidate_sha"] = cand_sha
    l_ok["g4_rounds_total"] = 1
    task_runner.save_ledger(ok_ledger, l_ok)
    out_ok = tmp_path / "out_g5_ok"
    proc_ok = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-OK",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_ok),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_ok.returncode == 0
    ok_receipt = json.loads((out_ok / "receipt.json").read_text(encoding="utf-8"))
    assert ok_receipt["conclusion"] == "VERIFIED"
    assert len(ok_receipt["artifact_hashes"]["ledger_json_sha256"]) == 64
    assert json.loads(ok_ledger.read_text(encoding="utf-8"))["state"] == "VERIFIED"

    # 5. Valid dispositions from ACTIVE_PEER_G1 with last_g4_candidate_sha == cand_sha -> VERIFIED.
    disp_ledger = repo / "harness" / "runs" / "T-G5-DISP" / "ledger.json"
    l_disp = task_runner.init_or_load_ledger(
        disp_ledger, root_task_id="T-G5-DISP", base_sha=base_sha, budget_usd=6.0
    )
    l_disp["state"] = "ACTIVE_PEER_G1"
    l_disp["last_g4_candidate_sha"] = cand_sha
    l_disp["consecutive_failed_candidates"] = 1
    task_runner.save_ledger(disp_ledger, l_disp)
    r_disp = tmp_path / "r_disp.json"
    r_disp.write_text(
        json.dumps(
            _review(
                cand_sha,
                findings=[{"id": "P1-77", "severity": "medium", "blocking": True, "title": "FP"}],
            )
        ),
        encoding="utf-8",
    )
    d_path = tmp_path / "disp_ok.json"
    d_path.write_text(
        json.dumps(
            {
                "dispositions": [
                    {
                        "id": "P1-77",
                        "status": "REFUTED_WITH_EVIDENCE",
                        "rationale": "Line 1 sets constant x = 2 safely.",
                        "evidence_ref": "mod.py:1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out_disp = tmp_path / "out_g5_disp"
    proc_disp = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-DISP",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_disp),
            "--dispositions-json",
            str(d_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_disp),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_disp.returncode == 0
    disp_after = json.loads(disp_ledger.read_text(encoding="utf-8"))
    assert disp_after["state"] == "VERIFIED"
    assert disp_after["candidate_sha"] == cand_sha
    assert disp_after["consecutive_failed_candidates"] == 0

    # 6. g4-tracker.json > 6 blocks G5 even when attempts/ dir is empty (custom --out-dir).
    tracker_path = task_runner.default_g4_tracker_path("T-G5-TRACKER", repo_root=repo)
    tracker = task_runner.load_g4_tracker(tracker_path, task_id="T-G5-TRACKER")
    tracker["completed_rounds"] = 7
    task_runner.save_g4_tracker(tracker_path, tracker)
    out_trk = tmp_path / "out_g5_tracker"
    proc_trk = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "receipt.py"),
            "--task-id",
            "T-G5-TRACKER",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_trk),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_trk.returncode == 2
    rec_trk = json.loads((out_trk / "receipt.json").read_text(encoding="utf-8"))
    assert rec_trk["conclusion"] == "BLOCKED"


def test_g5_receipt_evaluates_and_writes_atomically_under_ledger_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent PAUSED_BUDGET mutation and receipt.main never emit VERIFIED on PAUSED ledger."""
    import threading

    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo = tmp_path / "repo_g5_atomic"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "v_atom.json"
    r_path = tmp_path / "r_atom.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    # 1. Deterministic check: mutation right before _ledger_lock acquisition makes G5 see PAUSED.
    ledger_det = repo / "harness" / "runs" / "T-G5-DET" / "ledger.json"
    l_det = task_runner.init_or_load_ledger(
        ledger_det, root_task_id="T-G5-DET", base_sha=base_sha, budget_usd=1.0
    )
    l_det["state"] = "READY_FOR_ACCEPTANCE"
    l_det["candidate_sha"] = cand_sha
    task_runner.save_ledger(ledger_det, l_det)

    orig_lock = task_runner._ledger_lock
    mutated_once = False

    def _mutating_lock_wrapper(p: Path):
        nonlocal mutated_once
        if not mutated_once and p.resolve() == ledger_det.resolve():
            mutated_once = True
            with orig_lock(ledger_det):
                cur = task_runner.load_ledger(ledger_det)
                cur["state"] = "PAUSED_BUDGET"
                task_runner.save_ledger(ledger_det, cur)
        return orig_lock(p)

    monkeypatch.setattr(task_runner, "_ledger_lock", _mutating_lock_wrapper)
    out_det = tmp_path / "out_g5_det"
    rc_det = receipt_mod.main(
        [
            "--task-id",
            "T-G5-DET",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_det),
        ]
    )
    assert rc_det == 2
    assert json.loads((out_det / "receipt.json").read_text(encoding="utf-8"))["conclusion"] == (
        "BLOCKED"
    )
    monkeypatch.setattr(task_runner, "_ledger_lock", orig_lock)

    # 2. Barrier race between receipt.main and concurrent record_usage exceeding budget.
    ledger_race = repo / "harness" / "runs" / "T-G5-RACE" / "ledger.json"
    l_race = task_runner.init_or_load_ledger(
        ledger_race, root_task_id="T-G5-RACE", base_sha=base_sha, budget_usd=1.0
    )
    l_race["state"] = "READY_FOR_ACCEPTANCE"
    l_race["candidate_sha"] = cand_sha
    task_runner.save_ledger(ledger_race, l_race)

    out_race = tmp_path / "out_g5_race"
    barrier = threading.Barrier(2)
    g5_rc_box: list[int] = []

    def _run_g5() -> None:
        barrier.wait(timeout=5.0)
        g5_rc_box.append(
            receipt_mod.main(
                [
                    "--task-id",
                    "T-G5-RACE",
                    "--phase",
                    "1",
                    "--base-sha",
                    base_sha,
                    "--candidate-sha",
                    cand_sha,
                    "--verifier-json",
                    str(v_path),
                    "--review-json",
                    str(r_path),
                    "--repo-root",
                    str(repo),
                    "--out-dir",
                    str(out_race),
                ]
            )
        )

    def _run_over_budget() -> None:
        barrier.wait(timeout=5.0)
        task_runner.record_usage(
            ledger_race, model="gpt-6.1-sol", input_tokens=1_000_000, counter_mode="delta"
        )

    t_g5 = threading.Thread(target=_run_g5)
    t_spend = threading.Thread(target=_run_over_budget)
    t_g5.start()
    t_spend.start()
    t_g5.join(timeout=10.0)
    t_spend.join(timeout=10.0)

    rec_race = json.loads((out_race / "receipt.json").read_text(encoding="utf-8"))
    led_race_after = json.loads(ledger_race.read_text(encoding="utf-8"))
    if led_race_after["state"] == "PAUSED_BUDGET":
        assert g5_rc_box[0] == 2
        assert rec_race["conclusion"] == "BLOCKED"
    else:
        assert led_race_after["state"] == "VERIFIED"
        assert g5_rc_box[0] == 0
        assert rec_race["conclusion"] == "VERIFIED"


def test_g5_receipt_blocks_union_over_cap_across_attempts_and_custom_out_dir(
    tmp_path: Path,
) -> None:
    """6 legacy attempts/ dirs + 1 custom --out-dir in g4-tracker = 7 > 6 blocks G5."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo = tmp_path / "repo_g5_union"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "v_union.json"
    r_path = tmp_path / "r_union.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    attempts_root = repo / "harness" / "runs" / "T-G5-UNION" / "attempts"
    for idx in range(6):
        p_sha = f"{idx + 1}" * 40
        p_dir = attempts_root / f"att-{idx}-{p_sha[:12]}"
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

    tracker_path = task_runner.default_g4_tracker_path("T-G5-UNION", repo_root=repo)
    tracker = task_runner.load_g4_tracker(tracker_path, task_id="T-G5-UNION")
    custom_out = (tmp_path / "custom_out_g5").resolve().as_posix()
    tracker["completed_rounds"] = 1
    tracker["completed_attempts"] = {
        custom_out: {"candidate_sha": cand_sha, "status": "PASSED"}
    }
    task_runner.save_g4_tracker(tracker_path, tracker)

    ledger_path = repo / "harness" / "runs" / "T-G5-UNION" / "ledger.json"
    ledger = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-G5-UNION", base_sha=base_sha, budget_usd=6.0
    )
    ledger["state"] = "READY_FOR_ACCEPTANCE"
    ledger["candidate_sha"] = cand_sha
    task_runner.save_ledger(ledger_path, ledger)

    out_union = tmp_path / "out_g5_union"
    rc_union = receipt_mod.main(
        [
            "--task-id",
            "T-G5-UNION",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_union),
        ]
    )
    assert rc_union == 2
    rec_union = json.loads((out_union / "receipt.json").read_text(encoding="utf-8"))
    assert rec_union["conclusion"] == "BLOCKED"
    assert any("7" in b and "max_g4_rounds_per_root_task" in b for b in rec_union["blockers"])


def test_g5_receipt_blocks_per_sha_cap_and_verifies_ledger_hash_exact(
    tmp_path: Path,
) -> None:
    """Exceeding max_g4_invocations_per_candidate_sha (2) blocks G5 even with rounds <= 6."""
    import hashlib

    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo = tmp_path / "repo_g5_sha_cap"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "v_sha.json"
    r_path = tmp_path / "r_sha.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    # 1. Two BLOCKED_INFRA + one PASSED attempt on same SHA -> 1 round <= 6, yet 3 > 2 -> BLOCKED.
    attempts_sha = repo / "harness" / "runs" / "T-G5-SHA-FS" / "attempts"
    for idx, st in enumerate(("BLOCKED_INFRA", "BLOCKED_INFRA", "PASSED")):
        d = attempts_sha / f"att-{idx}-{cand_sha[:12]}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "consolidated-review.json").write_text(
            json.dumps(
                {
                    "revision_sha": cand_sha,
                    "passes_completed": 2,
                    "independent_contexts": True,
                    "review_gate_status": st,
                    "blocking_open_count": 0,
                    "consolidated_findings": [],
                }
            ),
            encoding="utf-8",
        )
    led_fs = repo / "harness" / "runs" / "T-G5-SHA-FS" / "ledger.json"
    l_fs = task_runner.init_or_load_ledger(
        led_fs, root_task_id="T-G5-SHA-FS", base_sha=base_sha, budget_usd=6.0
    )
    l_fs["state"] = "READY_FOR_ACCEPTANCE"
    l_fs["candidate_sha"] = cand_sha
    l_fs["g4_rounds_total"] = 1
    task_runner.save_ledger(led_fs, l_fs)

    out_fs = tmp_path / "out_g5_sha_fs"
    rc_fs = receipt_mod.main(
        [
            "--task-id",
            "T-G5-SHA-FS",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_fs),
        ]
    )
    assert rc_fs == 2
    rec_fs = json.loads((out_fs / "receipt.json").read_text(encoding="utf-8"))
    assert rec_fs["conclusion"] == "BLOCKED"
    assert any("max_g4_invocations_per_candidate_sha" in b for b in rec_fs["blockers"])

    # 2. Ledger g4_invocations_by_sha[cand_sha] == 3 with rounds <= 6 -> BLOCKED.
    led_map = repo / "harness" / "runs" / "T-G5-SHA-LED" / "ledger.json"
    l_map = task_runner.init_or_load_ledger(
        led_map, root_task_id="T-G5-SHA-LED", base_sha=base_sha, budget_usd=6.0
    )
    l_map["state"] = "READY_FOR_ACCEPTANCE"
    l_map["candidate_sha"] = cand_sha
    l_map["g4_rounds_total"] = 1
    l_map["g4_invocations_by_sha"] = {cand_sha: 3}
    task_runner.save_ledger(led_map, l_map)

    out_map = tmp_path / "out_g5_sha_led"
    rc_map = receipt_mod.main(
        [
            "--task-id",
            "T-G5-SHA-LED",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_map),
        ]
    )
    assert rc_map == 2
    rec_map = json.loads((out_map / "receipt.json").read_text(encoding="utf-8"))
    assert rec_map["conclusion"] == "BLOCKED"
    assert any("g4_invocations_by_sha" in b for b in rec_map["blockers"])

    # 3. Valid VERIFIED run checks exact sha256(ledger.json) recorded in artifact_hashes.
    led_ok = repo / "harness" / "runs" / "T-G5-SHA-OK" / "ledger.json"
    l_ok = task_runner.init_or_load_ledger(
        led_ok, root_task_id="T-G5-SHA-OK", base_sha=base_sha, budget_usd=6.0
    )
    l_ok["state"] = "READY_FOR_ACCEPTANCE"
    l_ok["candidate_sha"] = cand_sha
    l_ok["g4_rounds_total"] = 1
    l_ok["g4_invocations_by_sha"] = {cand_sha: 2}
    task_runner.save_ledger(led_ok, l_ok)
    expected_ledger_sha = hashlib.sha256(led_ok.read_bytes()).hexdigest()

    out_ok = tmp_path / "out_g5_sha_ok"
    rc_ok = receipt_mod.main(
        [
            "--task-id",
            "T-G5-SHA-OK",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_ok),
        ]
    )
    assert rc_ok == 0
    rec_ok = json.loads((out_ok / "receipt.json").read_text(encoding="utf-8"))
    assert rec_ok["conclusion"] == "VERIFIED"
    assert rec_ok["artifact_hashes"]["ledger_json_sha256"] == expected_ledger_sha


def test_g5_root_discovery_foreign_ledger_paused_with_findings_and_human_override(
    tmp_path: Path,
) -> None:
    """Verify root discovery, foreign ledger rejection, PAUSED+findings -> BLOCKED, and cap=7."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    repo = tmp_path / "repo_g5_a6"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    v_path = tmp_path / "v_a6.json"
    r_ok_path = tmp_path / "r_a6_ok.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_ok_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    # 1. Root discovery + PAUSED_BUDGET with CHANGES_REQUESTED (1 open finding) -> BLOCKED (rc=2).
    r_cr_path = tmp_path / "r_a6_cr.json"
    cr_review = _review(
        cand_sha,
        findings=[
            {
                "id": "P1-01",
                "severity": "high",
                "blocking": True,
                "file": "mod.py",
                "line_start": 1,
                "line_end": 1,
                "title": "Open blocking finding",
                "status": "OPEN",
            }
        ],
    )
    cr_review["blocking_open_count"] = 1
    r_cr_path.write_text(json.dumps(cr_review), encoding="utf-8")

    root_led_path = repo / "harness" / "runs" / "ROOT" / "ledger.json"
    l_root = task_runner.init_or_load_ledger(
        root_led_path, root_task_id="ROOT", task_id="CHILD-V2", base_sha=base_sha, budget_usd=6.0
    )
    l_root["state"] = "PAUSED_BUDGET"
    l_root["candidate_sha"] = cand_sha
    task_runner.save_ledger(root_led_path, l_root)

    out_root = tmp_path / "out_g5_root_paused"
    rc_root = receipt_mod.main(
        [
            "--task-id",
            "CHILD-V2",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_cr_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_root),
        ]
    )
    assert rc_root == 2
    rec_root = json.loads((out_root / "receipt.json").read_text(encoding="utf-8"))
    assert rec_root["conclusion"] == "BLOCKED"
    assert rec_root["failures"]

    # 2. Foreign --ledger OTHER/ledger.json with --task-id CHILD-V2 -> BLOCKED (rc=2).
    other_led_path = repo / "harness" / "runs" / "OTHER" / "ledger.json"
    l_other = task_runner.init_or_load_ledger(
        other_led_path, root_task_id="OTHER", base_sha=base_sha, budget_usd=6.0
    )
    l_other["state"] = "READY_FOR_ACCEPTANCE"
    l_other["candidate_sha"] = cand_sha
    task_runner.save_ledger(other_led_path, l_other)

    out_foreign = tmp_path / "out_g5_foreign"
    rc_foreign = receipt_mod.main(
        [
            "--task-id",
            "CHILD-V2",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_ok_path),
            "--ledger",
            str(other_led_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_foreign),
        ]
    )
    assert rc_foreign == 2
    rec_foreign = json.loads((out_foreign / "receipt.json").read_text(encoding="utf-8"))
    assert rec_foreign["conclusion"] == "BLOCKED"
    assert any("Ledger task mismatch" in b for b in rec_foreign["blockers"])

    # 3. Human-approved extended circuit_breakers (max_total_candidates=7, max_g4_rounds=7) -> OK.
    ext_led_path = repo / "harness" / "runs" / "T-G5-EXT" / "ledger.json"
    l_ext = task_runner.init_or_load_ledger(
        ext_led_path, root_task_id="T-G5-EXT", base_sha=base_sha, budget_usd=6.0
    )
    l_ext["state"] = "READY_FOR_ACCEPTANCE"
    l_ext["candidate_sha"] = cand_sha
    l_ext["total_candidates"] = 7
    l_ext["g4_rounds_total"] = 7
    l_ext["circuit_breakers"]["max_total_candidates_per_root_task"] = 7
    l_ext["circuit_breakers"]["max_g4_rounds_per_root_task"] = 7
    task_runner.save_ledger(ext_led_path, l_ext)

    out_ext = tmp_path / "out_g5_ext"
    rc_ext = receipt_mod.main(
        [
            "--task-id",
            "T-G5-EXT",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_ok_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_ext),
        ]
    )
    assert rc_ext == 0
    rec_ext = json.loads((out_ext / "receipt.json").read_text(encoding="utf-8"))
    assert rec_ext["conclusion"] == "VERIFIED"


def test_g5_ledgerless_root_packet_blocks_child_over_cap(tmp_path: Path) -> None:
    """Without ledger.json, CHILD-V2 declaring Root Task ID=ROOT shares ROOT's 6 attempts."""
    from harness.scripts import receipt as receipt_mod

    repo = tmp_path / "repo_g5_root_nolog"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    src = repo / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    src.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cand"], cwd=repo, check=True, capture_output=True)
    cand_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    root_attempts = repo / "harness" / "runs" / "ROOT" / "attempts"
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
    child_att = repo / "harness" / "runs" / "CHILD-V2" / "attempts" / f"att-7-{cand_sha[:12]}"
    child_att.mkdir(parents=True, exist_ok=True)
    (child_att / "consolidated-review.json").write_text(
        json.dumps(
            {
                "revision_sha": cand_sha,
                "passes_completed": 2,
                "independent_contexts": True,
                "review_gate_status": "PASSED",
                "blocking_open_count": 0,
                "consolidated_findings": [],
            }
        ),
        encoding="utf-8",
    )

    pkt = tmp_path / "child_packet.md"
    pkt.write_text(
        "# Task Packet\n\n- **Root Task ID**: `ROOT`\n- **Task ID**: `CHILD-V2`\n",
        encoding="utf-8",
    )
    v_path = tmp_path / "v_nolog_root.json"
    r_path = tmp_path / "r_nolog_root.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    out_dir = tmp_path / "out_g5_nolog_root"
    rc = receipt_mod.main(
        [
            "--task-id",
            "CHILD-V2",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--task-packet",
            str(pkt),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_dir),
        ]
    )
    assert rc == 2
    rec = json.loads((out_dir / "receipt.json").read_text(encoding="utf-8"))
    assert rec["conclusion"] == "BLOCKED"
    assert any("7" in b and "max_g4_rounds_per_root_task" in b for b in rec["blockers"])


def test_g5_receipt_blocks_per_packet_ledger_and_ledgerless_ceilings(tmp_path: Path) -> None:
    """G5 blocks when ledger or ledgerless tracker exceeds per-packet round/candidate caps."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    cand_sha = "c" * 40
    base_sha = "b" * 40
    rec_g4 = receipt_mod.evaluate_receipt(
        task_id="T-PKT-CAP",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha),
        repo_root=tmp_path,
        ledger_data={
            "schema_version": 1,
            "root_task_id": "T-PKT-CAP",
            "task_id": "T-PKT-CAP",
            "state": "READY_FOR_ACCEPTANCE",
            "candidate_sha": cand_sha,
            "g4_rounds_total": 4,
            "g4_rounds_in_packet": 4,
            "candidates_in_packet": 2,
            "total_candidates": 4,
            "g4_invocations_by_sha": {cand_sha: 1},
            "active_g4_reservations": {},
        },
        require_ledger=True,
    )
    assert rec_g4["conclusion"] == "BLOCKED"
    assert any("g4_rounds_in_packet=4/3" in b for b in rec_g4["blockers"])

    rec_cand = receipt_mod.evaluate_receipt(
        task_id="T-PKT-CAND",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha),
        repo_root=tmp_path,
        ledger_data={
            "schema_version": 1,
            "root_task_id": "T-PKT-CAND",
            "task_id": "T-PKT-CAND",
            "state": "READY_FOR_ACCEPTANCE",
            "candidate_sha": cand_sha,
            "g4_rounds_total": 2,
            "g4_rounds_in_packet": 2,
            "candidates_in_packet": 4,
            "total_candidates": 4,
            "g4_invocations_by_sha": {cand_sha: 1},
            "active_g4_reservations": {},
        },
        require_ledger=True,
    )
    assert rec_cand["conclusion"] == "BLOCKED"
    assert any("candidates_in_packet=4/3" in b for b in rec_cand["blockers"])

    pkt = tmp_path / "harness" / "runs" / "T-NOLOG-PKT" / "packet.md"
    pkt.parent.mkdir(parents=True, exist_ok=True)
    pkt.write_text("# Packet\n\n- **Packet Version**: `1`\n", encoding="utf-8")
    epoch = task_runner.extract_packet_epoch(pkt)
    assert epoch is not None
    trk_path = task_runner.default_g4_tracker_path("T-NOLOG-PKT", repo_root=tmp_path)
    trk = task_runner.load_g4_tracker(trk_path, task_id="T-NOLOG-PKT")
    trk["packet_rounds"][epoch] = 4
    task_runner.save_g4_tracker(trk_path, trk)

    rec_nolog = receipt_mod.evaluate_receipt(
        task_id="T-NOLOG-PKT",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha),
        repo_root=tmp_path,
        packet_candidate=pkt,
    )
    assert rec_nolog["conclusion"] == "BLOCKED"
    assert any("max_g4_rounds_per_packet_version" in b for b in rec_nolog["blockers"])


def test_g5_receipt_blocks_corrupt_root_and_sibling_g4_tracker(tmp_path: Path) -> None:
    """Corrupt root or sibling g4-tracker.json fails closed with rc=2 (BLOCKED)."""
    from harness.scripts import receipt as receipt_mod

    repo = tmp_path / "repo_corrupt_g5"
    repo.mkdir()
    base_sha = "1" * 40
    cand_sha = "2" * 40
    root_dir = repo / "harness" / "runs" / "ROOT-CORR"
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "g4-tracker.json").write_text("{corrupt", encoding="utf-8")
    v_path = tmp_path / "v_corr.json"
    r_path = tmp_path / "r_corr.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")

    out_root = tmp_path / "out_corr_root"
    rc_root = receipt_mod.main(
        [
            "--task-id",
            "ROOT-CORR",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_root),
        ]
    )
    assert rc_root == 2
    rec_root = json.loads((out_root / "receipt.json").read_text(encoding="utf-8"))
    assert rec_root["conclusion"] == "BLOCKED"
    assert any("g4-tracker.json" in b for b in rec_root["blockers"])

    # Valid root tracker, corrupt sibling tracker discovered via packet.md.
    (root_dir / "g4-tracker.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "ROOT-CORR",
                "completed_rounds": 1,
                "sha_invocations": {},
                "in_flight": {},
                "completed_attempts": {},
                "attempts": {},
            }
        ),
        encoding="utf-8",
    )
    sib_dir = repo / "harness" / "runs" / "SIB-CORR"
    sib_dir.mkdir(parents=True, exist_ok=True)
    (sib_dir / "packet.md").write_text(
        "# P\n\n- **Root Task ID**: `ROOT-CORR`\n", encoding="utf-8"
    )
    (sib_dir / "g4-tracker.json").write_text("{bad-sibling", encoding="utf-8")
    child_pkt = tmp_path / "child_v2.md"
    child_pkt.write_text("# P\n\n- **Root Task ID**: `ROOT-CORR`\n", encoding="utf-8")

    out_sib = tmp_path / "out_corr_sib"
    rc_sib = receipt_mod.main(
        [
            "--task-id",
            "CHILD-CLEAN",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--task-packet",
            str(child_pkt),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_sib),
        ]
    )
    assert rc_sib == 2
    rec_sib = json.loads((out_sib / "receipt.json").read_text(encoding="utf-8"))
    assert rec_sib["conclusion"] == "BLOCKED"
    assert any("sibling g4-tracker.json" in b for b in rec_sib["blockers"])


def test_g5_receipt_blocks_hybrid_tracker_packet_cap_and_wrong_type_tracker(
    tmp_path: Path,
) -> None:
    """G5 blocks when tracker packet rounds > 3 even with low-count ledger, or bad field type."""
    from harness.scripts import receipt as receipt_mod
    from harness.scripts import task_runner

    cand_sha = "e" * 40
    base_sha = "d" * 40
    pkt = tmp_path / "harness" / "runs" / "T-HYB-G5" / "packet.md"
    pkt.parent.mkdir(parents=True, exist_ok=True)
    pkt.write_text("# P\n\n- **Root Task ID**: `T-HYB-G5`\n- **Packet Version**: `1`\n")
    epoch = task_runner.extract_packet_epoch(pkt)
    assert epoch is not None

    trk_path = task_runner.default_g4_tracker_path("T-HYB-G5", repo_root=tmp_path)
    trk = task_runner.load_g4_tracker(trk_path, task_id="T-HYB-G5")
    trk["completed_rounds"] = 4
    trk["packet_rounds"][epoch] = 4
    task_runner.save_g4_tracker(trk_path, trk)

    rec_hyb = receipt_mod.evaluate_receipt(
        task_id="T-HYB-G5",
        phase=1,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_data=_verifier(cand_sha),
        review_data=_review(cand_sha),
        repo_root=tmp_path,
        packet_candidate=pkt,
        ledger_data={
            "schema_version": 1,
            "root_task_id": "T-HYB-G5",
            "task_id": "T-HYB-G5",
            "state": "READY_FOR_ACCEPTANCE",
            "candidate_sha": cand_sha,
            "g4_rounds_total": 4,
            "g4_rounds_in_packet": 1,
            "candidates_in_packet": 1,
            "total_candidates": 4,
            "g4_invocations_by_sha": {cand_sha: 1},
            "active_g4_reservations": {},
        },
        require_ledger=True,
    )
    assert rec_hyb["conclusion"] == "BLOCKED"
    assert any("max_g4_rounds_per_packet_version" in b for b in rec_hyb["blockers"])

    # Valid-JSON wrong-field-type tracker fails closed via receipt CLI too.
    repo = tmp_path / "repo_g5_bad_type"
    repo.mkdir()
    root_dir = repo / "harness" / "runs" / "ROOT-G5BT"
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "g4-tracker.json").write_text(
        json.dumps({"schema_version": 1, "task_id": "ROOT-G5BT", "in_flight": ["x"]}),
        encoding="utf-8",
    )
    v_path = tmp_path / "v_g5bt.json"
    r_path = tmp_path / "r_g5bt.json"
    v_path.write_text(json.dumps(_verifier(cand_sha)), encoding="utf-8")
    r_path.write_text(json.dumps(_review(cand_sha)), encoding="utf-8")
    out_bt = tmp_path / "out_g5bt"
    rc_bt = receipt_mod.main(
        [
            "--task-id",
            "ROOT-G5BT",
            "--phase",
            "1",
            "--base-sha",
            base_sha,
            "--candidate-sha",
            cand_sha,
            "--verifier-json",
            str(v_path),
            "--review-json",
            str(r_path),
            "--repo-root",
            str(repo),
            "--out-dir",
            str(out_bt),
        ]
    )
    assert rc_bt == 2
    assert json.loads((out_bt / "receipt.json").read_text(encoding="utf-8"))["conclusion"] == (
        "BLOCKED"
    )









