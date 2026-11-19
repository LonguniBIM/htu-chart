"""Immutable gate receipt generator and final task adjudicator.

Implements pstack-codex-room.md §7 (immutable receipts with artifact SHA-256)
and test-required.md §§5, 7, 8, 9 (RED->GREEN bugfix proof, 2-pass review
consolidation, verifier gate, and VERIFIED/FAILED/BLOCKED verdict table).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

ENV_OR_IMPORT_ERROR_MARKERS = (
    "modulenotfounderror",
    "importerror",
    "syntaxerror",
    "collection error",
    "no tests ran",
    "collected 0 items",
    "fixture not found",
    "missing dependency",
)


def _sha256_file(path: Path | None) -> str | None:
    if not path or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_red_proof(red_proof: dict[str, Any] | None, candidate_sha: str) -> tuple[bool, str]:
    if not isinstance(red_proof, dict):
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (missing red_proof)."

    red_exit = red_proof.get("red_exit_code")
    green_exit = red_proof.get("green_exit_code")
    red_sha = str(red_proof.get("red_sha", "")).strip()
    green_sha = str(red_proof.get("green_sha", "")).strip()
    reason = str(red_proof.get("red_failure_reason", "")).strip()

    if not isinstance(red_exit, int) or red_exit == 0:
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (red_exit_code must be non-zero)."
    if not reason:
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (missing failure reason)."
    lowered = reason.lower()
    if any(marker in lowered for marker in ENV_OR_IMPORT_ERROR_MARKERS):
        return False, f"BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (invalid RED reason: {reason})."
    if not red_sha or red_sha == candidate_sha:
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (red_sha must differ from candidate_sha)."
    if not isinstance(green_exit, int) or green_exit != 0:
        return False, f"FAILED — regression test failed after fix (green_exit_code={green_exit})."
    if green_sha and green_sha != candidate_sha:
        return False, f"BLOCKED — GREEN test ran on mismatched SHA ({green_sha} != {candidate_sha})."
    return True, f"RED on {red_sha[:12]} (exit={red_exit}: {reason}) -> GREEN on {candidate_sha[:12]} (exit=0)"


def _index_dispositions(dispositions: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(dispositions, dict):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for item in dispositions.get("dispositions", []):
        fid = str(item.get("id", "")).strip()
        if fid:
            indexed[fid] = item
    return indexed


def _evaluate_review_gate(
    review_data: dict[str, Any] | None,
    candidate_sha: str,
    dispositions: dict[str, Any] | None,
) -> tuple[str, list[str], dict[str, Any]]:
    if not isinstance(review_data, dict):
        return "BLOCKED", ["Missing consolidated Codex Room review JSON."], {"passes_completed": 0}

    passes = int(review_data.get("passes_completed", 0))
    independent = bool(review_data.get("independent_contexts", False))
    rev_sha = str(review_data.get("revision_sha", "")).strip()
    gate_status = str(review_data.get("review_gate_status", ""))

    if passes < 2:
        return "BLOCKED", [f"Review gate requires at least 2 passes, got {passes}."], {"passes_completed": passes}
    if not independent:
        return "BLOCKED", ["Review passes did not run in independent contexts."], {"passes_completed": passes}
    if rev_sha != candidate_sha:
        return (
            "BLOCKED",
            [f"Review revision_sha mismatch ({rev_sha} != {candidate_sha}); stale review cannot approve."],
            {"passes_completed": passes, "revision_sha": rev_sha},
        )
    if gate_status == "BLOCKED":
        return (
            "BLOCKED",
            list(review_data.get("errors", ["Review reported BLOCKED."])),
            {"passes_completed": passes, "revision_sha": rev_sha},
        )

    disp_map = _index_dispositions(dispositions)
    unresolved: list[str] = []
    resolved: list[dict[str, Any]] = []

    for finding in review_data.get("consolidated_findings", []):
        if not finding.get("blocking"):
            continue
        candidate_ids = [str(finding.get("id", "")), *[str(s) for s in finding.get("source_ids", [])]]
        matched_disp = next((disp_map[cid] for cid in candidate_ids if cid in disp_map), None)
        if not matched_disp:
            unresolved.append(f"{finding.get('id')}: {finding.get('title')} (OPEN)")
            continue

        d_status = str(matched_disp.get("status", "")).upper()
        rationale = str(matched_disp.get("rationale", "")).strip()
        evidence_ref = str(matched_disp.get("evidence_ref", "")).strip()

        if d_status == "REFUTED_WITH_EVIDENCE" and rationale and evidence_ref:
            resolved.append({"id": finding.get("id"), "status": d_status, "evidence_ref": evidence_ref})
        elif d_status == "FIXED":
            verified_sha = str(matched_disp.get("verified_on_sha", "")).strip()
            if verified_sha and verified_sha != rev_sha and verified_sha == candidate_sha:
                resolved.append({"id": finding.get("id"), "status": d_status, "verified_on_sha": verified_sha})
            else:
                unresolved.append(
                    f"{finding.get('id')}: marked FIXED without re-review on newer SHA (test-required.md §7)"
                )
        else:
            unresolved.append(f"{finding.get('id')}: invalid disposition {d_status!r}")

    summary = {
        "passes_completed": passes,
        "independent_contexts": independent,
        "revision_sha": rev_sha,
        "total_findings": len(review_data.get("consolidated_findings", [])),
        "resolved_blocking": resolved,
        "unresolved_blocking": unresolved,
    }
    if unresolved:
        return "FAILED", unresolved, summary
    return "PASSED", [], summary


def evaluate_receipt(
    *,
    task_id: str,
    phase: int,
    base_sha: str,
    candidate_sha: str,
    verifier_data: dict[str, Any] | None,
    review_data: dict[str, Any] | None = None,
    is_bugfix: bool = False,
    red_proof: dict[str, Any] | None = None,
    dispositions: dict[str, Any] | None = None,
    debate_data: dict[str, Any] | None = None,
    behavior_summary: str = "",
    artifact_hashes: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Evaluate all gates and build the immutable receipt payload."""
    blockers: list[str] = []
    failures: list[str] = []

    # 1. Verifier gate
    if not isinstance(verifier_data, dict):
        verifier_gate = "BLOCKED"
        blockers.append("Missing build_verifier output JSON.")
    else:
        v_status = str(verifier_data.get("status", "FAILED"))
        v_sha = verifier_data.get("git_sha")
        if v_status == "FAILED":
            verifier_gate = "FAILED"
            failures.append("build_verifier reported FAILED.")
        elif v_sha != candidate_sha:
            verifier_gate = "BLOCKED"
            blockers.append(f"build_verifier git_sha mismatch ({v_sha!r} != {candidate_sha!r}).")
        elif v_status == "BLOCKED":
            verifier_gate = "BLOCKED"
            blockers.append("build_verifier reported BLOCKED (missing required phase target).")
        elif v_status == "VERIFIED":
            verifier_gate = "VERIFIED"
        else:
            verifier_gate = "FAILED"
            failures.append(f"Unexpected build_verifier status: {v_status!r}")

    # 2. Bugfix RED -> GREEN gate
    if is_bugfix:
        red_ok, red_msg = _validate_red_proof(red_proof, candidate_sha)
        red_gate = "PASSED" if red_ok else ("FAILED" if red_msg.startswith("FAILED") else "BLOCKED")
        if red_gate == "FAILED":
            failures.append(red_msg)
        elif red_gate == "BLOCKED":
            blockers.append(red_msg)
    else:
        red_gate = "NOT_APPLICABLE"
        red_msg = "Không áp dụng (non-bugfix task)."

    # 3. Review gate (2 independent Codex Room passes)
    review_gate, review_reasons, review_summary = _evaluate_review_gate(
        review_data, candidate_sha, dispositions
    )
    if review_gate == "FAILED":
        failures.extend(review_reasons)
    elif review_gate == "BLOCKED":
        blockers.extend(review_reasons)

    if failures:
        conclusion = "FAILED"
    elif blockers:
        conclusion = "BLOCKED"
    else:
        conclusion = "VERIFIED"

    verifier_steps = verifier_data.get("steps", []) if isinstance(verifier_data, dict) else []
    commands_summary = [
        f"`{' '.join(s.get('command', []))}` -> {s.get('status')} (exit={s.get('exit_code')})"
        for s in verifier_steps
    ]

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "phase": phase,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "is_bugfix": is_bugfix,
        "behavior_summary": behavior_summary or f"Task {task_id} (Phase {phase})",
        "gates": {
            "verifier": verifier_gate,
            "red_to_green": red_gate,
            "review": review_gate,
        },
        "red_to_green_detail": red_msg,
        "commands_summary": commands_summary,
        "review_summary": review_summary,
        "debate_summary": debate_data or {},
        "artifact_hashes": artifact_hashes or {},
        "failures": failures,
        "blockers": blockers,
        "conclusion": conclusion,
    }


