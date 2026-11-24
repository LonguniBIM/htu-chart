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

    # Hung git subprocess must time out cleanly via run_managed_cmd and kill orphan children
    from harness.scripts import schema_utils

    git_child_pid_file = tmp_path / "git_child.pid"
    fake_git_script = tmp_path / "fake_hung_git.py"
    fake_git_script.write_text(
        (
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(git_child_pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
            "sys.exit(0)\n"
        ),
        encoding="utf-8",
    )

    try:
        schema_utils.run_managed_cmd([sys.executable, str(fake_git_script)], timeout_sec=0.8)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("Expected run_managed_cmd to raise TimeoutExpired on hung child holding stdout")
    assert git_child_pid_file.is_file()
    git_child_pid = int(git_child_pid_file.read_text(encoding="utf-8").strip())
    assert not _is_pid_alive(git_child_pid)

    monkeypatch.setattr(
        build_verifier,
        "run_managed_cmd",
        lambda *_a, **_kw: schema_utils.run_managed_cmd([sys.executable, str(fake_git_script)], timeout_sec=0.3),
    )
    git_timeout_report = run_verifier(tmp_path, phase=0, strict_clean=False, total_timeout_sec=2.0)
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
        "from app.val import VALUE\n\n\ndef test_committed_value():\n    assert VALUE == 'COMMITTED'\n",
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


def _is_pid_alive(pid: int) -> bool:
    import ctypes
    import os

    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_build_verifier_commit_fails_when_sibling_lightweight_charts_is_dirty(tmp_path: Path) -> None:
    """S2-001: build_verifier --commit must enforce ARCH-006 against real root.parent / 'lightweight-charts'."""
    app_dir = tmp_path / "topstep-chart-app"
    _setup_mini_repo(app_dir)
    subprocess.run(["git", "init"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "clean app"], cwd=app_dir, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=app_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    sibling = tmp_path / "lightweight-charts"
    sibling.mkdir()
    subprocess.run(["git", "init"], cwd=sibling, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=sibling, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=sibling, check=True, capture_output=True)
    (sibling / "README.md").write_text("upstream\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=sibling, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "upstream"], cwd=sibling, check=True, capture_output=True)
    # Dirty edit in sibling lightweight-charts
    (sibling / "README.md").write_text("MODIFIED UPSTREAM\n", encoding="utf-8")

    report = run_verifier(
        app_dir,
        phase=0,
        strict_clean=False,
        commit_sha=sha,
        frozen_dir=tmp_path / "out" / "frozen" / "topstep-chart-app",
    )
    assert report["status"] == "FAILED"
    assert any("ARCH-006" in s["summary"] or "lightweight-charts" in s["summary"] for s in report["steps"] if s["status"] == "FAILED")


def test_build_verifier_kills_orphan_child_when_parent_exits_early(tmp_path: Path, monkeypatch) -> None:
    """P1-02 & S2-002: Child holding stdout alive after parent exits early must be killed on timeout."""
    _setup_mini_repo(tmp_path)
    child_pid_file = tmp_path / "orphan_child.pid"
    parent_script = tmp_path / "parent_exit_early.py"
    parent_script.write_text(
        (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
            "sys.exit(0)\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: [sys.executable, str(parent_script)] if name == "pytest" else [sys.executable, "-c", "pass"],
    )
    report = run_verifier(tmp_path, phase=0, strict_clean=False, step_timeout_sec=0.6, total_timeout_sec=5.0)
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["exit_code"] == 124
    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    assert not _is_pid_alive(child_pid)


def test_spawn_managed_process_kills_rapid_grandchild_and_fails_closed_on_job_error(
    tmp_path: Path, monkeypatch
) -> None:
    """P1-001 & S2-002: Suspended-spawn Job Object prevents pre-attach race on rapid grandchildren and fails closed."""
    from harness.scripts import schema_utils

    _setup_mini_repo(tmp_path)
    grandchild_pid_file = tmp_path / "grandchild.pid"
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        (
            "import pathlib, os, time\n"
            f"pathlib.Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
            "time.sleep(30)\n"
        ),
        encoding="utf-8",
    )
    child_script = tmp_path / "child_spawn.py"
    child_script.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(grandchild_script)!r}])\nsys.exit(0)\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent_fast.py"
    parent_script.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child_script)!r}])\nsys.exit(0)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: [sys.executable, str(parent_script)] if name == "pytest" else [sys.executable, "-c", "pass"],
    )
    report = run_verifier(tmp_path, phase=0, strict_clean=False, step_timeout_sec=1.2, total_timeout_sec=5.0)
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["exit_code"] == 124
    assert grandchild_pid_file.is_file()
    gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
    assert not _is_pid_alive(gc_pid)

    # Part B: Simulate Win32 Job Object attach failure -> fail closed and kill suspended process
    if sys.platform == "win32":
        orig_cfg = schema_utils._configure_kernel32

        class _FailingKernel32:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def AssignProcessToJobObject(self, _job, _proc):
                return 0

        monkeypatch.setattr(schema_utils, "_configure_kernel32", lambda: _FailingKernel32(orig_cfg()))
        try:
            proc, _ = schema_utils.spawn_managed_process([sys.executable, "-c", "import time; time.sleep(30)"])
        except RuntimeError as exc:
            assert "Job Object" in str(exc)
        else:
            schema_utils.terminate_process_tree(proc)
            raise AssertionError("Expected RuntimeError when AssignProcessToJobObject fails")


