"""Immutable gate receipt generator and final task adjudicator.

Implements pstack-codex-room.md §7 (immutable receipts with artifact SHA-256)
and test-required.md §§5, 7, 8, 9 (RED->GREEN bugfix proof, 2-pass review
consolidation, verifier gate, and VERIFIED/FAILED/BLOCKED verdict table).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts.schema_utils import is_hex_sha40, is_strict_bool, is_strict_int
except ImportError:
    from schema_utils import is_hex_sha40, is_strict_bool, is_strict_int

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

VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})
EVIDENCE_REF_RE = re.compile(r"^(?P<path>[^\s:]+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def _sha256_file(path: Path | None) -> str | None:
    if not path or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_test_target(command_text: str) -> str:
    tokens = command_text.strip().split()
    targets = [tok for tok in tokens if ("test_" in tok or "/tests" in tok or "\\tests" in tok or ".py" in tok)]
    if targets:
        return " ".join(tok.replace("\\", "/") for tok in targets)
    return " ".join(tokens)


def _validate_red_proof(
    red_proof: dict[str, Any] | None,
    *,
    base_sha: str,
    candidate_sha: str,
) -> tuple[bool, str]:
    if not isinstance(red_proof, dict):
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (missing red_proof)."

    red_exit = red_proof.get("red_exit_code")
    green_exit = red_proof.get("green_exit_code")
    red_sha = red_proof.get("red_sha")
    green_sha = red_proof.get("green_sha")
    reason = str(red_proof.get("red_failure_reason", "")).strip()

    if not is_strict_int(red_exit) or red_exit == 0:
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (red_exit_code must be non-zero int)."
    if not reason:
        return False, "BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (missing failure reason)."
    lowered = reason.lower()
    if any(marker in lowered for marker in ENV_OR_IMPORT_ERROR_MARKERS):
        return False, f"BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (invalid RED reason: {reason})."
    if not is_hex_sha40(red_sha):
        return False, f"BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi (invalid red_sha {red_sha!r})."
    if red_sha != base_sha:
        return False, f"BLOCKED — red_sha ({red_sha}) does not match base_sha ({base_sha})."
    if not is_hex_sha40(green_sha):
        return False, f"BLOCKED — missing or invalid green_sha ({green_sha!r})."
    if green_sha != candidate_sha:
        return False, f"BLOCKED — GREEN test ran on mismatched SHA ({green_sha} != {candidate_sha})."
    if not is_strict_int(green_exit) or green_exit != 0:
        return False, f"FAILED — regression test failed after fix (green_exit_code={green_exit})."

    if "red_test_command" in red_proof or "green_test_command" in red_proof:
        red_cmd = red_proof.get("red_test_command")
        green_cmd = red_proof.get("green_test_command")
        if not isinstance(red_cmd, str) or not red_cmd.strip() or not isinstance(green_cmd, str) or not green_cmd.strip():
            return False, "BLOCKED — red_test_command and green_test_command must both be non-empty strings."
        if _extract_test_target(red_cmd) != _extract_test_target(green_cmd):
            return False, "BLOCKED — red_test_command and green_test_command target different tests."

    return True, f"RED on {red_sha[:12]} (exit={red_exit}: {reason}) -> GREEN on {candidate_sha[:12]} (exit=0)"


def _index_dispositions(dispositions: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(dispositions, dict):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for item in dispositions.get("dispositions", []):
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id", "")).strip()
        if fid:
            indexed[fid] = item
    return indexed


def _verify_evidence_ref(evidence_ref: str, candidate_sha: str, repo_root: Path) -> tuple[bool, str]:
    match = EVIDENCE_REF_RE.fullmatch(evidence_ref.strip())
    if not match:
        return False, f"invalid evidence_ref format {evidence_ref!r} (expected '<repo_rel_path>:<line>' or '<path>:<start>-<end>')"

    raw_path = match.group("path").replace("\\", "/")
    line_start = int(match.group("start"))
    line_end = int(match.group("end") or line_start)
    if line_start < 1 or line_end < line_start:
        return False, f"invalid line range {line_start}-{line_end} in {evidence_ref!r}"

    rel_parts = Path(raw_path).parts
    if raw_path.startswith("/") or ".." in rel_parts or ":" in raw_path:
        return False, f"unsafe evidence_ref path {raw_path!r}"

    cat = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{candidate_sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if cat.returncode == 0:
        blob = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{candidate_sha}:{raw_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if blob.returncode != 0:
            return False, f"evidence_ref file {raw_path!r} does not exist at commit {candidate_sha[:12]}"
        total_lines = len(blob.stdout.splitlines())
    else:
        resolved_root = repo_root.resolve()
        target = (resolved_root / raw_path).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            return False, f"evidence_ref file {raw_path!r} does not exist"
        total_lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())

    if line_end > total_lines:
        return False, f"evidence_ref line {line_end} exceeds {raw_path} line count ({total_lines})"
    return True, ""


def _evaluate_review_gate(
    review_data: dict[str, Any] | None,
    candidate_sha: str,
    dispositions: dict[str, Any] | None,
    repo_root: Path = REPO_ROOT,
) -> tuple[str, list[str], dict[str, Any]]:
    if not isinstance(review_data, dict):
        return "BLOCKED", ["Missing consolidated Codex Room review JSON."], {"passes_completed": 0}

    passes = review_data.get("passes_completed", 0)
    independent = review_data.get("independent_contexts")
    rev_sha = review_data.get("revision_sha")
    gate_status = review_data.get("review_gate_status")

    if not is_strict_int(passes) or passes < 2:
        return "BLOCKED", [f"Review gate requires integer passes_completed >= 2, got {passes!r}."], {"passes_completed": passes}
    if not is_strict_bool(independent) or independent is not True:
        return "BLOCKED", [f"Review passes did not run in independent contexts (got {independent!r})."], {"passes_completed": passes}
    if not is_hex_sha40(rev_sha) or rev_sha != candidate_sha:
        return (
            "BLOCKED",
            [f"Review revision_sha mismatch ({rev_sha!r} != {candidate_sha!r}); stale or invalid review cannot approve."],
            {"passes_completed": passes, "revision_sha": rev_sha},
        )
    if gate_status not in {"PASSED", "CHANGES_REQUESTED"}:
        err_list = list(review_data.get("errors", [])) if isinstance(review_data.get("errors"), list) else []
        if not err_list:
            err_list = [f"Review gate status must be PASSED or CHANGES_REQUESTED, got {gate_status!r}."]
        return "BLOCKED", err_list, {"passes_completed": passes, "revision_sha": rev_sha}

    raw_findings = review_data.get("consolidated_findings")
    if not isinstance(raw_findings, list):
        return "BLOCKED", ["Review consolidated_findings must be a list."], {"passes_completed": passes, "revision_sha": rev_sha}

    disp_map = _index_dispositions(dispositions)
    unresolved: list[str] = []
    resolved: list[dict[str, Any]] = []
    effective_blocking_count = 0

    for finding in raw_findings:
        if not isinstance(finding, dict):
            return "BLOCKED", ["Malformed non-dict item in consolidated_findings."], {"passes_completed": passes, "revision_sha": rev_sha}
        sev = finding.get("severity")
        if not isinstance(sev, str) or sev.lower() not in VALID_SEVERITIES:
            return (
                "BLOCKED",
                [f"Invalid finding severity {sev!r} for finding {finding.get('id')!r}."],
                {"passes_completed": passes, "revision_sha": rev_sha},
            )
        blocking_flag = finding.get("blocking")
        if not is_strict_bool(blocking_flag):
            return (
                "BLOCKED",
                [f"Finding {finding.get('id')!r} has non-boolean blocking flag {blocking_flag!r}."],
                {"passes_completed": passes, "revision_sha": rev_sha},
            )
        effective_blocking = (sev.lower() in BLOCKING_SEVERITIES) or (blocking_flag is True)
        if not effective_blocking:
            continue
        effective_blocking_count += 1

        candidate_ids = [str(finding.get("id", "")), *[str(s) for s in finding.get("source_ids", [])]]
        matched_disp = next((disp_map[cid] for cid in candidate_ids if cid in disp_map), None)
        if not matched_disp:
            unresolved.append(f"{finding.get('id')}: {finding.get('title')} (OPEN)")
            continue

        if "candidate_sha" in matched_disp and matched_disp.get("candidate_sha") != candidate_sha:
            unresolved.append(
                f"{finding.get('id')}: disposition candidate_sha mismatch "
                f"({matched_disp.get('candidate_sha')!r} != {candidate_sha!r})"
            )
            continue

        d_status = str(matched_disp.get("status", "")).upper()
        rationale = str(matched_disp.get("rationale", "")).strip()
        evidence_ref = str(matched_disp.get("evidence_ref", "")).strip()

        if d_status == "REFUTED_WITH_EVIDENCE":
            if not rationale or not evidence_ref:
                unresolved.append(f"{finding.get('id')}: REFUTED_WITH_EVIDENCE requires rationale and evidence_ref")
                continue
            ref_ok, ref_err = _verify_evidence_ref(evidence_ref, candidate_sha, repo_root)
            if not ref_ok:
                unresolved.append(f"{finding.get('id')}: {ref_err}")
                continue
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

    if gate_status == "CHANGES_REQUESTED" and effective_blocking_count == 0:
        return (
            "BLOCKED",
            ["Inconsistent review: review_gate_status='CHANGES_REQUESTED' with 0 blocking findings."],
            {"passes_completed": passes, "revision_sha": rev_sha},
        )

    summary = {
        "passes_completed": passes,
        "independent_contexts": independent,
        "revision_sha": rev_sha,
        "review_backend": review_data.get("review_backend", "codex"),
        "review_model": review_data.get("review_model"),
        "fallback_used": bool(review_data.get("fallback_used", False)),
        "fallback_reason": review_data.get("fallback_reason"),
        "total_findings": len(raw_findings),
        "resolved_blocking": resolved,
        "unresolved_blocking": unresolved,
    }
    if unresolved:
        return "FAILED", unresolved, summary
    return "PASSED", [], summary


def _evaluate_verifier_gate(
    verifier_data: dict[str, Any] | None,
    review_data: dict[str, Any] | None,
    candidate_sha: str,
    *,
    require_frozen: bool = False,
) -> tuple[str, list[str], list[str]]:
    if not isinstance(verifier_data, dict):
        return "BLOCKED", [], ["Missing build_verifier output JSON."]

    v_status = str(verifier_data.get("status", "FAILED"))
    v_sha = verifier_data.get("git_sha")
    if v_status == "FAILED":
        return "FAILED", ["build_verifier reported FAILED."], []
    if not is_hex_sha40(v_sha) or v_sha != candidate_sha:
        return "BLOCKED", [], [f"build_verifier git_sha mismatch ({v_sha!r} != {candidate_sha!r})."]
    if v_status == "BLOCKED":
        return "BLOCKED", [], ["build_verifier reported BLOCKED (missing required phase target)."]
    if v_status != "VERIFIED":
        return "FAILED", [f"Unexpected build_verifier status: {v_status!r}"], []

    worktree_dirty = verifier_data.get("worktree_dirty")
    if not is_strict_bool(worktree_dirty) or worktree_dirty is not False:
        return "BLOCKED", [], [f"build_verifier worktree_dirty must be boolean false, got {worktree_dirty!r}."]

    steps = verifier_data.get("steps")
    if not isinstance(steps, list) or len(steps) < 1:
        return "BLOCKED", [], ["build_verifier must record at least one executed step."]

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            return "BLOCKED", [], [f"build_verifier step[{idx}] is malformed."]
        s_status = step.get("status")
        s_exit = step.get("exit_code")
        s_name = step.get("name", f"step[{idx}]")
        if s_status == "FAILED" or (is_strict_int(s_exit) and s_exit != 0):
            return "FAILED", [f"build_verifier step '{s_name}' failed (status={s_status!r}, exit_code={s_exit!r})."], []
        if s_status != "PASSED" or not is_strict_int(s_exit) or s_exit != 0:
            return "BLOCKED", [], [f"build_verifier step '{s_name}' did not pass cleanly (status={s_status!r}, exit_code={s_exit!r})."]

    if require_frozen:
        if verifier_data.get("source_mode") != "frozen_commit_archive":
            return (
                "BLOCKED",
                [],
                [f"Frozen archive required: verifier source_mode={verifier_data.get('source_mode')!r}."],
            )
        v_tree_hash = verifier_data.get("frozen_tree_sha256")
        if not isinstance(v_tree_hash, str) or len(v_tree_hash) != 64:
            return "BLOCKED", [], ["Frozen archive required: verifier missing frozen_tree_sha256."]
        if isinstance(review_data, dict):
            r_tree_hash = review_data.get("frozen_tree_sha256")
            if r_tree_hash is not None and r_tree_hash != v_tree_hash:
                return (
                    "BLOCKED",
                    [],
                    [f"Frozen tree hash mismatch between verifier ({v_tree_hash[:12]}) and review ({str(r_tree_hash)[:12]})."],
                )

    return "VERIFIED", [], []


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
    repo_root: Path | None = None,
    require_frozen: bool = False,
) -> dict[str, Any]:
    """Evaluate all gates and build the immutable receipt payload."""
    active_root = repo_root or REPO_ROOT
    blockers: list[str] = []
    failures: list[str] = []

    if not is_hex_sha40(base_sha):
        blockers.append(f"Invalid base_sha: expected 40-char lowercase hex, got {base_sha!r}.")
    if not is_hex_sha40(candidate_sha):
        blockers.append(f"Invalid candidate_sha: expected 40-char lowercase hex, got {candidate_sha!r}.")
    if is_hex_sha40(base_sha) and is_hex_sha40(candidate_sha) and base_sha == candidate_sha:
        blockers.append(f"base_sha and candidate_sha must differ (both {candidate_sha}).")

    # 1. Verifier gate
    verifier_gate, v_failures, v_blockers = _evaluate_verifier_gate(
        verifier_data,
        review_data,
        candidate_sha,
        require_frozen=require_frozen,
    )
    failures.extend(v_failures)
    blockers.extend(v_blockers)

    # 2. Bugfix RED -> GREEN gate
    if is_bugfix:
        red_ok, red_msg = _validate_red_proof(red_proof, base_sha=base_sha, candidate_sha=candidate_sha)
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
        review_data,
        candidate_sha,
        dispositions,
        repo_root=active_root,
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
        if isinstance(s, dict)
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
    backend_tag = str(rev_sum.get("review_backend", "codex"))
    if rev_sum.get("review_model"):
        backend_tag += f"/{rev_sum['review_model']}"
    if rev_sum.get("fallback_used"):
        backend_tag += ", fallback"
    review_row = (
        f"Revision `{rev_short}`, backend=`{backend_tag}`, passes={rev_sum.get('passes_completed', 0)}, "
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


def _validate_task_manifest(manifest_path: Path, args: argparse.Namespace) -> str | None:
    if not manifest_path.is_file():
        return f"Task manifest not found: {manifest_path}"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"Invalid task manifest JSON: {exc}"
    if not isinstance(manifest, dict):
        return "Task manifest must be a JSON object."

    m_bugfix = manifest.get("is_bugfix")
    if not is_strict_bool(m_bugfix):
        return f"Task manifest missing strict boolean 'is_bugfix' (got {m_bugfix!r})."
    if m_bugfix != bool(args.bugfix):
        return f"Task manifest is_bugfix={m_bugfix} conflicts with CLI --bugfix={bool(args.bugfix)}."

    m_base = manifest.get("base_sha")
    if not is_hex_sha40(m_base) or m_base != args.base_sha:
        return f"Task manifest base_sha ({m_base!r}) does not match CLI --base-sha ({args.base_sha!r})."

    if "task_id" in manifest and manifest.get("task_id") != args.task_id:
        return f"Task manifest task_id ({manifest.get('task_id')!r}) does not match --task-id ({args.task_id!r})."

    return None


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
    parser.add_argument("--task-manifest", type=Path, default=None, help="Optional task manifest JSON verifying is_bugfix and base_sha.")
    parser.add_argument("--require-frozen", action="store_true", help="Require frozen_commit_archive verification evidence.")
    parser.add_argument("--attempt-id", type=str, default=None, help="Optional attempt identifier for default write-once path.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--behavior", type=str, default="")
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.task_manifest is not None:
        manifest_err = _validate_task_manifest(args.task_manifest, args)
        if manifest_err:
            print(f"[receipt] BLOCKED — {manifest_err}", file=sys.stderr)
            return 2

    if args.out_dir is not None:
        out_dir = args.out_dir
    else:
        attempt_stamp = args.attempt_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = REPO_ROOT / "harness" / "runs" / args.task_id / "attempts" / f"{attempt_stamp}-{args.candidate_sha[:12]}"

    receipt_json_path = out_dir / "receipt.json"
    receipt_md_path = out_dir / "receipt.md"
    if receipt_json_path.exists():
        print(f"[receipt] Write-once refusal: {receipt_json_path} already exists.", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    artifact_hashes = {
        "verifier_json_sha256": _sha256_file(args.verifier_json),
        "review_json_sha256": _sha256_file(args.review_json),
        "red_proof_json_sha256": _sha256_file(args.red_proof_json),
        "dispositions_json_sha256": _sha256_file(args.dispositions_json),
        "debate_json_sha256": _sha256_file(args.debate_json),
        "task_manifest_json_sha256": _sha256_file(args.task_manifest),
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
        repo_root=args.repo_root.resolve(),
        require_frozen=args.require_frozen,
    )

    try:
        with receipt_json_path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, indent=2) + "\n")
    except FileExistsError:
        print(f"[receipt] Write-once refusal: {receipt_json_path} already exists.", file=sys.stderr)
        return 2
    with receipt_md_path.open("x", encoding="utf-8") as fh:
        fh.write(render_receipt_markdown(receipt))

    print(f"[receipt] conclusion={receipt['conclusion']} out={out_dir}")

    if receipt["conclusion"] == "VERIFIED":
        return 0
    if receipt["conclusion"] == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
