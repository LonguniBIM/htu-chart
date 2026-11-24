"""Tests for build_verifier.py ensuring zero-test suites and failures are never masked."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness.scripts import build_verifier
from harness.scripts.build_verifier import run_verifier

REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_mini_repo(root: Path, test_code: str = "def test_ok():\n    assert 2 + 2 == 4\n") -> None:
    scripts_dir = root / "harness" / "scripts"
    tests_dir = root / "harness" / "tests"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)
    (root / "harness" / "__init__.py").write_text("", encoding="utf-8")
    (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "harness" / "scripts" / "arch_check.py", scripts_dir / "arch_check.py")
    (tests_dir / "test_sample.py").write_text(test_code, encoding="utf-8")


def test_build_verifier_passes_phase0_on_clean_fixture(tmp_path: Path) -> None:
    _setup_mini_repo(tmp_path)
    report = run_verifier(tmp_path, phase=0, strict_clean=False)
    assert report["status"] == "VERIFIED"
    assert all(s["status"] == "PASSED" for s in report["steps"])


def test_build_verifier_fails_when_zero_tests_collected(tmp_path: Path) -> None:
    _setup_mini_repo(tmp_path, test_code="VALUE = 1\n")
    report = run_verifier(tmp_path, phase=0, strict_clean=False)
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["status"] == "FAILED"


def test_build_verifier_fails_on_arch_violation(tmp_path: Path) -> None:
    _setup_mini_repo(tmp_path)
    bad = tmp_path / "backend" / "app" / "bad.py"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('ENDPOINT = "/api/Order/create"\n', encoding="utf-8")
    report = run_verifier(tmp_path, phase=0, strict_clean=False)
    assert report["status"] == "FAILED"
    arch_step = next(s for s in report["steps"] if s["name"] == "arch_check")
    assert arch_step["status"] == "FAILED"


def test_build_verifier_blocks_phase1_when_backend_missing(tmp_path: Path) -> None:
    _setup_mini_repo(tmp_path)
    report = run_verifier(tmp_path, phase=1, strict_clean=False)
    assert report["status"] == "BLOCKED"
    backend_step = next(s for s in report["steps"] if s["name"] == "backend_suite")
    assert backend_step["status"] == "BLOCKED"


def test_build_verifier_commit_runs_against_frozen_snapshot_not_dirty_worktree(tmp_path: Path) -> None:
    """NC-09: build_verifier --commit <sha> must verify frozen commit archive, ignoring uncommitted edits."""
    _setup_mini_repo(tmp_path, test_code="def test_ok():\n    assert 2 + 2 == 4\n")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "clean base"], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty working tree after commit: break test and add SEC-001 violation
    (tmp_path / "harness" / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 2 + 2 == 5\n", encoding="utf-8"
    )
    bad = tmp_path / "backend" / "app" / "bad.py"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('ENDPOINT = "/api/Order/create"\n', encoding="utf-8")

    out_json = tmp_path / "out" / "verifier.json"
    rc = build_verifier.main(["--root", str(tmp_path), "--phase", "0", "--commit", sha, "--out", str(out_json)])
    assert rc == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["status"] == "VERIFIED"
    assert report["git_sha"] == sha
    assert report["source_mode"] == "frozen_commit_archive"
    assert report["worktree_dirty"] is True
    assert len(report["frozen_tree_sha256"]) == 64
    assert (tmp_path / "out" / "frozen" / "topstep-chart-app").is_dir()
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert Path(pytest_step["cwd"]).name == "topstep-chart-app"


def test_build_verifier_commit_inside_gitignored_runs_dir_succeeds(tmp_path: Path) -> None:
    """Reject-fix 2: build_verifier --commit works when --out is inside a gitignored subdirectory of repo."""
    _setup_mini_repo(tmp_path, test_code="def test_ok():\n    assert 2 + 2 == 4\n")
    (tmp_path / ".gitignore").write_text("harness/runs/\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    nested_out = tmp_path / "harness" / "runs" / "g3-check" / "verifier.json"
    rc = build_verifier.main(["--root", str(tmp_path), "--phase", "0", "--commit", sha, "--out", str(nested_out)])
    assert rc == 0
    report = json.loads(nested_out.read_text(encoding="utf-8"))
    assert report["status"] == "VERIFIED"
    assert (nested_out.parent / "frozen" / "topstep-chart-app").is_dir()


def test_build_verifier_terminates_timed_out_subprocess_with_exit_124(tmp_path: Path, monkeypatch) -> None:
    """Bounded subprocess execution: step exceeding step_timeout_sec must terminate and return exit_code 124."""
    _setup_mini_repo(tmp_path)
    sleeper = tmp_path / "sleep_cmd.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: [sys.executable, str(sleeper)] if name == "pytest" else [sys.executable, "-c", "pass"],
    )
    report = run_verifier(tmp_path, phase=0, strict_clean=False, step_timeout_sec=0.4, total_timeout_sec=5.0)
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["status"] == "FAILED"
    assert pytest_step["exit_code"] == 124
    assert "TIMED_OUT" in pytest_step["summary"]


def test_build_verifier_handles_hung_git_and_kills_child_process_tree(tmp_path: Path, monkeypatch) -> None:
    """P1-05 & S2-004: Hung git command and child-spawning step must terminate cleanly with exit 124."""
    _setup_mini_repo(tmp_path)
    child_pid_file = tmp_path / "child.pid"
    spawner = tmp_path / "spawn_child.py"
    spawner.write_text(
        (
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_pid_file)!r}, 'w', encoding='utf-8').write(str(p.pid))\n"
            "time.sleep(30)\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: [sys.executable, str(spawner)] if name == "pytest" else [sys.executable, "-c", "pass"],
    )
    report = run_verifier(tmp_path, phase=0, strict_clean=False, step_timeout_sec=0.8, total_timeout_sec=5.0)
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["exit_code"] == 124
    assert "TIMED_OUT" in pytest_step["summary"]

    # Hung git subprocess must time out cleanly instead of blocking indefinitely
    def _hung_git(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=0.1)

    monkeypatch.setattr(build_verifier.subprocess, "run", _hung_git)
    git_timeout_report = run_verifier(tmp_path, phase=0, strict_clean=False, total_timeout_sec=0.5)
    assert git_timeout_report["status"] == "FAILED"
    assert any(s["exit_code"] == 124 and "TIMED_OUT" in s["summary"] for s in git_timeout_report["steps"])


def test_build_verifier_backend_isolation_uses_frozen_commit_not_dirty_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    """Frozen backend import isolation: PYTHONPATH prepends frozen backend so dirty worktree app edits are ignored."""
    _setup_mini_repo(tmp_path)
    backend_dir = tmp_path / "backend"
    app_pkg = backend_dir / "app"
    tests_dir = backend_dir / "tests"
    app_pkg.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (backend_dir / "pyproject.toml").write_text('[project]\nname = "mini"\nversion = "0.1.0"\n', encoding="utf-8")
    (app_pkg / "__init__.py").write_text("", encoding="utf-8")
    (app_pkg / "val.py").write_text("VALUE = 'COMMITTED'\n", encoding="utf-8")
    (tests_dir / "test_val.py").write_text(
        "from app.val import VALUE\n\ndef test_committed_value():\n    assert VALUE == 'COMMITTED'\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "backend v1"], cwd=tmp_path, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty worktree edit after commit + dirty PYTHONPATH pointing to mutable worktree
    (app_pkg / "val.py").write_text("VALUE = 'DIRTY'\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(backend_dir))
    monkeypatch.setattr(build_verifier, "_resolve_backend_venv_python", lambda _root: Path(sys.executable))

    out_json = tmp_path / "out_iso" / "verifier.json"
    report = run_verifier(
        tmp_path,
        phase=1,
        strict_clean=False,
        commit_sha=sha,
        frozen_dir=out_json.parent / "frozen" / "topstep-chart-app",
    )
    assert report["status"] == "VERIFIED"
    backend_step = next(s for s in report["steps"] if s["name"] == "backend_pytest")
    assert backend_step["status"] == "PASSED"
