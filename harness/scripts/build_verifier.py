"""Executable build-verifier gate (test-required.md §8).

Runs all deterministic checks on an immutable commit snapshot (or worktree when
``--commit`` is omitted), enforces bounded step/total subprocess timeouts, fails
visibly when zero tests are collected, and prevents unverified completion claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts.schema_utils import cleanup_readonly_tree, materialize_frozen_tree
except ImportError:
    from schema_utils import cleanup_readonly_tree, materialize_frozen_tree

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


def _resolve_backend_venv_python(root: Path) -> Path | None:
    for rel in (Path("backend/.venv/Scripts/python.exe"), Path("backend/.venv/bin/python")):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        pass


def _run_cmd(
    cmd: list[str],
    cwd: Path,
    *,
    timeout_sec: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec if timeout_sec and timeout_sec > 0 else None)
        return (proc.returncode if proc.returncode is not None else 1), stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return 124, stdout or "", stderr or "", True


def _run_step(
    name: str,
    cmd: list[str],
    cwd: Path,
    *,
    check_pytest: bool = False,
    step_timeout_sec: float = 180.0,
    deadline: float | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    effective_timeout: float | None = step_timeout_sec if step_timeout_sec > 0 else None
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "name": name,
                "command": cmd,
                "cwd": str(cwd),
                "status": "FAILED",
                "exit_code": 124,
                "duration_ms": 0,
                "stdout_sha256": _sha256_text(""),
                "stderr_sha256": _sha256_text(""),
                "summary": "TIMED_OUT: total_timeout_sec exhausted before step started.",
            }
        effective_timeout = min(effective_timeout, remaining) if effective_timeout is not None else remaining

    start = time.monotonic()
    exit_code, stdout, stderr, timed_out = _run_cmd(cmd, cwd=cwd, timeout_sec=effective_timeout, env=env)
    duration_ms = int((time.monotonic() - start) * 1000)
    combined = f"{stdout}\n{stderr}".strip()
    status = "PASSED" if exit_code == 0 else "FAILED"
    note = ""

    if timed_out:
        status = "FAILED"
        exit_code = 124
        note = f"TIMED_OUT after {effective_timeout:.2f}s"
    elif check_pytest:
        if exit_code == 5 or PYTEST_ZERO_RE.search(combined) or not PYTEST_PASSED_RE.search(combined):
            status = "FAILED"
            note = "Zero tests collected/passed (test-required.md §8 forbids empty test gate)."
            if exit_code == 0:
                exit_code = 1

    last_lines = [ln for ln in combined.splitlines() if ln.strip()][-6:]
    summary = " | ".join(last_lines)
    if note:
        summary = f"{note} | {summary}" if summary else note

    return {
        "name": name,
        "command": cmd,
        "cwd": str(cwd),
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_sha256": _sha256_text(stdout),
        "stderr_sha256": _sha256_text(stderr),
        "summary": summary,
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


def _isolated_python_env(target_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    prepends = [str(target_root)]
    backend_dir = target_root / "backend"
    if backend_dir.is_dir():
        prepends.insert(0, str(backend_dir))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*prepends, existing]) if existing else os.pathsep.join(prepends)
    return env


def run_verifier(
    root: Path,
    phase: int = 0,
    strict_clean: bool = False,
    *,
    step_timeout_sec: float = 180.0,
    total_timeout_sec: float = 420.0,
    commit_sha: str | None = None,
    frozen_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute all deterministic verification gates on ``root`` or frozen ``commit_sha``."""
    root = root.resolve()
    snapshot = _git_snapshot(root)
    steps: list[dict[str, Any]] = []
    deadline = (time.monotonic() + total_timeout_sec) if total_timeout_sec > 0 else None

    temp_holder: tempfile.TemporaryDirectory[str] | None = None
    target_root = root
    resolved_sha = snapshot["git_sha"]
    source_mode = "worktree"
    frozen_tree_sha256: str | None = None
    isolated_env: dict[str, str] | None = None

    try:
        if commit_sha is not None:
            if frozen_dir is not None:
                dest_dir = frozen_dir.resolve()
            else:
                temp_holder = tempfile.TemporaryDirectory(prefix="bv-frozen-")
                dest_dir = Path(temp_holder.name) / "frozen-src"
            try:
                mat = materialize_frozen_tree(root, commit_sha, dest_dir)
            except (ValueError, RuntimeError) as exc:
                steps.append(
                    {
                        "name": "git_commit_archive",
                        "command": ["git", "-C", str(root), "archive", "--format=tar", commit_sha],
                        "cwd": str(root),
                        "status": "BLOCKED",
                        "exit_code": 2,
                        "duration_ms": 0,
                        "stdout_sha256": _sha256_text(""),
                        "stderr_sha256": _sha256_text(str(exc)),
                        "summary": f"Unable to materialize frozen commit '{commit_sha}': {exc}",
                    }
                )
                return {
                    "verified_at_utc": datetime.now(timezone.utc).isoformat(),
                    "root": str(root),
                    "phase": phase,
                    "git_sha": None,
                    "source_mode": "frozen_commit_archive",
                    "worktree_dirty": snapshot["worktree_dirty"],
                    "dirty_files": snapshot["dirty_files"],
                    "status": "BLOCKED",
                    "steps": steps,
                }
            target_root = Path(mat["dest_dir"])
            resolved_sha = mat["candidate_sha"]
            frozen_tree_sha256 = mat["frozen_tree_sha256"]
            source_mode = "frozen_commit_archive"
            isolated_env = _isolated_python_env(target_root)

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
                [sys.executable, str(target_root / "harness" / "scripts" / "arch_check.py"), "--root", str(target_root), "--json"],
                cwd=target_root,
                step_timeout_sec=step_timeout_sec,
                deadline=deadline,
                env=isolated_env,
            )
        )

        ruff_cmd = _resolve_tool("ruff", python_module="ruff")
        steps.append(
            _run_step(
                "harness_ruff",
                [*ruff_cmd, "check", "harness"],
                cwd=target_root,
                step_timeout_sec=step_timeout_sec,
                deadline=deadline,
                env=isolated_env,
            )
        )

        pytest_cmd = _resolve_tool("pytest", python_module="pytest")
        steps.append(
            _run_step(
                "harness_pytest",
                [*pytest_cmd, "harness/tests", "-q"],
                cwd=target_root,
                check_pytest=True,
                step_timeout_sec=step_timeout_sec,
                deadline=deadline,
                env=isolated_env,
            )
        )

        backend_dir = target_root / "backend"
        if (backend_dir / "pyproject.toml").is_file():
            venv_python = _resolve_backend_venv_python(root)
            if venv_python is not None:
                steps.append(
                    _run_step(
                        "backend_ruff",
                        [str(venv_python), "-m", "ruff", "check", "."],
                        cwd=backend_dir,
                        step_timeout_sec=step_timeout_sec,
                        deadline=deadline,
                        env=isolated_env,
                    )
                )
                steps.append(
                    _run_step(
                        "backend_pytest",
                        [str(venv_python), "-m", "pytest", "-q"],
                        cwd=backend_dir,
                        check_pytest=True,
                        step_timeout_sec=step_timeout_sec,
                        deadline=deadline,
                        env=isolated_env,
                    )
                )
            else:
                uv_cmd = _resolve_tool("uv")
                steps.append(
                    _run_step(
                        "backend_ruff",
                        [*uv_cmd, "run", "--directory", str(backend_dir), "ruff", "check", "."],
                        cwd=target_root,
                        step_timeout_sec=step_timeout_sec,
                        deadline=deadline,
                        env=isolated_env,
                    )
                )
                steps.append(
                    _run_step(
                        "backend_pytest",
                        [*uv_cmd, "run", "--directory", str(backend_dir), "pytest", "-q"],
                        cwd=target_root,
                        check_pytest=True,
                        step_timeout_sec=step_timeout_sec,
                        deadline=deadline,
                        env=isolated_env,
                    )
                )
        elif phase >= 1:
            steps.append(
                {
                    "name": "backend_suite",
                    "command": ["uv", "run", "--directory", str(backend_dir), "pytest", "-q"],
                    "cwd": str(target_root),
                    "status": "BLOCKED",
                    "exit_code": 2,
                    "duration_ms": 0,
                    "stdout_sha256": _sha256_text(""),
                    "stderr_sha256": _sha256_text(""),
                    "summary": f"Required backend/pyproject.toml missing for phase {phase}.",
                }
            )

        frontend_dir = target_root / "frontend"
        pkg_json = frontend_dir / "package.json"
        if pkg_json.is_file():
            pnpm_cmd = _resolve_tool("pnpm")
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg_data.get("scripts", {})
            steps.append(
                _run_step(
                    "frontend_typecheck",
                    [*pnpm_cmd, "--dir", str(frontend_dir), "typecheck"],
                    cwd=target_root,
                    step_timeout_sec=step_timeout_sec,
                    deadline=deadline,
                    env=isolated_env,
                )
            )
            steps.append(
                _run_step(
                    "frontend_build",
                    [*pnpm_cmd, "--dir", str(frontend_dir), "build"],
                    cwd=target_root,
                    step_timeout_sec=step_timeout_sec,
                    deadline=deadline,
                    env=isolated_env,
                )
            )
            for optional_script in ("lint", "test"):
                if optional_script in scripts:
                    steps.append(
                        _run_step(
                            f"frontend_{optional_script}",
                            [*pnpm_cmd, "--dir", str(frontend_dir), optional_script],
                            cwd=target_root,
                            step_timeout_sec=step_timeout_sec,
                            deadline=deadline,
                            env=isolated_env,
                        )
                    )
        elif phase >= 3:
            steps.append(
                {
                    "name": "frontend_suite",
                    "command": ["pnpm", "--dir", str(frontend_dir), "build"],
                    "cwd": str(target_root),
                    "status": "BLOCKED",
                    "exit_code": 2,
                    "duration_ms": 0,
                    "stdout_sha256": _sha256_text(""),
                    "stderr_sha256": _sha256_text(""),
                    "summary": f"Required frontend/package.json missing for phase {phase}.",
                }
            )
    finally:
        if temp_holder is not None:
            cleanup_readonly_tree(Path(temp_holder.name))

    statuses = {s["status"] for s in steps}
    if "FAILED" in statuses:
        overall = "FAILED"
    elif "BLOCKED" in statuses:
        overall = "BLOCKED"
    else:
        overall = "VERIFIED"

    report: dict[str, Any] = {
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "phase": phase,
        "git_sha": resolved_sha,
        "source_mode": source_mode,
        "worktree_dirty": snapshot["worktree_dirty"],
        "dirty_files": snapshot["dirty_files"],
        "status": overall,
        "steps": steps,
    }
    if frozen_tree_sha256 is not None:
        report["frozen_tree_sha256"] = frozen_tree_sha256
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute build-verifier checks for topstep-chart-app.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--phase", type=int, default=0, choices=range(0, 7))
    parser.add_argument("--strict-clean", action="store_true")
    parser.add_argument("--commit", type=str, default=None, help="Verify an immutable commit archive instead of worktree.")
    parser.add_argument("--step-timeout", type=float, default=180.0, help="Per-step timeout in seconds. Default: 180.")
    parser.add_argument("--total-timeout", type=float, default=420.0, help="Total verifier timeout in seconds. Default: 420.")
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON output file.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    frozen_dir = (args.out.resolve().parent / "frozen-src") if (args.commit and args.out) else None
    report = run_verifier(
        args.root,
        phase=args.phase,
        strict_clean=args.strict_clean,
        step_timeout_sec=args.step_timeout,
        total_timeout_sec=args.total_timeout,
        commit_sha=args.commit,
        frozen_dir=frozen_dir,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"[build_verifier] status={report['status']} phase={report['phase']} "
            f"mode={report['source_mode']} git_sha={report['git_sha']} dirty={report['worktree_dirty']}"
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
