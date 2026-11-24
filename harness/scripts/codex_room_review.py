"""Independent 2-pass Codex Room Lead Review and finding consolidator.

Runs two isolated read-only review passes on an immutable commit SHA:
  Pass 1: Correctness, spec adherence, concurrency, and test strength.
  Pass 2: Security, Prop Firm Read-Only, F01-F08 & R01-R06 reliability.

Prioritizes Codex CLI (`codex exec -s read-only`), with optional fallback to
Antigravity CLI (`agy --mode plan --sandbox --model claude-opus-5-5-medium`)
when Codex CLI quota is exhausted or unavailable (`--backend auto`).

Enforces test-required.md §7: no stale-SHA approval, no majority-vote
dismissal (any blocking finding from either pass keeps status OPEN).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts.codex_room_sync import (
        EXPECTED_REVIEW_MODEL,
        is_excluded_model,
        load_model_policy,
        verify_role,
    )
    from harness.scripts.schema_utils import (
        cleanup_readonly_tree,
        close_job_handle,
        compute_tree_sha256,
        is_hex_sha40,
        is_strict_int,
        materialize_frozen_tree,
        resolve_git_toplevel,
        run_managed_cmd,
        spawn_managed_process,
        terminate_process_tree,
        validate_instance_against_schema,
    )
except ImportError:
    from codex_room_sync import (
        EXPECTED_REVIEW_MODEL,
        is_excluded_model,
        load_model_policy,
        verify_role,
    )
    from schema_utils import (
        cleanup_readonly_tree,
        close_job_handle,
        compute_tree_sha256,
        is_hex_sha40,
        is_strict_int,
        materialize_frozen_tree,
        resolve_git_toplevel,
        run_managed_cmd,
        spawn_managed_process,
        terminate_process_tree,
        validate_instance_against_schema,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_ROOM_DIR = REPO_ROOT / "harness" / "codex-room"
SCHEMA_PATH = CODEX_ROOM_DIR / "schemas" / "review-output.schema.json"
PROMPTS_DIR = CODEX_ROOM_DIR / "prompts"

DEFAULT_AGY_FALLBACK_MODEL = "claude-opus-5-5-medium"
AGY_INLINE_PROMPT_CHAR_LIMIT = 16000
CODEX_QUOTA_ERROR_MARKERS = (
    "usage_limit_reached",
    "insufficient_quota",
    "rate_limit_exceeded",
    "rate_limit",
    "quota",
    "too many requests",
    "429",
    "usage limit",
    "billing",
    "credit balance",
    "exceeded your current quota",
)

SEVERITY_RANK = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}
BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})


def validate_strict_json_schema(schema: Any, path: str = "$") -> list[str]:
    """Verify JSON Schema satisfies OpenAI Structured Outputs strict constraints."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    schema_type = schema.get("type")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        props = schema.get("properties", {})
        req = schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(req, list):
            errors.append(f"{path}: properties must be object and required must be array")
        else:
            missing_req = [k for k in props if k not in req]
            if missing_req:
                errors.append(f"{path}: missing required keys {missing_req}")
            for k, sub in props.items():
                errors.extend(validate_strict_json_schema(sub, f"{path}.properties.{k}"))
    elif schema_type == "array":
        if "items" not in schema:
            errors.append(f"{path}: array schema missing items")
        else:
            errors.extend(validate_strict_json_schema(schema["items"], f"{path}.items"))
    return errors


def resolve_codex_command() -> list[str]:
    """Resolve Codex CLI executable safely across Windows (.cmd/.exe/.ps1) and POSIX."""
    for candidate in ("codex.cmd", "codex.exe", "codex"):
        found = shutil.which(candidate)
        if found:
            if found.lower().endswith(".ps1"):
                return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", found]
            return [found]
    ps1 = shutil.which("codex.ps1")
    if ps1:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    raise FileNotFoundError("Codex CLI (`codex`) not found on PATH.")


def resolve_agy_command() -> list[str]:
    """Resolve Antigravity CLI (`agy`) executable safely across Windows and POSIX."""
    for candidate in ("agy.exe", "agy.cmd", "agy"):
        found = shutil.which(candidate)
        if found:
            return [found]
    raise FileNotFoundError("Antigravity CLI (`agy`) not found on PATH.")


def _detect_codex_quota_error(out_dir: Path, pass_names: tuple[str, ...] = ("pass1", "pass2")) -> str | None:
    """Inspect Codex pass logs for quota exhaustion or rate-limit errors."""
    for pass_name in pass_names:
        for suffix in (".stderr.log", ".raw.jsonl"):
            log_path = out_dir / f"{pass_name}{suffix}"
            if not log_path.is_file():
                continue
            text = log_path.read_text(encoding="utf-8", errors="replace").lower()
            for marker in CODEX_QUOTA_ERROR_MARKERS:
                if marker in text:
                    return f"codex_quota_exhausted ({pass_name}{suffix}: matched '{marker}')"
    return None


