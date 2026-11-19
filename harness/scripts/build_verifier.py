"""Executable build-verifier gate (test-required.md §8).

Runs all deterministic checks on the exact repository snapshot, preserves every
exit code and hash, fails visibly when zero tests are collected, and prevents
unverified claims of completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")
PYTEST_ZERO_RE = re.compile(r"(?:collected\s+0\s+items|no\s+tests\s+ran)", re.IGNORECASE)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _resolve_tool(name: str, python_module: str | None = None) -> list[str]:
    for candidate in (f"{name}.cmd", f"{name}.exe", name):
        found = shutil.which(candidate)
        if found:
            if found.lower().endswith(".ps1"):
                return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", found]
            return [found]
    ps1 = shutil.which(f"{name}.ps1")
    if ps1:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    if python_module:
        return [sys.executable, "-m", python_module]
    raise FileNotFoundError(f"Required tool '{name}' not found on PATH.")


def _run_step(name: str, cmd: list[str], cwd: Path, check_pytest: bool = False) -> dict[str, Any]:
    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    exit_code = proc.returncode
    status = "PASSED" if exit_code == 0 else "FAILED"
    note = ""

    if check_pytest:
        if exit_code == 5 or PYTEST_ZERO_RE.search(combined) or not PYTEST_PASSED_RE.search(combined):
            status = "FAILED"
            note = "Zero tests collected/passed (test-required.md §8 forbids empty test gate)."
            if exit_code == 0:
                exit_code = 1

    last_lines = [ln for ln in combined.splitlines() if ln.strip()][-6:]
    return {
        "name": name,
        "command": cmd,
        "cwd": str(cwd),
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_sha256": _sha256_text(proc.stdout or ""),
        "stderr_sha256": _sha256_text(proc.stderr or ""),
        "summary": " | ".join(last_lines) if not note else f"{note} | {' | '.join(last_lines)}",
    }


def _git_snapshot(root: Path) -> dict[str, Any]:
    rev = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    status_proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty_files = [ln for ln in status_proc.stdout.splitlines() if ln.strip()] if status_proc.returncode == 0 else []
    return {
        "initialized": rev.returncode == 0,
        "git_sha": rev.stdout.strip() if rev.returncode == 0 else None,
        "worktree_dirty": bool(dirty_files),
        "dirty_files": dirty_files,
    }


def run_verifier(root: Path, phase: int = 0, strict_clean: bool = False) -> dict[str, Any]:
    """Execute all deterministic verification gates on ``root``."""
    root = root.resolve()
    snapshot = _git_snapshot(root)
    steps: list[dict[str, Any]] = []

    if strict_clean and (not snapshot["initialized"] or snapshot["worktree_dirty"]):
        steps.append(
            {
                "name": "git_strict_clean",
                "command": ["git", "-C", str(root), "status", "--porcelain"],
                "cwd": str(root),
                "status": "FAILED",
                "exit_code": 1,
                "duration_ms": 0,
                "stdout_sha256": _sha256_text(""),
                "stderr_sha256": _sha256_text(""),
                "summary": "Strict clean check failed: repository is uninitialized or has uncommitted changes.",
            }
        )

    steps.append(
        _run_step(
            "arch_check",
            [sys.executable, str(root / "harness" / "scripts" / "arch_check.py"), "--root", str(root), "--json"],
            cwd=root,
        )
    )

    ruff_cmd = _resolve_tool("ruff", python_module="ruff")
    steps.append(_run_step("harness_ruff", [*ruff_cmd, "check", "harness"], cwd=root))

    pytest_cmd = _resolve_tool("pytest", python_module="pytest")
    steps.append(_run_step("harness_pytest", [*pytest_cmd, "harness/tests", "-q"], cwd=root, check_pytest=True))

    backend_dir = root / "backend"
    if (backend_dir / "pyproject.toml").is_file():
        uv_cmd = _resolve_tool("uv")
        steps.append(_run_step("backend_ruff", [*uv_cmd, "run", "--directory", str(backend_dir), "ruff", "check", "."], cwd=root))
        steps.append(
            _run_step(
                "backend_pytest",
                [*uv_cmd, "run", "--directory", str(backend_dir), "pytest", "-q"],
                cwd=root,
                check_pytest=True,
            )
        )
    elif phase >= 1:
        steps.append(
            {
                "name": "backend_suite",
                "command": ["uv", "run", "--directory", str(backend_dir), "pytest", "-q"],
                "cwd": str(root),
                "status": "BLOCKED",
                "exit_code": 2,
                "duration_ms": 0,
                "stdout_sha256": _sha256_text(""),
                "stderr_sha256": _sha256_text(""),
                "summary": f"Required backend/pyproject.toml missing for phase {phase}.",
            }
        )

    frontend_dir = root / "frontend"
    pkg_json = frontend_dir / "package.json"
    if pkg_json.is_file():
        pnpm_cmd = _resolve_tool("pnpm")
        pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
        scripts = pkg_data.get("scripts", {})
        steps.append(_run_step("frontend_typecheck", [*pnpm_cmd, "--dir", str(frontend_dir), "typecheck"], cwd=root))
        steps.append(_run_step("frontend_build", [*pnpm_cmd, "--dir", str(frontend_dir), "build"], cwd=root))
        for optional_script in ("lint", "test"):
            if optional_script in scripts:
                steps.append(
                    _run_step(
                        f"frontend_{optional_script}",
                        [*pnpm_cmd, "--dir", str(frontend_dir), optional_script],
                        cwd=root,
                    )
                )
    elif phase >= 3:
        steps.append(
            {
                "name": "frontend_suite",
                "command": ["pnpm", "--dir", str(frontend_dir), "build"],
                "cwd": str(root),
                "status": "BLOCKED",
                "exit_code": 2,
                "duration_ms": 0,
                "stdout_sha256": _sha256_text(""),
                "stderr_sha256": _sha256_text(""),
                "summary": f"Required frontend/package.json missing for phase {phase}.",
            }
        )

    statuses = {s["status"] for s in steps}
    if "FAILED" in statuses:
        overall = "FAILED"
    elif "BLOCKED" in statuses:
        overall = "BLOCKED"
    else:
        overall = "VERIFIED"

    return {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "phase": phase,
        "git_sha": snapshot["git_sha"],
        "worktree_dirty": snapshot["worktree_dirty"],
        "dirty_files": snapshot["dirty_files"],
        "status": overall,
        "steps": steps,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute build-verifier checks for topstep-chart-app.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--phase", type=int, default=0, choices=range(0, 7))
    parser.add_argument("--strict-clean", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON output file.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_verifier(args.root, phase=args.phase, strict_clean=args.strict_clean)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"[build_verifier] status={report['status']} phase={report['phase']} "
            f"git_sha={report['git_sha']} dirty={report['worktree_dirty']}"
        )
        for step in report["steps"]:
            print(f"  - {step['name']}: {step['status']} (exit={step['exit_code']}, {step['duration_ms']}ms)")

    if report["status"] == "VERIFIED":
        return 0
    if report["status"] == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
