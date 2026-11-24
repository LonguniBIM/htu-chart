"""Tests for receipt.py gate adjudication (VERIFIED / FAILED / BLOCKED)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


