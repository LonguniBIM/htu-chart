"""Independent 2-pass Codex Room Lead Review and finding consolidator.

Runs two isolated read-only Codex CLI passes on an immutable commit SHA:
  Pass 1: Correctness, spec adherence, concurrency, and test strength.
  Pass 2: Security, Prop Firm Read-Only, F01-F08 & R01-R06 reliability.

Enforces test-required.md §7: no stale-SHA approval, no majority-vote
dismissal (any blocking finding from either pass keeps status OPEN).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts.codex_room_sync import verify_role
except ImportError:
    from codex_room_sync import verify_role

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_ROOM_DIR = REPO_ROOT / "harness" / "codex-room"
SCHEMA_PATH = CODEX_ROOM_DIR / "schemas" / "review-output.schema.json"
PROMPTS_DIR = CODEX_ROOM_DIR / "prompts"

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


def _git_output(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_commit_info(repo_root: Path, commit_ref: str, base_ref: str | None) -> dict[str, Any]:
    """Resolve immutable candidate SHA, base SHA, diff stat, and dirty status."""
    rev = _git_output(repo_root, ["rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
    if rev.returncode != 0:
        raise ValueError(f"Cannot resolve commit '{commit_ref}': {rev.stderr.strip()}")
    candidate_sha = rev.stdout.strip()

    if base_ref:
        base_rev = _git_output(repo_root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        if base_rev.returncode != 0:
            raise ValueError(f"Cannot resolve base commit '{base_ref}': {base_rev.stderr.strip()}")
        base_sha = base_rev.stdout.strip()
        diff_range = f"{base_sha}..{candidate_sha}"
    else:
        parent_rev = _git_output(repo_root, ["rev-parse", "--verify", f"{candidate_sha}^"])
        if parent_rev.returncode == 0:
            base_sha = parent_rev.stdout.strip()
            diff_range = f"{base_sha}..{candidate_sha}"
        else:
            base_sha = "ROOT_COMMIT"
            diff_range = candidate_sha

    stat_proc = _git_output(repo_root, ["show", "--stat", "--format=", candidate_sha])
    if base_sha != "ROOT_COMMIT":
        stat_proc = _git_output(repo_root, ["diff", "--stat", diff_range])
        files_proc = _git_output(repo_root, ["diff", "--name-status", diff_range])
    else:
        files_proc = _git_output(repo_root, ["show", "--name-status", "--format=", candidate_sha])

    status_proc = _git_output(repo_root, ["status", "--porcelain"])
    dirty_lines = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]

    return {
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
        "{{TASK_PACKET}}": task_packet_text,
    }
    rendered = template
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _finding_dedup_key(f: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(f.get("file", "")).replace("\\", "/").strip(),
        int(f.get("line_start", 0)),
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
    if not isinstance(pass1_data, dict):
        errors.append("Pass 1 (correctness_and_tests) output is missing or invalid JSON.")
    if not isinstance(pass2_data, dict):
        errors.append("Pass 2 (security_and_invariants) output is missing or invalid JSON.")

    if errors:
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

    assert pass1_data is not None and pass2_data is not None
    p1_sha = str(pass1_data.get("revision_sha", "")).strip()
    p2_sha = str(pass2_data.get("revision_sha", "")).strip()
    if p1_sha != expected_sha:
        errors.append(f"Pass 1 revision_sha mismatch: expected {expected_sha}, got {p1_sha!r}")
    if p2_sha != expected_sha:
        errors.append(f"Pass 2 revision_sha mismatch: expected {expected_sha}, got {p2_sha!r}")

    if pass1_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 1 reported BLOCKED: {pass1_data.get('summary', '')}")
    if pass2_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 2 reported BLOCKED: {pass2_data.get('summary', '')}")

    lens1 = str(pass1_data.get("lens", ""))
    lens2 = str(pass2_data.get("lens", ""))
    independent_contexts = bool(lens1 and lens2 and lens1 != lens2)

    merged_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for pass_label, payload in (("pass1", pass1_data), ("pass2", pass2_data)):
        for raw_finding in payload.get("findings", []):
            key = _finding_dedup_key(raw_finding)
            sev = str(raw_finding.get("severity", "medium")).lower()
            is_blocking = sev in BLOCKING_SEVERITIES
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
                    "line_start": int(raw_finding.get("line_start", 0)),
                    "line_end": int(raw_finding.get("line_end", 0)),
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
                existing["line_end"] = max(existing["line_end"], int(raw_finding.get("line_end", 0)))
                ev = str(raw_finding.get("evidence", "")).strip()
                if ev and ev not in existing["evidence"]:
                    existing["evidence"] = f"{existing['evidence']}\n[{pass_label}] {ev}".strip()

    findings_list = sorted(
        merged_by_key.values(),
        key=lambda f: (-SEVERITY_RANK.get(f["severity"], 0), f["file"], f["line_start"]),
    )

    blocking_open_count = sum(1 for f in findings_list if f["blocking"] and f["status"] == "OPEN")
    advisory_count = sum(1 for f in findings_list if not f["blocking"])

    test_issues = _dedup_ordered(
        [*pass1_data.get("test_quality_issues", []), *pass2_data.get("test_quality_issues", [])]
    )
    unverified = _dedup_ordered(
        [*pass1_data.get("unverified_areas", []), *pass2_data.get("unverified_areas", [])]
    )

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
    lines = [
        f"# Consolidated Codex Room Review — `{report['revision_sha'][:12]}`",
        "",
        f"- **Gate Status**: `{report['review_gate_status']}`",
        f"- **Revision SHA**: `{report['revision_sha']}`",
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
    timeout_sec: int,
) -> dict[str, Any] | None:
    pass_json = out_dir / f"{pass_name}.json"
    raw_jsonl = out_dir / f"{pass_name}.raw.jsonl"
    stderr_log = out_dir / f"{pass_name}.stderr.log"

    cmd = [
        *codex_prefix,
        "exec",
        "-s",
        "read-only",
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

    proc = subprocess.run(
        cmd,
        input=prompt_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_sec,
        check=False,
    )
    raw_jsonl.write_text(proc.stdout or "", encoding="utf-8")
    stderr_log.write_text(proc.stderr or "", encoding="utf-8")

    if proc.returncode != 0 or not pass_json.is_file():
        return None
    try:
        return json.loads(pass_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 2-pass Codex Room independent code review.")
    parser.add_argument("--commit", default="HEAD", help="Immutable commit SHA or ref to review.")
    parser.add_argument("--base", default=None, help="Optional base commit SHA for diff context.")
    parser.add_argument("--task-id", default="adhoc", help="Task identifier for run folder naming.")
    parser.add_argument("--task-packet", type=Path, default=None, help="Path to Task Packet markdown.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".codex-runtime")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--timeout", type=int, default=300)
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_errors = validate_strict_json_schema(schema)
    if schema_errors:
        print(f"[codex_room_review] Invalid review-output.schema.json: {schema_errors}", file=sys.stderr)
        return 2

    info = resolve_commit_info(repo_root, args.commit, args.base)
    candidate_sha = info["candidate_sha"]
    out_dir = args.out_dir or (repo_root / "harness" / "runs" / f"{args.task_id}-{candidate_sha[:8]}")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.consolidate_only:
        p1_path, p2_path = args.consolidate_only
        p1_data = json.loads(p1_path.read_text(encoding="utf-8")) if p1_path.is_file() else None
        p2_data = json.loads(p2_path.read_text(encoding="utf-8")) if p2_path.is_file() else None
        report = consolidate_reviews(p1_data, p2_data, expected_sha=candidate_sha)
        (out_dir / "consolidated-review.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (out_dir / "consolidated-review.md").write_text(render_consolidated_markdown(report), encoding="utf-8")
        print(f"[codex_room_review] status={report['review_gate_status']} out={out_dir}")
        if report["review_gate_status"] == "BLOCKED":
            return 2
        if args.fail_on_blocking and report["blocking_open_count"] > 0:
            return 1
        return 0

    task_text = (
        args.task_packet.read_text(encoding="utf-8")
        if args.task_packet and args.task_packet.is_file()
        else "No Task Packet file supplied; review candidate commit against AGENTS.md, implementation_plan.md, and docs/WORKSPACE_PROTOCOL.md."
    )
    prompt1 = render_prompt(PROMPTS_DIR / "pass1-correctness.md", info, task_text)
    prompt2 = render_prompt(PROMPTS_DIR / "pass2-security.md", info, task_text)

    if args.dry_run:
        (out_dir / "pass1.prompt.md").write_text(prompt1, encoding="utf-8")
        (out_dir / "pass2.prompt.md").write_text(prompt2, encoding="utf-8")
        meta = {
            "dry_run": True,
            "candidate_sha": candidate_sha,
            "base_sha": info["base_sha"],
            "worktree_dirty": info["worktree_dirty"],
            "schema_valid": True,
            "out_dir": str(out_dir),
            "runtime_pass1": str(args.runtime_root / "topstep-review-pass1"),
            "runtime_pass2": str(args.runtime_root / "topstep-review-pass2"),
        }
        print(json.dumps(meta, indent=2))
        return 0

    v1 = verify_role("pass1", args.runtime_root)
    v2 = verify_role("pass2", args.runtime_root)
    if not (v1["ok"] and v2["ok"]):
        print(
            "[codex_room_review] Codex Room runtimes not ready. Run codex_room_sync.py --role all first.\n"
            f"  pass1: {v1['errors']}\n  pass2: {v2['errors']}",
            file=sys.stderr,
        )
        return 2

    codex_prefix = resolve_codex_command()
    (out_dir / "pass1.prompt.md").write_text(prompt1, encoding="utf-8")
    (out_dir / "pass2.prompt.md").write_text(prompt2, encoding="utf-8")

    p1_data = _run_codex_pass(
        codex_prefix,
        "pass1",
        args.runtime_root / "topstep-review-pass1",
        repo_root,
        SCHEMA_PATH,
        prompt1,
        out_dir,
        "pass1",
        args.model,
        args.timeout,
    )
    p2_data = _run_codex_pass(
        codex_prefix,
        "pass2",
        args.runtime_root / "topstep-review-pass2",
        repo_root,
        SCHEMA_PATH,
        prompt2,
        out_dir,
        "pass2",
        args.model,
        args.timeout,
    )

    report = consolidate_reviews(p1_data, p2_data, expected_sha=candidate_sha)
    report["worktree_dirty_at_review"] = info["worktree_dirty"]
    (out_dir / "consolidated-review.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "consolidated-review.md").write_text(render_consolidated_markdown(report), encoding="utf-8")

    print(
        f"[codex_room_review] sha={candidate_sha[:12]} status={report['review_gate_status']} "
        f"blocking_open={report['blocking_open_count']} out={out_dir}"
    )
    if report["review_gate_status"] == "BLOCKED":
        return 2
    if args.fail_on_blocking and report["blocking_open_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
