"""Tests for receipt.py gate adjudication (VERIFIED / FAILED / BLOCKED)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.scripts.receipt import evaluate_receipt, render_receipt_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]


def _verifier(sha: str, status: str = "VERIFIED") -> dict:
    return {
        "status": status,
        "git_sha": sha,
        "worktree_dirty": False,
        "steps": [
            {"command": ["pytest", "harness/tests"], "status": "PASSED" if status == "VERIFIED" else "FAILED", "exit_code": 0 if status == "VERIFIED" else 1}
        ],
    }


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
        verifier_data=_verifier(cand),
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


def test_receipt_rejects_refuted_disposition_with_nonexistent_file_or_line() -> None:
    """NC-05: REFUTED_WITH_EVIDENCE must point to an existing file and valid line range."""
    base = "1" * 40
    cand = "2" * 40
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
    )
    assert bad_file_res["conclusion"] != "VERIFIED"

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
    )
    assert bad_line_res["conclusion"] != "VERIFIED"


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
    frozen_v = _verifier(cand_sha)
    frozen_v["source_mode"] = "frozen_commit_archive"
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