def test_spawn_managed_process_attaches_job_before_resuming_process(monkeypatch) -> None:
    """P1-001: Verify exact call order CREATE_SUSPENDED -> AssignProcessToJobObject -> NtResumeProcess."""
    if sys.platform != "win32":
        return

    from harness.scripts import schema_utils

    events: list[str] = []

    class _FakeProc:
        def __init__(self) -> None:
            self.pid = 4321
            self._handle = 999

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float = 0.0) -> int:
            return 0

    def _fake_popen(cmd, **kwargs):
        assert int(kwargs.get("creationflags", 0)) & 0x00000004
        events.append("Popen(CREATE_SUSPENDED)")
        return _FakeProc()

    class _FakeKernel32:
        def AssignProcessToJobObject(self, job, proc_handle):
            assert int(job.value) == 777
            assert int(proc_handle.value) == 999
            events.append("AssignProcessToJobObject")
            return 1

        def CloseHandle(self, _handle):
            return 1

    class _FakeNtdll:
        def NtResumeProcess(self, proc_handle):
            assert int(proc_handle.value) == 999
            events.append("NtResumeProcess")
            return 0

    monkeypatch.setattr(schema_utils.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(schema_utils, "_configure_kernel32", lambda: _FakeKernel32())
    monkeypatch.setattr(schema_utils, "_create_kill_on_close_job", lambda _k32: 777)
    monkeypatch.setattr(schema_utils, "_configure_ntdll", lambda: _FakeNtdll())

    proc, job = schema_utils.spawn_managed_process(["echo", "hi"])
    assert proc.pid == 4321
    assert job == 777
    assert events == ["Popen(CREATE_SUSPENDED)", "AssignProcessToJobObject", "NtResumeProcess"]


def test_build_verifier_commit_blocks_uncommitted_module_leak_via_caller_pythonpath(
    tmp_path: Path, monkeypatch
) -> None:
    """P1-001: Dirty PYTHONPATH / live root modules missing from commit must NOT be importable in --commit mode."""
    _setup_mini_repo(
        tmp_path,
        test_code="import uncommitted_helper\n\ndef test_uses_uncommitted():\n    assert uncommitted_helper.VAL == 1\n",
    )
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "missing helper"], cwd=tmp_path, check=True, capture_output=True)

    # Create uncommitted_helper.py ONLY in dirty working tree + point caller PYTHONPATH to dirty root
    (tmp_path / "uncommitted_helper.py").write_text("VAL = 1\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    report = run_verifier(
        tmp_path,
        phase=0,
        strict_clean=False,
        commit_sha="HEAD",
        frozen_dir=tmp_path / "out_leak" / "frozen" / "topstep-chart-app",
    )
    assert report["status"] == "FAILED"
    pytest_step = next(s for s in report["steps"] if s["name"] == "harness_pytest")
    assert pytest_step["status"] == "FAILED"
