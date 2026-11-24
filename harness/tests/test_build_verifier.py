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
