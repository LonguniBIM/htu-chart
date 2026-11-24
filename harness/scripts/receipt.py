"""Immutable gate receipt generator and final task adjudicator.

Implements pstack-codex-room.md §7 (immutable receipts with artifact SHA-256)
and test-required.md §§5, 7, 8, 9 (RED->GREEN bugfix proof, 2-pass review
consolidation, verifier gate, and VERIFIED/FAILED/BLOCKED verdict table).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts import task_runner
    from harness.scripts.codex_room_sync import is_excluded_model, load_model_policy
    from harness.scripts.schema_utils import is_hex_sha40, is_strict_bool, is_strict_int, resolve_git_toplevel, run_managed_cmd
except ImportError:
    import task_runner
    from codex_room_sync import is_excluded_model, load_model_policy
    from schema_utils import is_hex_sha40, is_strict_bool, is_strict_int, resolve_git_toplevel, run_managed_cmd

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
HEX_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path | None) -> str | None:
    if not path or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


SAFE_STRIP_ENV_KEYS = frozenset(
    {
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "NO_COLOR",
        "FORCE_COLOR",
        "TERM",
    }
)


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and ((tok[0] == '"' and tok[-1] == '"') or (tok[0] == "'" and tok[-1] == "'")):
        return tok[1:-1]
    return tok


def _normalize_dir_arg(raw: str) -> str:
    d = raw.strip().replace("\\", "/")
    if re.fullmatch(r"/+", d):
        return "/"
    if re.fullmatch(r"[A-Za-z]:/+", d):
        return f"{d[0].upper()}:/"
    stripped = d.rstrip("/")
    if stripped.startswith("./") and len(stripped) > 2:
        stripped = stripped[2:]
    return stripped or "."


def _extract_test_target(command_text: str) -> str:
    try:
        raw_tokens = shlex.split(command_text.strip(), posix=False)
    except ValueError:
        raw_tokens = command_text.strip().split()
    tokens = [_strip_quotes(t) for t in raw_tokens if t.strip()]

    preserved_env: list[str] = []
    idx = 0
    while idx < len(tokens):
        env_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", tokens[idx])
        if not env_match:
            break
        if env_match.group(1) not in SAFE_STRIP_ENV_KEYS:
            preserved_env.append(tokens[idx])
        idx += 1

    if idx < len(tokens) and Path(tokens[idx]).name.lower() in {"uv", "uv.exe"}:
        idx += 1
        if idx < len(tokens) and tokens[idx] == "run":
            idx += 1
        while idx < len(tokens):
            if tokens[idx] in {"--directory", "-C"} and idx + 1 < len(tokens):
                norm_dir = _normalize_dir_arg(tokens[idx + 1])
                preserved_env.extend(["--directory", norm_dir])
                idx += 2
                continue
            if tokens[idx].startswith(("--directory=", "-C=")):
                dir_val = _normalize_dir_arg(tokens[idx].split("=", 1)[1])
                preserved_env.extend(["--directory", dir_val])
                idx += 1
                continue
            break

    if idx < len(tokens) and Path(tokens[idx]).name.lower() in {"python", "python3", "python.exe", "py", "py.exe"}:
        idx += 1
        if idx + 1 < len(tokens) and tokens[idx] == "-m":
            idx += 1

    if idx < len(tokens) and Path(tokens[idx]).name.lower() in {"pytest", "pytest.exe"}:
        idx += 1

    kept: list[str] = [*preserved_env]
    verbosity_flags = {"-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-s", "--no-header", "-ra", "-rx", "-rA"}
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in verbosity_flags or tok.startswith(("--tb=", "--capture=", "--color=")):
            idx += 1
            continue
        if tok in {"--tb", "--capture", "--color"}:
            idx += 2
            continue
        if tok in {"-k", "-m"} and idx + 1 < len(tokens):
            kept.append(tok)
            kept.append(tokens[idx + 1])
            idx += 2
            continue
        kept.append(tok.replace("\\", "/"))
        idx += 1

    return " ".join(kept) if kept else " ".join(tokens)


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
    if not is_strict_int(green_exit):
        return False, f"BLOCKED — green_exit_code must be an integer, got {green_exit!r}."
    if green_exit != 0:
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

    try:
        git_top = resolve_git_toplevel(repo_root, timeout_sec=60.0)
    except (ValueError, TimeoutError) as exc:
        return False, f"evidence_ref requires a valid git repository: {exc}"

    try:
        cat = run_managed_cmd(["git", "-C", str(git_top), "cat-file", "-e", f"{candidate_sha}^{{commit}}"], timeout_sec=60.0)
    except subprocess.TimeoutExpired:
        return False, f"git timed out checking commit {candidate_sha[:12]}"
    if cat.returncode != 0:
        return False, f"evidence_ref commit {candidate_sha[:12]} does not exist in git repository"

    try:
        obj_type = run_managed_cmd(
            ["git", "-C", str(git_top), "cat-file", "-t", f"{candidate_sha}:{raw_path}"],
            timeout_sec=60.0,
        )
    except subprocess.TimeoutExpired:
        return False, f"git timed out checking object type for {raw_path!r}"
    if obj_type.returncode != 0 or obj_type.stdout.strip() != "blob":
        return False, f"evidence_ref {raw_path!r} does not point to a file blob at commit {candidate_sha[:12]}"

    try:
        blob = run_managed_cmd(
            ["git", "-C", str(git_top), "show", f"{candidate_sha}:{raw_path}"],
            timeout_sec=60.0,
        )
    except subprocess.TimeoutExpired:
        return False, f"git timed out reading {raw_path!r} at commit {candidate_sha[:12]}"
    if blob.returncode != 0:
        return False, f"evidence_ref file {raw_path!r} does not exist at commit {candidate_sha[:12]}"
    total_lines = len(blob.stdout.splitlines())

    if line_end > total_lines:
        return False, f"evidence_ref line {line_end} exceeds {raw_path} line count ({total_lines})"
    return True, ""


def _evaluate_review_gate(
    review_data: dict[str, Any] | None,
    candidate_sha: str,
    dispositions: dict[str, Any] | None,
    repo_root: Path = REPO_ROOT,
    excluded_prefixes: tuple[str, ...] = (),
) -> tuple[str, list[str], dict[str, Any]]:
    if not isinstance(review_data, dict):
        return "BLOCKED", ["Missing consolidated Codex Room review JSON."], {"passes_completed": 0}

    rev_model = review_data.get("review_model")
    if isinstance(rev_model, str) and rev_model.strip() and is_excluded_model(
        rev_model, excluded_prefixes
    ):
        return (
            "BLOCKED",
            [f"Review model {rev_model!r} is excluded by model-policy.json (MOD-001)."],
            {"passes_completed": review_data.get("passes_completed", 0), "review_model": rev_model},
        )

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
    phase: int,
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

    v_phase = verifier_data.get("phase")
    if not is_strict_int(v_phase) or v_phase != phase:
        return "BLOCKED", [], [f"build_verifier phase mismatch ({v_phase!r} != {phase})."]

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

    required_steps: set[str] = {"arch_check", "harness_ruff", "harness_pytest"}
    if require_frozen or verifier_data.get("source_mode") == "frozen_commit_archive":
        required_steps.add("upstream_fork_clean")
    if phase >= 1:
        required_steps.update({"backend_ruff", "backend_pytest"})
    if phase >= 3:
        required_steps.update({"frontend_typecheck", "frontend_build"})

    passed_step_names = {str(s.get("name", "")) for s in steps}
    missing_steps = sorted(required_steps - passed_step_names)
    if missing_steps:
        return (
            "BLOCKED",
            [],
            [f"build_verifier missing required step(s) for phase {phase}: {', '.join(missing_steps)}."],
        )

    if require_frozen:
        if verifier_data.get("source_mode") != "frozen_commit_archive":
            return (
                "BLOCKED",
                [],
                [f"Frozen archive required: verifier source_mode={verifier_data.get('source_mode')!r}."],
            )
        v_tree_hash = verifier_data.get("frozen_tree_sha256")
        if not isinstance(v_tree_hash, str) or not HEX_SHA64_RE.fullmatch(v_tree_hash):
            return "BLOCKED", [], ["Frozen archive required: verifier missing valid 64-hex frozen_tree_sha256."]
        if isinstance(review_data, dict):
            r_tree_hash = review_data.get("frozen_tree_sha256")
            if not isinstance(r_tree_hash, str) or not HEX_SHA64_RE.fullmatch(r_tree_hash):
                return "BLOCKED", [], ["Frozen archive required: review missing valid 64-hex frozen_tree_sha256."]
            if r_tree_hash != v_tree_hash:
                return (
                    "BLOCKED",
                    [],
                    [f"Frozen tree hash mismatch between verifier ({v_tree_hash[:12]}) and review ({r_tree_hash[:12]})."],
                )

    return "VERIFIED", [], []


def _evaluate_ledger_and_attempts(
    *,
    task_id: str,
    candidate_sha: str,
    dispositions: dict[str, Any] | None,
    active_root: Path,
    ledger_data: dict[str, Any] | None,
    tracker_data: dict[str, Any] | None,
    require_ledger: bool,
) -> list[str]:
    """Enforce G4/candidate ceilings and ledger state/SHA invariants for G5."""
    blockers: list[str] = []
    breakers = task_runner.load_circuit_breakers(repo_root=active_root)
    max_g4_root = int(breakers.get("max_g4_rounds_per_root_task", 6))
    max_total_cand = int(breakers.get("max_total_candidates_per_root_task", 6))

    attempts_dir = active_root / "harness" / "runs" / task_id / "attempts"
    fs_rounds, _ = task_runner.count_review_attempts([attempts_dir])
    effective_tracker = tracker_data
    if effective_tracker is None:
        try:
            t_path = task_runner.default_g4_tracker_path(task_id, repo_root=active_root)
            if t_path.is_file():
                effective_tracker = task_runner.load_g4_tracker(t_path, task_id=task_id)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            effective_tracker = None
    tracker_rounds = 0
    if isinstance(effective_tracker, dict):
        tracker_rounds = int(effective_tracker.get("completed_rounds", 0)) + len(
            effective_tracker.get("in_flight", {})
        )
    effective_attempts = max(fs_rounds, tracker_rounds)
    if effective_attempts > max_g4_root:
        blockers.append(
            f"Review attempts ({effective_attempts}) exceed "
            f"max_g4_rounds_per_root_task ({max_g4_root})."
        )

    if require_ledger and ledger_data is None:
        blockers.append("Missing required ledger.json for G5 verification.")
        return blockers

    if ledger_data is None:
        return blockers

    state = ledger_data.get("state")
    halted_states = {"PAUSED_BUDGET", "BLOCKED", "BLOCKED_BRIEF", "BLOCKED_AUTH", "BLOCKED_INFRA"}
    if state in halted_states:
        blockers.append(
            f"Task ledger is halted in state {state!r}; cannot issue VERIFIED receipt."
        )

    if ledger_data.get("active_g4_reservations"):
        blockers.append("Task ledger still has active in-flight G4 reservation(s).")

    g4_total = int(ledger_data.get("g4_rounds_total", 0))
    total_cand = int(ledger_data.get("total_candidates", 0))
    if g4_total > max_g4_root or total_cand > max_total_cand:
        blockers.append(
            f"Task ledger exceeds ceiling (g4_rounds_total={g4_total}/{max_g4_root}, "
            f"total_candidates={total_cand}/{max_total_cand})."
        )

    budget_limit = float(ledger_data.get("budget_limit_usd", 0.0) or 0.0)
    spent_usd = float(ledger_data.get("spent_api_equiv_usd", 0.0) or 0.0)
    if budget_limit > 0 and round(spent_usd, 8) > round(budget_limit, 8):
        blockers.append(
            f"Task ledger spent_api_equiv_usd (${spent_usd:.4f}) exceeds "
            f"budget_limit_usd (${budget_limit:.4f})."
        )

    has_dispositions = isinstance(dispositions, dict) and bool(dispositions.get("dispositions"))
    ready_or_verified = (
        state in {"READY_FOR_ACCEPTANCE", "VERIFIED"}
        and ledger_data.get("candidate_sha") == candidate_sha
    )
    resolved_via_disp = (
        has_dispositions
        and state in {"ACTIVE_PEER_G1", "NEEDS_REPLAN"}
        and ledger_data.get("last_g4_candidate_sha") == candidate_sha
    )
    if not (ready_or_verified or resolved_via_disp):
        blockers.append(
            f"Task ledger state/SHA mismatch for G5 (state={state!r}, "
            f"candidate_sha={ledger_data.get('candidate_sha')!r}, "
            f"last_g4_candidate_sha={ledger_data.get('last_g4_candidate_sha')!r})."
        )
    return blockers


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
    ledger_data: dict[str, Any] | None = None,
    tracker_data: dict[str, Any] | None = None,
    require_ledger: bool = False,
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
        phase=phase,
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
    _, excluded_prefixes, policy_err = load_model_policy(active_root, require_exists=False)
    if policy_err:
        blockers.append(f"Model policy error: {policy_err}")
    review_gate, review_reasons, review_summary = _evaluate_review_gate(
        review_data,
        candidate_sha,
        dispositions,
        repo_root=active_root,
        excluded_prefixes=excluded_prefixes,
    )
    if review_gate == "FAILED":
        failures.extend(review_reasons)
    elif review_gate == "BLOCKED":
        blockers.extend(review_reasons)

    # 4. Ledger & filesystem G4/candidate ceiling verification
    blockers.extend(
        _evaluate_ledger_and_attempts(
            task_id=task_id,
            candidate_sha=candidate_sha,
            dispositions=dispositions,
            active_root=active_root,
            ledger_data=ledger_data,
            tracker_data=tracker_data,
            require_ledger=require_ledger,
        )
    )

    if failures:
        conclusion = "FAILED"
    elif blockers:
        conclusion = "BLOCKED"
    else:
        conclusion = "VERIFIED"

    raw_steps = verifier_data.get("steps") if isinstance(verifier_data, dict) else None
    verifier_steps = raw_steps if isinstance(raw_steps, list) else []
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
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Optional task ledger.json path for G5 ceiling and state checks.",
    )
    parser.add_argument(
        "--require-ledger",
        action="store_true",
        help="Require a valid ledger.json and fail closed (BLOCKED) if missing.",
    )
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
        out_dir = args.out_dir.resolve()
    else:
        attempt_stamp = args.attempt_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
        out_dir = (
            REPO_ROOT / "harness" / "runs" / args.task_id / "attempts" / f"{attempt_stamp}-{args.candidate_sha[:12]}"
        ).resolve()

    receipt_json_path = out_dir / "receipt.json"
    receipt_md_path = out_dir / "receipt.md"
    if receipt_json_path.exists():
        print(f"[receipt] Write-once refusal: {receipt_json_path} already exists.", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    active_root = args.repo_root.resolve()
    auto_ledger = active_root / "harness" / "runs" / args.task_id / "ledger.json"
    ledger_path: Path | None
    if args.ledger is not None:
        ledger_path = args.ledger.resolve()
    elif auto_ledger.is_file():
        ledger_path = auto_ledger.resolve()
    else:
        ledger_path = None

    tracker_path: Path | None = None
    try:
        cand_tracker = task_runner.default_g4_tracker_path(args.task_id, repo_root=active_root)
        if cand_tracker.is_file():
            tracker_path = cand_tracker
    except ValueError:
        tracker_path = None

    dispositions_obj = _load_optional_json(args.dispositions_json)
    explicit_missing = args.ledger is not None and (
        ledger_path is None or not ledger_path.is_file()
    )

    def _eval_write_and_transition_locked() -> tuple[dict[str, Any] | None, int | None]:
        ledger_data: dict[str, Any] | None = None
        ledger_load_err: str | None = None
        if ledger_path is not None and ledger_path.is_file():
            try:
                ledger_data = task_runner.load_ledger(ledger_path, repo_root=active_root)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                ledger_load_err = f"Invalid ledger.json at {ledger_path}: {exc}"

        tracker_data: dict[str, Any] | None = None
        if tracker_path is not None and tracker_path.is_file():
            try:
                tracker_data = task_runner.load_g4_tracker(tracker_path, task_id=args.task_id)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                if not ledger_load_err:
                    ledger_load_err = f"Invalid g4-tracker.json at {tracker_path}: {exc}"

        artifact_hashes: dict[str, str | None] = {
            "verifier_json_sha256": _sha256_file(args.verifier_json),
            "review_json_sha256": _sha256_file(args.review_json),
            "red_proof_json_sha256": _sha256_file(args.red_proof_json),
            "dispositions_json_sha256": _sha256_file(args.dispositions_json),
            "debate_json_sha256": _sha256_file(args.debate_json),
            "task_manifest_json_sha256": _sha256_file(args.task_manifest),
        }
        if ledger_path is not None and ledger_path.is_file():
            artifact_hashes["ledger_json_sha256"] = _sha256_file(ledger_path)

        receipt = evaluate_receipt(
            task_id=args.task_id,
            phase=args.phase,
            base_sha=args.base_sha,
            candidate_sha=args.candidate_sha,
            verifier_data=_load_optional_json(args.verifier_json),
            review_data=_load_optional_json(args.review_json),
            is_bugfix=args.bugfix,
            red_proof=_load_optional_json(args.red_proof_json),
            dispositions=dispositions_obj,
            debate_data=_load_optional_json(args.debate_json),
            behavior_summary=args.behavior,
            artifact_hashes=artifact_hashes,
            repo_root=active_root,
            require_frozen=args.require_frozen,
            ledger_data=ledger_data,
            tracker_data=tracker_data,
            require_ledger=bool(args.require_ledger or explicit_missing),
        )
        if ledger_load_err and ledger_load_err not in receipt["blockers"]:
            receipt["blockers"].append(ledger_load_err)
            if not receipt["failures"]:
                receipt["conclusion"] = "BLOCKED"

        try:
            with receipt_json_path.open("x", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, indent=2) + "\n")
        except FileExistsError:
            print(
                f"[receipt] Write-once refusal: {receipt_json_path} already exists.",
                file=sys.stderr,
            )
            return None, 2
        with receipt_md_path.open("x", encoding="utf-8") as fh:
            fh.write(render_receipt_markdown(receipt))

        if (
            receipt["conclusion"] == "VERIFIED"
            and ledger_path is not None
            and ledger_path.is_file()
        ):
            cur_ledger = task_runner.load_ledger(ledger_path, repo_root=active_root)
            if cur_ledger["state"] == "READY_FOR_ACCEPTANCE":
                _, g5_rc = task_runner.record_gate_outcome(
                    ledger_path,
                    gate="G5",
                    outcome="VERIFIED",
                    candidate_sha=args.candidate_sha,
                )
                if g5_rc != 0:
                    return None, 2
            elif (
                isinstance(dispositions_obj, dict)
                and dispositions_obj.get("dispositions")
                and cur_ledger["state"] in {"ACTIVE_PEER_G1", "NEEDS_REPLAN"}
                and cur_ledger.get("last_g4_candidate_sha") == args.candidate_sha
            ):
                cur_ledger["state"] = "VERIFIED"
                cur_ledger["candidate_sha"] = args.candidate_sha
                cur_ledger["consecutive_failed_candidates"] = 0
                cur_ledger["transient_retries_in_stage"] = 0
                task_runner._append_event(
                    cur_ledger,
                    "G5_VERIFIED",
                    {"candidate_sha": args.candidate_sha, "via_dispositions": True},
                )
                task_runner.save_ledger(ledger_path, cur_ledger)
        return receipt, None

    if tracker_path is not None:
        with task_runner._ledger_lock(tracker_path):
            if ledger_path is not None:
                with task_runner._ledger_lock(ledger_path):
                    receipt, err_rc = _eval_write_and_transition_locked()
            else:
                receipt, err_rc = _eval_write_and_transition_locked()
    elif ledger_path is not None:
        with task_runner._ledger_lock(ledger_path):
            receipt, err_rc = _eval_write_and_transition_locked()
    else:
        receipt, err_rc = _eval_write_and_transition_locked()

    if err_rc is not None or receipt is None:
        return err_rc if err_rc is not None else 2

    print(f"[receipt] conclusion={receipt['conclusion']} out={out_dir}")

    if receipt["conclusion"] == "VERIFIED":
        return 0
    if receipt["conclusion"] == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