def render_receipt_markdown(receipt: dict[str, Any]) -> str:
    """Render receipt in the 7-row Vietnamese evidence table from test-required.md §9."""
    rev_short = receipt["candidate_sha"][:12]
    red_row = receipt["red_to_green_detail"] if receipt["is_bugfix"] else "Không áp dụng (task không phải bug fix)."
    green_row = (
        f"GREEN trên `{rev_short}`"
        if receipt["gates"]["red_to_green"] == "PASSED"
        else (
            "Chưa đạt" if receipt["is_bugfix"] else "Tất cả kiểm tra bắt buộc GREEN trên candidate."
        )
    )
    cmds_row = "<br>".join(receipt["commands_summary"]) or "Chưa có lệnh được ghi nhận."
    rev_sum = receipt["review_summary"]
    review_row = (
        f"Revision `{rev_short}`, passes={rev_sum.get('passes_completed', 0)}, "
        f"gate=`{receipt['gates']['review']}`, unresolved={len(rev_sum.get('unresolved_blocking', []))}"
    )
    verifier_row = f"gate=`{receipt['gates']['verifier']}` on `{rev_short}`"
    reasons = receipt["failures"] + receipt["blockers"]
    reason_text = "; ".join(reasons) if reasons else "Đầy đủ bằng chứng verifier, review 2 lượt độc lập và không còn finding chặn."

    lines = [
        f"# Gate Receipt — {receipt['task_id']} (`{rev_short}`)",
        "",
        f"- **Base SHA**: `{receipt['base_sha']}`",
        f"- **Candidate SHA**: `{receipt['candidate_sha']}`",
        f"- **Generated (UTC)**: `{receipt['generated_at_utc']}`",
        "",
        "| Nội dung | Bằng chứng thực thi |",
        "| :--- | :--- |",
        f"| **Hành vi được bảo vệ** | {receipt['behavior_summary']} |",
        f"| **RED trước sửa** | {red_row} |",
        f"| **GREEN sau sửa** | {green_row} |",
        f"| **Kiểm tra liên quan** | {cmds_row} |",
        f"| **Review gate** | {review_row} |",
        f"| **Verifier gate** | {verifier_row} |",
        f"| **Kết luận** | **`{receipt['conclusion']}`** — {reason_text} |",
        "",
    ]
    return "\n".join(lines)