def _strip_json_fences(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_agy_structured_output(stdout_lines: list[str]) -> dict[str, Any] | None:
    """Parse structured review output from `agy --output-format stream-json` or `json`."""
    result_events: list[dict[str, Any]] = []
    for line in stdout_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result_events.append(event)

    if result_events:
        last_result = result_events[-1].get("result")
        if not isinstance(last_result, dict):
            return None
        status = str(last_result.get("status", "")).upper()
        if status and status != "SUCCESS":
            return None
        structured = last_result.get("structured_output")
        if isinstance(structured, dict):
            return structured
        response_text = last_result.get("response")
        if isinstance(response_text, str) and response_text.strip():
            try:
                parsed = json.loads(_strip_json_fences(response_text))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return None
        return None

    full_text = "".join(stdout_lines).strip()
    if not full_text:
        return None
    try:
        parsed_full = json.loads(_strip_json_fences(full_text))
    except json.JSONDecodeError:
        return None
    if isinstance(parsed_full, dict):
        if isinstance(parsed_full.get("structured_output"), dict):
            return parsed_full["structured_output"]
        if "revision_sha" in parsed_full and "lens" in parsed_full:
            return parsed_full
    return None


def _archive_failed_codex_logs(out_dir: Path, pass_names: tuple[str, ...] = ("pass1", "pass2")) -> None:
    """Preserve failed Codex pass logs before running fallback backend."""
    for pass_name in pass_names:
        for suffix in (".json", ".raw.jsonl", ".stderr.log"):
            src = out_dir / f"{pass_name}{suffix}"
            if src.is_file():
                dst = out_dir / f"{pass_name}.codex-failed{suffix}"
                src.replace(dst)


def _git_output(repo_root: Path, args: list[str], *, timeout_sec: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return run_managed_cmd(["git", "-C", str(repo_root), *args], timeout_sec=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Git command timed out after {timeout_sec:.1f}s: git {' '.join(args)}") from exc


def resolve_commit_info(repo_root: Path, commit_ref: str, base_ref: str | None) -> dict[str, Any]:
    """Resolve immutable candidate SHA, base SHA, diff stat, and dirty status."""
    git_top = resolve_git_toplevel(repo_root)
    rev = _git_output(git_top, ["rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
    if rev.returncode != 0:
        raise ValueError(f"Cannot resolve commit '{commit_ref}': {rev.stderr.strip()}")
    candidate_sha = rev.stdout.strip()

    if base_ref:
        base_rev = _git_output(git_top, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        if base_rev.returncode != 0:
            raise ValueError(f"Cannot resolve base commit '{base_ref}': {base_rev.stderr.strip()}")
        base_sha = base_rev.stdout.strip()
        diff_range = f"{base_sha}..{candidate_sha}"
    else:
        parent_rev = _git_output(git_top, ["rev-parse", "--verify", f"{candidate_sha}^"])
        if parent_rev.returncode == 0:
            base_sha = parent_rev.stdout.strip()
            diff_range = f"{base_sha}..{candidate_sha}"
        else:
            base_sha = "ROOT_COMMIT"
            diff_range = candidate_sha

    stat_proc = _git_output(git_top, ["show", "--stat", "--format=", candidate_sha])
    if base_sha != "ROOT_COMMIT":
        stat_proc = _git_output(git_top, ["diff", "--stat", diff_range])
        files_proc = _git_output(git_top, ["diff", "--name-status", diff_range])
    else:
        files_proc = _git_output(git_top, ["show", "--name-status", "--format=", candidate_sha])

    status_proc = _git_output(git_top, ["status", "--porcelain"])
    dirty_lines = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]

    return {
        "git_top": str(git_top),
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "diff_stat": stat_proc.stdout.strip() or "(no diff stat)",
        "changed_files": files_proc.stdout.strip() or "(no changed files)",
        "worktree_dirty": bool(dirty_lines),
        "dirty_files": dirty_lines,
    }


def render_prompt(template_path: Path, info: dict[str, Any], task_packet_text: str) -> str:
    """Populate a Codex Room pass prompt template with immutable commit context."""
    template = template_path.read_text(encoding="utf-8")
    replacements = {
        "{{CANDIDATE_SHA}}": info["candidate_sha"],
        "{{BASE_SHA}}": info["base_sha"],
        "{{DIFF_STAT}}": info["diff_stat"],
        "{{CHANGED_FILES}}": info["changed_files"],
        "{{FROZEN_SRC_DIR}}": str(info.get("frozen_src_dir", "")),
        "{{PATCH_PATH}}": str(info.get("patch_path") or "(none — ROOT_COMMIT)"),
        "{{TASK_PACKET}}": task_packet_text,
    }
    rendered = template
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _finding_dedup_key(f: dict[str, Any]) -> tuple[str, int, str, str]:
    line_raw = f.get("line_start", 0)
    line_num = line_raw if is_strict_int(line_raw) else 0
    return (
        str(f.get("file", "")).replace("\\", "/").strip(),
        line_num,
        str(f.get("category", "")).strip().lower(),
        _normalize_title(str(f.get("title", ""))),
    )


def consolidate_reviews(
    pass1_data: dict[str, Any] | None,
    pass2_data: dict[str, Any] | None,
    expected_sha: str,
) -> dict[str, Any]:
    """Consolidate two independent review passes on ``expected_sha``."""
    errors: list[str] = []
    if not is_hex_sha40(expected_sha):
        errors.append(f"Invalid expected_sha: must be 40-char lowercase hex, got {expected_sha!r}")
    if not isinstance(pass1_data, dict):
        errors.append("Pass 1 (correctness_and_tests) output is missing or invalid JSON.")
    if not isinstance(pass2_data, dict):
        errors.append("Pass 2 (security_and_invariants) output is missing or invalid JSON.")

    if not isinstance(pass1_data, dict) or not isinstance(pass2_data, dict):
        return {
            "revision_sha": expected_sha,
            "passes_completed": int(isinstance(pass1_data, dict)) + int(isinstance(pass2_data, dict)),
            "independent_contexts": False,
            "review_gate_status": "BLOCKED",
            "blocking_open_count": 0,
            "advisory_count": 0,
            "consolidated_findings": [],
            "test_quality_issues": [],
            "unverified_areas": [],
            "errors": errors,
        }

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    p1_schema_errors = validate_instance_against_schema(pass1_data, schema, path="$.pass1")
    p2_schema_errors = validate_instance_against_schema(pass2_data, schema, path="$.pass2")
    errors.extend(p1_schema_errors)
    errors.extend(p2_schema_errors)

    p1_sha = pass1_data.get("revision_sha")
    p2_sha = pass2_data.get("revision_sha")
    if not is_hex_sha40(p1_sha):
        errors.append(f"Pass 1 revision_sha is not a valid 40-char lowercase hex SHA: {p1_sha!r}")
    elif p1_sha != expected_sha:
        errors.append(f"Pass 1 revision_sha mismatch: expected {expected_sha}, got {p1_sha!r}")

    if not is_hex_sha40(p2_sha):
        errors.append(f"Pass 2 revision_sha is not a valid 40-char lowercase hex SHA: {p2_sha!r}")
    elif p2_sha != expected_sha:
        errors.append(f"Pass 2 revision_sha mismatch: expected {expected_sha}, got {p2_sha!r}")

    if pass1_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 1 reported BLOCKED: {pass1_data.get('summary', '')}")
    if pass2_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 2 reported BLOCKED: {pass2_data.get('summary', '')}")

    lens1 = pass1_data.get("lens")
    lens2 = pass2_data.get("lens")
    if lens1 != "correctness_and_tests":
        errors.append(f"Pass 1 lens must be 'correctness_and_tests', got {lens1!r}")
    if lens2 != "security_and_invariants":
        errors.append(f"Pass 2 lens must be 'security_and_invariants', got {lens2!r}")

    independent_contexts = bool(
        lens1 == "correctness_and_tests"
        and lens2 == "security_and_invariants"
        and not p1_schema_errors
        and not p2_schema_errors
    )

    if p1_schema_errors or p2_schema_errors:
        return {
            "revision_sha": expected_sha,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "passes_completed": 2,
            "independent_contexts": False,
            "pass1_verdict": pass1_data.get("verdict"),
            "pass2_verdict": pass2_data.get("verdict"),
            "review_gate_status": "BLOCKED",
            "blocking_open_count": 0,
            "advisory_count": 0,
            "consolidated_findings": [],
            "test_quality_issues": [],
            "unverified_areas": [],
            "errors": errors,
        }

    merged_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for pass_label, payload in (("pass1", pass1_data), ("pass2", pass2_data)):
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list):
            continue
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            sev = str(raw_finding.get("severity", "")).lower()
            if sev not in SEVERITY_RANK:
                errors.append(f"{pass_label} finding {raw_finding.get('id', '?')!r} has invalid severity {sev!r}")
                continue
            key = _finding_dedup_key(raw_finding)
            is_blocking = sev in BLOCKING_SEVERITIES
            line_start = raw_finding.get("line_start") if is_strict_int(raw_finding.get("line_start")) else 0
            line_end = raw_finding.get("line_end") if is_strict_int(raw_finding.get("line_end")) else 0
            if key not in merged_by_key:
                merged_by_key[key] = {
                    "id": raw_finding.get("id", ""),
                    "severity": sev,
                    "blocking": is_blocking,
                    "status": "OPEN",
                    "detected_by": [pass_label],
                    "source_ids": [raw_finding.get("id", "")],
                    "category": raw_finding.get("category", ""),
                    "file": str(raw_finding.get("file", "")).replace("\\", "/"),
                    "line_start": line_start,
                    "line_end": line_end,
                    "title": raw_finding.get("title", ""),
                    "behavior_affected": raw_finding.get("behavior_affected", ""),
                    "trigger_condition": raw_finding.get("trigger_condition", ""),
                    "evidence": raw_finding.get("evidence", ""),
                    "verification_method": raw_finding.get("verification_method", ""),
                    "plan_ref": raw_finding.get("plan_ref", ""),
                }
            else:
                existing = merged_by_key[key]
                if pass_label not in existing["detected_by"]:
                    existing["detected_by"].append(pass_label)
                fid = raw_finding.get("id", "")
                if fid and fid not in existing["source_ids"]:
                    existing["source_ids"].append(fid)
                if SEVERITY_RANK.get(sev, 0) > SEVERITY_RANK.get(existing["severity"], 0):
                    existing["severity"] = sev
                    existing["blocking"] = sev in BLOCKING_SEVERITIES
                existing["line_end"] = max(existing["line_end"], line_end)
                ev = str(raw_finding.get("evidence", "")).strip()
                if ev and ev not in existing["evidence"]:
                    existing["evidence"] = f"{existing['evidence']}\n[{pass_label}] {ev}".strip()

    findings_list = sorted(
        merged_by_key.values(),
        key=lambda f: (-SEVERITY_RANK.get(f["severity"], 0), f["file"], f["line_start"]),
    )

    blocking_open_count = sum(1 for f in findings_list if f["blocking"] and f["status"] == "OPEN")
    advisory_count = sum(1 for f in findings_list if not f["blocking"])

    for label, payload in (("Pass 1", pass1_data), ("Pass 2", pass2_data)):
        if payload.get("verdict") == "CHANGES_REQUESTED":
            p_findings = payload.get("findings", [])
            has_blocking = any(
                isinstance(f, dict) and str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
                for f in p_findings
            )
            if not has_blocking:
                errors.append(
                    f"{label} reported CHANGES_REQUESTED without any blocking finding (critical/high/medium)."
                )

    p1_tqi = pass1_data.get("test_quality_issues") if isinstance(pass1_data.get("test_quality_issues"), list) else []
    p2_tqi = pass2_data.get("test_quality_issues") if isinstance(pass2_data.get("test_quality_issues"), list) else []
    p1_unv = pass1_data.get("unverified_areas") if isinstance(pass1_data.get("unverified_areas"), list) else []
    p2_unv = pass2_data.get("unverified_areas") if isinstance(pass2_data.get("unverified_areas"), list) else []

    test_issues = _dedup_ordered([*p1_tqi, *p2_tqi])
    unverified = _dedup_ordered([*p1_unv, *p2_unv])

    if errors:
        gate_status = "BLOCKED"
    elif blocking_open_count == 0:
        gate_status = "PASSED"
    else:
        gate_status = "CHANGES_REQUESTED"

    return {
        "revision_sha": expected_sha,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "passes_completed": 2,
        "independent_contexts": independent_contexts,
        "pass1_verdict": pass1_data.get("verdict"),
        "pass2_verdict": pass2_data.get("verdict"),
        "review_gate_status": gate_status,
        "blocking_open_count": blocking_open_count,
        "advisory_count": advisory_count,
        "consolidated_findings": findings_list,
        "test_quality_issues": test_issues,
        "unverified_areas": unverified,
        "errors": errors,
    }


def _dedup_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def render_consolidated_markdown(report: dict[str, Any]) -> str:
    """Format consolidated Codex Room review as readable markdown."""
    backend = report.get("review_backend", "codex")
    model = report.get("review_model")
    backend_label = f"`{backend}`" + (f" (`{model}`)" if model else "")
    if report.get("fallback_used"):
        reason = report.get("fallback_reason") or "codex_unavailable"
        backend_label += f" — fallback ({reason})"

    lines = [
        f"# Consolidated Codex Room Review — `{report['revision_sha'][:12]}`",
        "",
        f"- **Gate Status**: `{report['review_gate_status']}`",
        f"- **Revision SHA**: `{report['revision_sha']}`",
        f"- **Review Backend**: {backend_label}",
        f"- **Passes Completed**: `{report['passes_completed']}` (independent contexts: `{report['independent_contexts']}`)",
        f"- **Blocking Open Findings**: `{report['blocking_open_count']}`",
        f"- **Advisory Findings**: `{report['advisory_count']}`",
        "",
    ]
    if report["errors"]:
        lines.append("## Errors / Blockers")
        for err in report["errors"]:
            lines.append(f"- {err}")
        lines.append("")

    if report["consolidated_findings"]:
        lines.extend(
            [
                "## Consolidated Findings",
                "",
                "| ID | Severity | Blocking | Location | Detected By | Title | Status |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for f in report["consolidated_findings"]:
            loc = f"`{f['file']}:{f['line_start']}-{f['line_end']}`"
            det = ", ".join(f["detected_by"])
            lines.append(
                f"| {f['id']} | `{f['severity']}` | {f['blocking']} | {loc} | {det} | {f['title']} | `{f['status']}` |"
            )
        lines.append("")

    if report["test_quality_issues"]:
        lines.append("## Test Quality Issues")
        for item in report["test_quality_issues"]:
            lines.append(f"- {item}")
        lines.append("")

    if report["unverified_areas"]:
        lines.append("## Unverified Areas")
        for item in report["unverified_areas"]:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _run_subprocess_with_watchdog(
    cmd: list[str],
    *,
    pass_name: str,
    timeout_sec: float,
    idle_timeout_sec: float,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, list[str], list[str], str | None]:
    """Run a review subprocess with activity-based idle timeout and hard ceiling."""
    try:
        proc, job_handle = spawn_managed_process(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
    except RuntimeError as exc:
        return 1, [], [f"{exc}\n"], str(exc)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    activity_lock = threading.Lock()
    started_at = time.monotonic()
    last_activity = started_at

    def _touch_activity() -> None:
        nonlocal last_activity
        with activity_lock:
            last_activity = time.monotonic()

    def _write_stdin() -> None:
        if proc.stdin is None or stdin_text is None:
            return
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _pump_stream(stream: Any, bucket: list[str]) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                bucket.append(line)
                _touch_activity()
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    writer_thread = threading.Thread(target=_write_stdin, daemon=True) if stdin_text is not None else None
    stdout_thread = threading.Thread(target=_pump_stream, args=(proc.stdout, stdout_lines), daemon=True)
    stderr_thread = threading.Thread(target=_pump_stream, args=(proc.stderr, stderr_lines), daemon=True)
    if writer_thread is not None:
        writer_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    termination_reason: str | None = None
    try:
        while proc.poll() is None or stdout_thread.is_alive() or stderr_thread.is_alive():
            now = time.monotonic()
            with activity_lock:
                idle_elapsed = now - last_activity
            total_elapsed = now - started_at

            if idle_timeout_sec > 0 and idle_elapsed >= idle_timeout_sec:
                termination_reason = (
                    f"[codex_room_review] pass={pass_name} terminated: "
                    f"idle_timeout={idle_timeout_sec:.1f}s (total={total_elapsed:.1f}s)\n"
                )
                terminate_process_tree(proc, job_handle=job_handle)
                job_handle = None
                break
            if timeout_sec > 0 and total_elapsed >= timeout_sec:
                termination_reason = (
                    f"[codex_room_review] pass={pass_name} terminated: "
                    f"total_timeout={timeout_sec:.1f}s (idle={idle_elapsed:.1f}s)\n"
                )
                terminate_process_tree(proc, job_handle=job_handle)
                job_handle = None
                break
            time.sleep(0.05)

        if writer_thread is not None:
            writer_thread.join(timeout=2.0)
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
    finally:
        close_job_handle(job_handle)

    if termination_reason:
        stderr_lines.append(termination_reason)

    return proc.returncode if proc.returncode is not None else 1, stdout_lines, stderr_lines, termination_reason


def _run_codex_pass(
    codex_prefix: list[str],
    role: str,
    runtime_dir: Path,
    repo_root: Path,
    schema_path: Path,
    prompt_text: str,
    out_dir: Path,
    pass_name: str,
    model: str | None,
    timeout_sec: float,
    idle_timeout_sec: float = 180.0,
) -> dict[str, Any] | None:
    pass_json = out_dir / f"{pass_name}.json"
    raw_jsonl = out_dir / f"{pass_name}.raw.jsonl"
    stderr_log = out_dir / f"{pass_name}.stderr.log"

    cmd = [
        *codex_prefix,
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(repo_root),
        "--output-schema",
        str(schema_path),
        "-o",
        str(pass_json),
        "--json",
    ]
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")

    env = dict(os.environ)
    env["CODEX_HOME"] = str(runtime_dir)

    rc, stdout_lines, stderr_lines, termination_reason = _run_subprocess_with_watchdog(
        cmd,
        pass_name=pass_name,
        timeout_sec=timeout_sec,
        idle_timeout_sec=idle_timeout_sec,
        stdin_text=prompt_text,
        env=env,
    )

    raw_jsonl.write_text("".join(stdout_lines), encoding="utf-8")
    stderr_log.write_text("".join(stderr_lines), encoding="utf-8")

    if termination_reason is not None or rc != 0 or not pass_json.is_file():
        return None
    try:
        return json.loads(pass_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _build_agy_cli_prompt(prompt_text: str, prompt_file: Path) -> str:
    """Build a strictly read-only CLI prompt for `agy -p`, avoiding Windows argv limits."""
    readonly_prefix = (
        "STRICT READ-ONLY INDEPENDENT REVIEW PASS (HEADLESS MODE). "
        "Do NOT create, edit, delete, or modify any files, do NOT spawn subagents, "
        "and NEVER invoke any shell, terminal, or command execution tool (headless mode auto-denies all shell commands). "
        "Use ONLY built-in read-only file/search tools (read_file, grep/search, find/list); "
        "all commit SHA, diff stat, and changed-file metadata are already provided in the prompt. "
        "Return ONLY valid JSON matching the provided schema.\n\n"
    )
    if len(prompt_text) <= AGY_INLINE_PROMPT_CHAR_LIMIT:
        return readonly_prefix + prompt_text
    return (
        readonly_prefix
        + f"Read and execute the complete review instructions from `{prompt_file}` using ONLY read-only file/search tools "
        + "(never call shell/command tools), then return ONLY the final JSON object matching the provided schema."
    )


def _run_agy_pass(
    agy_prefix: list[str],
    role: str,
    repo_root: Path,
    schema_path: Path,
    prompt_text: str,
    prompt_file: Path,
    out_dir: Path,
    pass_name: str,
    model: str,
    timeout_sec: float,
    idle_timeout_sec: float = 180.0,
) -> dict[str, Any] | None:
    """Run one isolated read-only review pass via Antigravity CLI (`agy`)."""
    pass_json = out_dir / f"{pass_name}.json"
    raw_jsonl = out_dir / f"{pass_name}.raw.jsonl"
    stderr_log = out_dir / f"{pass_name}.stderr.log"

    cli_prompt = _build_agy_cli_prompt(prompt_text, prompt_file)
    cmd = [
        *agy_prefix,
        "--sandbox",
        "--mode",
        "plan",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--json-schema",
        str(schema_path),
        "-p",
        cli_prompt,
    ]

    rc, stdout_lines, stderr_lines, termination_reason = _run_subprocess_with_watchdog(
        cmd,
        pass_name=f"{pass_name}:agy",
        timeout_sec=timeout_sec,
        idle_timeout_sec=idle_timeout_sec,
        stdin_text=None,
        cwd=repo_root,
    )

    raw_jsonl.write_text("".join(stdout_lines), encoding="utf-8")
    stderr_log.write_text("".join(stderr_lines), encoding="utf-8")

    if termination_reason is not None or rc != 0:
        return None
    parsed = _extract_agy_structured_output(stdout_lines)
    if not isinstance(parsed, dict):
        return None
    pass_json.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    return parsed


def _execute_two_passes(
    runner: Any,
    pass1_kwargs: dict[str, Any],
    pass2_kwargs: dict[str, Any],
    *,
    sequential: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if sequential:
        return runner(**pass1_kwargs), runner(**pass2_kwargs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(runner, **pass1_kwargs)
        fut2 = pool.submit(runner, **pass2_kwargs)
        return fut1.result(), fut2.result()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 2-pass Codex Room independent code review.")
    parser.add_argument("--commit", default="HEAD", help="Immutable commit SHA or ref to review.")
    parser.add_argument("--base", default=None, help="Optional base commit SHA for diff context.")
    parser.add_argument("--task-id", default="adhoc", help="Task identifier for run folder naming.")
    parser.add_argument("--attempt-id", default=None, help="Optional unique attempt ID for write-once run dir.")
    parser.add_argument("--task-packet", type=Path, default=None, help="Path to Task Packet markdown.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".codex-runtime")
    parser.add_argument("--model", type=str, default=None, help="Optional explicit model override for active backend.")
    parser.add_argument(
        "--backend",
        choices=("auto", "codex", "agy"),
        default="auto",
        help=(
            "Review backend: 'auto' (default — prioritize Codex CLI, fallback to agy Opus 5.5 on "
            "quota exhaustion or unavailability), 'codex' (Codex CLI only), or 'agy' (Antigravity CLI directly)."
        ),
    )
    parser.add_argument(
        "--fallback-model",
        type=str,
        default=DEFAULT_AGY_FALLBACK_MODEL,
        help=f"Antigravity CLI model used for fallback or --backend agy. Default: {DEFAULT_AGY_FALLBACK_MODEL}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1200.0,
        help="Hard wall-clock safety ceiling per pass in seconds (0 disables). Default: 1200.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=180.0,
        help="Inactivity watchdog timeout per pass in seconds; resets on stdout/stderr activity (0 disables). Default: 180.",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run pass1 and pass2 sequentially instead of in parallel.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--consolidate-only",
        nargs=2,
        metavar=("PASS1_JSON", "PASS2_JSON"),
        type=Path,
        default=None,
    )
    parser.add_argument("--fail-on-blocking", action="store_true")
    return parser


def _merge_prior_pass_findings(
    fallback_payload: dict[str, Any] | None,
    prior_payload: dict[str, Any] | None,
    expected_sha: str,
) -> dict[str, Any] | None:
    """Preserve findings/issues from a completed Codex pass when falling back to agy."""
    if not isinstance(prior_payload, dict):
        return fallback_payload
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if validate_instance_against_schema(prior_payload, schema):
        return prior_payload
    if str(prior_payload.get("revision_sha", "")).strip() != expected_sha:
        return prior_payload
    if prior_payload.get("verdict") == "BLOCKED":
        return prior_payload

    prior_findings = list(prior_payload.get("findings", []))
    prior_has_blocking = any(
        str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
        for f in prior_findings
        if isinstance(f, dict)
    )
    if prior_payload.get("verdict") == "CHANGES_REQUESTED" and not prior_has_blocking:
        return prior_payload
    if not isinstance(fallback_payload, dict):
        return prior_payload

    merged = dict(fallback_payload)
    fb_findings = list(fallback_payload.get("findings", []))
    merged["findings"] = [*prior_findings, *fb_findings]
    merged["test_quality_issues"] = _dedup_ordered(
        [*prior_payload.get("test_quality_issues", []), *fallback_payload.get("test_quality_issues", [])]
    )
    merged["unverified_areas"] = _dedup_ordered(
        [*prior_payload.get("unverified_areas", []), *fallback_payload.get("unverified_areas", [])]
    )
    has_blocking = any(
        str(f.get("severity", "medium")).lower() in BLOCKING_SEVERITIES
        for f in merged["findings"]
    )
    if merged.get("verdict") == "APPROVE" and has_blocking:
        merged["verdict"] = "CHANGES_REQUESTED"
    return merged


def _write_once_outputs(out_dir: Path, report: dict[str, Any]) -> bool:
    """Write consolidated-review.json and .md exclusively, refusing overwrite."""
    json_path = out_dir / "consolidated-review.json"
    md_path = out_dir / "consolidated-review.md"
    try:
        with json_path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(report, indent=2) + "\n")
    except FileExistsError:
        print(
            f"[codex_room_review] Write-once refusal: {json_path} already exists.",
            file=sys.stderr,
        )
        return False
    with md_path.open("x", encoding="utf-8") as fh:
        fh.write(render_consolidated_markdown(report))
    return True


def _call_verify_role(role: str, runtime_root: Path, repo_root: Path) -> dict[str, Any]:
    sig = inspect.signature(verify_role)
    has_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "repo_root"
        for p in sig.parameters.values()
    )
    if has_kw:
        return verify_role(role, runtime_root, repo_root=repo_root)
    return verify_role(role, runtime_root)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    _, excluded_prefixes, policy_err = load_model_policy(repo_root, require_exists=False)
    if policy_err:
        print(f"[codex_room_review] BLOCKED: {policy_err}", file=sys.stderr)
        return 2
    if args.model and is_excluded_model(args.model, excluded_prefixes):
        print(
            f"[codex_room_review] BLOCKED: --model {args.model!r} "
            "is excluded by model-policy.json.",
            file=sys.stderr,
        )
        return 2
    if args.fallback_model and is_excluded_model(args.fallback_model, excluded_prefixes):
        print(
            f"[codex_room_review] BLOCKED: --fallback-model {args.fallback_model!r} "
            "is excluded by model-policy.json.",
            file=sys.stderr,
        )
        return 2

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = validate_strict_json_schema(schema)
    if schema_errors:
        print(f"[codex_room_review] Invalid review-output.schema.json: {schema_errors}", file=sys.stderr)
        return 2

    try:
        info = resolve_commit_info(repo_root, args.commit, args.base)
    except (ValueError, TimeoutError) as exc:
        print(f"[codex_room_review] {exc}", file=sys.stderr)
        return 2
    git_top = Path(info["git_top"])
    if git_top != repo_root:
        _, git_prefixes, git_policy_err = load_model_policy(git_top, require_exists=False)
        if git_policy_err:
            print(f"[codex_room_review] BLOCKED: {git_policy_err}", file=sys.stderr)
            return 2
        if args.model and is_excluded_model(args.model, git_prefixes):
            print(
                f"[codex_room_review] BLOCKED: --model {args.model!r} is excluded.",
                file=sys.stderr,
            )
            return 2
        if args.fallback_model and is_excluded_model(args.fallback_model, git_prefixes):
            print(
                f"[codex_room_review] BLOCKED: --fallback-model {args.fallback_model!r} "
                "is excluded.",
                file=sys.stderr,
            )
            return 2
    candidate_sha = info["candidate_sha"]
    if args.out_dir is not None:
        out_dir = args.out_dir.resolve()
    else:
        attempt_id = args.attempt_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
        out_dir = (
            git_top / "harness" / "runs" / args.task_id / "attempts" / f"{attempt_id}-{candidate_sha[:12]}"
        ).resolve()

    if any(
        (out_dir / marker).exists()
        for marker in (".review-attempt.lock", "consolidated-review.json", "pass1.prompt.md", "pass2.prompt.md", "frozen")
    ):
        print(
            f"[codex_room_review] Write-once refusal: {out_dir} already contains review artifacts or attempt lock.",
            file=sys.stderr,
        )
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".review-attempt.lock"
    try:
        with lock_path.open("x", encoding="utf-8") as lock_fh:
            lock_fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "candidate_sha": candidate_sha,
                        "task_id": args.task_id,
                        "dry_run": bool(args.dry_run),
                        "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
    except FileExistsError:
        print(
            f"[codex_room_review] Write-once refusal: concurrent or duplicate attempt lock exists at {lock_path}.",
            file=sys.stderr,
        )
        return 2

    if args.consolidate_only:
        p1_path, p2_path = args.consolidate_only
        p1_data = json.loads(p1_path.read_text(encoding="utf-8")) if p1_path.is_file() else None
        p2_data = json.loads(p2_path.read_text(encoding="utf-8")) if p2_path.is_file() else None
        report = consolidate_reviews(p1_data, p2_data, expected_sha=candidate_sha)
        tmp_dir = tempfile.mkdtemp(prefix="crr-consolidate-")
        try:
            frozen_meta = materialize_frozen_tree(
                git_top,
                candidate_sha,
                Path(tmp_dir) / "topstep-chart-app",
                base_sha=info["base_sha"],
            )
            report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
        except (ValueError, RuntimeError, TimeoutError) as exc:
            report["errors"].append(f"Unable to compute frozen_tree_sha256: {exc}")
            report["review_gate_status"] = "BLOCKED"
        finally:
            cleanup_readonly_tree(Path(tmp_dir))
        if not _write_once_outputs(out_dir, report):
            return 2
        print(f"[codex_room_review] status={report['review_gate_status']} out={out_dir}")
        if report["review_gate_status"] == "BLOCKED":
            return 2
        if args.fail_on_blocking and report["blocking_open_count"] > 0:
            return 1
        return 0

    frozen_dir = out_dir / "frozen"
    frozen_src_dir = frozen_dir / "topstep-chart-app"
    try:
        frozen_meta = materialize_frozen_tree(git_top, candidate_sha, frozen_src_dir, base_sha=info["base_sha"])
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"[codex_room_review] {exc}", file=sys.stderr)
        return 2
    info["frozen_src_dir"] = frozen_meta["dest_dir"]
    info["patch_path"] = frozen_meta["patch_path"]
    patch_file_path = Path(frozen_meta["patch_path"]) if frozen_meta["patch_path"] else None
    patch_sha_before = (
        hashlib.sha256(patch_file_path.read_bytes()).hexdigest()
        if patch_file_path and patch_file_path.is_file()
        else None
    )

    def _patch_modified() -> bool:
        if patch_file_path is None:
            return False
        if not patch_file_path.is_file():
            return True
        return hashlib.sha256(patch_file_path.read_bytes()).hexdigest() != patch_sha_before

    task_text = (
        args.task_packet.read_text(encoding="utf-8")
        if args.task_packet and args.task_packet.is_file()
        else "No Task Packet file supplied; review candidate commit against AGENTS.md, implementation_plan.md, and docs/WORKSPACE_PROTOCOL.md."
    )
    prompt1 = render_prompt(PROMPTS_DIR / "pass1-correctness.md", info, task_text)
    prompt2 = render_prompt(PROMPTS_DIR / "pass2-security.md", info, task_text)
    prompt1_path = out_dir / "pass1.prompt.md"
    prompt2_path = out_dir / "pass2.prompt.md"
    prompt1_path.write_text(prompt1, encoding="utf-8")
    prompt2_path.write_text(prompt2, encoding="utf-8")

    frozen_prompts_dir = frozen_dir / "prompts"
    frozen_prompts_dir.mkdir(parents=True, exist_ok=True)
    frozen_prompt1_path = frozen_prompts_dir / "pass1.prompt.md"
    frozen_prompt2_path = frozen_prompts_dir / "pass2.prompt.md"
    frozen_prompt1_path.write_text(prompt1, encoding="utf-8")
    frozen_prompt2_path.write_text(prompt2, encoding="utf-8")
    os.chmod(frozen_prompt1_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(frozen_prompt2_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    if args.dry_run:
        try:
            resolve_agy_command()
            agy_available = True
        except FileNotFoundError:
            agy_available = False
        meta = {
            "dry_run": True,
            "candidate_sha": candidate_sha,
            "base_sha": info["base_sha"],
            "frozen_src_dir": str(frozen_src_dir),
            "patch_path": frozen_meta["patch_path"],
            "frozen_tree_sha256": frozen_meta["frozen_tree_sha256"],
            "worktree_dirty": info["worktree_dirty"],
            "schema_valid": True,
            "backend": args.backend,
            "fallback_model": args.fallback_model,
            "agy_available": agy_available,
            "out_dir": str(out_dir),
            "runtime_pass1": str(args.runtime_root / "topstep-review-pass1"),
            "runtime_pass2": str(args.runtime_root / "topstep-review-pass2"),
        }
        print(json.dumps(meta, indent=2))
        return 0

    p1_data: dict[str, Any] | None = None
    p2_data: dict[str, Any] | None = None
    active_backend = "codex"
    active_model: str | None = args.model
    fallback_used = False
    fallback_reason: str | None = None
    readonly_violation: str | None = None

    def _run_agy_review_pair(agy_model: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        agy_prefix = resolve_agy_command()
        try:
            before_status = _git_output(git_top, ["status", "--porcelain"]).stdout
        except TimeoutError as exc:
            return None, None, f"Read-only check failed before agy pass: {exc}"
        p1_kw = dict(
            agy_prefix=agy_prefix,
            role="pass1",
            repo_root=frozen_dir,
            schema_path=SCHEMA_PATH,
            prompt_text=prompt1,
            prompt_file=frozen_prompt1_path,
            out_dir=out_dir,
            pass_name="pass1",
            model=agy_model,
            timeout_sec=args.timeout,
            idle_timeout_sec=args.idle_timeout,
        )
        p2_kw = dict(
            agy_prefix=agy_prefix,
            role="pass2",
            repo_root=frozen_dir,
            schema_path=SCHEMA_PATH,
            prompt_text=prompt2,
            prompt_file=frozen_prompt2_path,
            out_dir=out_dir,
            pass_name="pass2",
            model=agy_model,
            timeout_sec=args.timeout,
            idle_timeout_sec=args.idle_timeout,
        )
        res1, res2 = _execute_two_passes(_run_agy_pass, p1_kw, p2_kw, sequential=args.sequential)
        try:
            after_status = _git_output(git_top, ["status", "--porcelain"]).stdout
        except TimeoutError as exc:
            return res1, res2, f"Read-only check failed after agy pass: {exc}"
        frozen_hash_after, _ = compute_tree_sha256(frozen_src_dir)
        if after_status != before_status:
            violation: str | None = "Read-only violation: git worktree changed during agy review pass."
        elif frozen_hash_after != frozen_meta["frozen_tree_sha256"]:
            violation = "Read-only violation: frozen-src snapshot modified during agy review pass."
        elif _patch_modified():
            violation = "Read-only violation: candidate.patch modified during agy review pass."
        else:
            violation = None
        return res1, res2, violation

    if args.backend == "agy":
        active_backend = "agy"
        active_model = args.model or args.fallback_model
        try:
            p1_data, p2_data, readonly_violation = _run_agy_review_pair(active_model)
        except FileNotFoundError as exc:
            print(f"[codex_room_review] {exc}", file=sys.stderr)
            return 2
    else:
        if args.model is not None and args.model != EXPECTED_REVIEW_MODEL:
            model_err = f"Primary codex reviewer model is locked to {EXPECTED_REVIEW_MODEL!r}, got {args.model!r}"
            print(f"[codex_room_review] {model_err}", file=sys.stderr)
            report = consolidate_reviews(None, None, expected_sha=candidate_sha)
            report["errors"] = [model_err]
            report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
            report["candidate_patch_path"] = frozen_meta["patch_path"]
            report["worktree_dirty_at_review"] = info["worktree_dirty"]
            report["review_backend"] = "codex"
            report["review_model"] = args.model
            report["fallback_used"] = False
            report["fallback_reason"] = None
            _write_once_outputs(out_dir, report)
            return 2

        active_model = EXPECTED_REVIEW_MODEL
        v1 = _call_verify_role("pass1", args.runtime_root, git_top)
        v2 = _call_verify_role("pass2", args.runtime_root, git_top)
        if not (v1["ok"] and v2["ok"]):
            preflight_err = (
                "Codex Room runtime failed safety/model preflight: "
                f"pass1={v1.get('errors', [])}; pass2={v2.get('errors', [])}"
            )
            print(f"[codex_room_review] {preflight_err}", file=sys.stderr)
            report = consolidate_reviews(None, None, expected_sha=candidate_sha)
            report["errors"] = [preflight_err]
            report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
            report["candidate_patch_path"] = frozen_meta["patch_path"]
            report["worktree_dirty_at_review"] = info["worktree_dirty"]
            report["review_backend"] = "codex"
            report["review_model"] = active_model
            report["fallback_used"] = False
            report["fallback_reason"] = None
            _write_once_outputs(out_dir, report)
            return 2

        codex_prefix: list[str] | None = None
        codex_precheck_reason: str | None = None
        try:
            codex_prefix = resolve_codex_command()
        except FileNotFoundError:
            codex_precheck_reason = "codex_cli_not_found"

        if codex_precheck_reason is not None:
            if args.backend == "codex":
                print("[codex_room_review] Codex CLI (`codex`) not found on PATH.", file=sys.stderr)
                return 2
            fallback_reason = codex_precheck_reason
        else:
            assert codex_prefix is not None
            pass1_kwargs = dict(
                codex_prefix=codex_prefix,
                role="pass1",
                runtime_dir=args.runtime_root / "topstep-review-pass1",
                repo_root=frozen_dir,
                schema_path=SCHEMA_PATH,
                prompt_text=prompt1,
                out_dir=out_dir,
                pass_name="pass1",
                model=active_model,
                timeout_sec=args.timeout,
                idle_timeout_sec=args.idle_timeout,
            )
            pass2_kwargs = dict(
                codex_prefix=codex_prefix,
                role="pass2",
                runtime_dir=args.runtime_root / "topstep-review-pass2",
                repo_root=frozen_dir,
                schema_path=SCHEMA_PATH,
                prompt_text=prompt2,
                out_dir=out_dir,
                pass_name="pass2",
                model=active_model,
                timeout_sec=args.timeout,
                idle_timeout_sec=args.idle_timeout,
            )
            p1_data, p2_data = _execute_two_passes(
                _run_codex_pass, pass1_kwargs, pass2_kwargs, sequential=args.sequential
            )

            if args.backend == "auto" and (p1_data is None or p2_data is None):
                quota_reason = _detect_codex_quota_error(out_dir)
                if quota_reason:
                    fallback_reason = quota_reason
                    _archive_failed_codex_logs(out_dir)

        if fallback_reason is not None and args.backend == "auto":
            print(
                f"[codex_room_review] Codex CLI unavailable ({fallback_reason}); "
                f"falling back to agy ({args.fallback_model}).",
                file=sys.stderr,
            )
            prior_codex_p1 = p1_data
            prior_codex_p2 = p2_data
            try:
                active_backend = "agy"
                active_model = args.fallback_model
                fallback_used = True
                fb_p1, fb_p2, readonly_violation = _run_agy_review_pair(active_model)
                p1_data = _merge_prior_pass_findings(fb_p1, prior_codex_p1, candidate_sha)
                p2_data = _merge_prior_pass_findings(fb_p2, prior_codex_p2, candidate_sha)
            except FileNotFoundError as exc:
                print(f"[codex_room_review] Fallback failed: {exc}", file=sys.stderr)
                return 2

    report = consolidate_reviews(p1_data, p2_data, expected_sha=candidate_sha)
    if readonly_violation:
        report["errors"].append(readonly_violation)
        report["review_gate_status"] = "BLOCKED"
    frozen_hash_final, _ = compute_tree_sha256(frozen_src_dir)
    if frozen_hash_final != frozen_meta["frozen_tree_sha256"]:
        report["errors"].append("Read-only violation: frozen-src snapshot changed during review.")
        report["review_gate_status"] = "BLOCKED"
    if _patch_modified():
        report["errors"].append("Read-only violation: candidate.patch changed during review.")
        report["review_gate_status"] = "BLOCKED"
    report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
    report["candidate_patch_path"] = frozen_meta["patch_path"]
    report["worktree_dirty_at_review"] = info["worktree_dirty"]
    report["review_backend"] = active_backend
    report["review_model"] = active_model
    report["fallback_used"] = fallback_used
    report["fallback_reason"] = fallback_reason
    if not _write_once_outputs(out_dir, report):
        return 2

    print(
        f"[codex_room_review] sha={candidate_sha[:12]} backend={active_backend} "
        f"model={active_model or 'default'} fallback={fallback_used} "
        f"status={report['review_gate_status']} blocking_open={report['blocking_open_count']} out={out_dir}"
    )
    if report["review_gate_status"] == "BLOCKED":
        return 2
    if args.fail_on_blocking and report["blocking_open_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
