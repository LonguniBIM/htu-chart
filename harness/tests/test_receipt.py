"""Tests for receipt.py gate adjudication (VERIFIED / FAILED / BLOCKED)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.scripts.receipt import evaluate_receipt

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
    base = "1" * 40
    cand = "2" * 40
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