def _load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate immutable task gate receipt.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--phase", type=int, required=True, choices=range(0, 7))
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--verifier-json", type=Path, required=True)
    parser.add_argument("--review-json", type=Path, default=None)
    parser.add_argument("--bugfix", action="store_true")
    parser.add_argument("--red-proof-json", type=Path, default=None)
    parser.add_argument("--dispositions-json", type=Path, default=None)
    parser.add_argument("--debate-json", type=Path, default=None)
    parser.add_argument("--behavior", type=str, default="")
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out_dir = args.out_dir or (REPO_ROOT / "harness" / "runs" / f"{args.task_id}-{args.candidate_sha[:8]}")
    out_dir.mkdir(parents=True, exist_ok=True)

    artifact_hashes = {
        "verifier_json_sha256": _sha256_file(args.verifier_json),
        "review_json_sha256": _sha256_file(args.review_json),
        "red_proof_json_sha256": _sha256_file(args.red_proof_json),
        "dispositions_json_sha256": _sha256_file(args.dispositions_json),
        "debate_json_sha256": _sha256_file(args.debate_json),
    }

    receipt = evaluate_receipt(
        task_id=args.task_id,
        phase=args.phase,
        base_sha=args.base_sha,
        candidate_sha=args.candidate_sha,
        verifier_data=_load_optional_json(args.verifier_json),
        review_data=_load_optional_json(args.review_json),
        is_bugfix=args.bugfix,
        red_proof=_load_optional_json(args.red_proof_json),
        dispositions=_load_optional_json(args.dispositions_json),
        debate_data=_load_optional_json(args.debate_json),
        behavior_summary=args.behavior,
        artifact_hashes=artifact_hashes,
    )

    (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (out_dir / "receipt.md").write_text(render_receipt_markdown(receipt), encoding="utf-8")
    print(f"[receipt] conclusion={receipt['conclusion']} out={out_dir}")

    if receipt["conclusion"] == "VERIFIED":
        return 0
    if receipt["conclusion"] == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
