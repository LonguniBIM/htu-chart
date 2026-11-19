"""Tests for build_verifier.py ensuring zero-test suites and failures are never masked."""

from __future__ import annotations

import shutil
from pathlib import Path

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
