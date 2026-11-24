"""Tests for frozen-mode frontend junction/symlink attach & teardown in build_verifier.py."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness.scripts import build_verifier
from harness.scripts.build_verifier import run_verifier

REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_repo_with_frontend(root: Path) -> None:
    app_dir = root / "topstep-chart-app"
    scripts_dir = app_dir / "harness" / "scripts"
    tests_dir = app_dir / "harness" / "tests"
    frontend_dir = app_dir / "frontend"
    backend_dir = app_dir / "backend"
    backend_tests = backend_dir / "tests"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)
    frontend_dir.mkdir(parents=True, exist_ok=True)
    backend_tests.mkdir(parents=True, exist_ok=True)

    (app_dir / "harness" / "__init__.py").write_text("", encoding="utf-8")
    (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "harness" / "scripts" / "arch_check.py", scripts_dir / "arch_check.py")
    (tests_dir / "test_sample.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    (backend_dir / "pyproject.toml").write_text('[project]\nname = "mini"\nversion = "0.1.0"\n', encoding="utf-8")
    (backend_tests / "test_ok.py").write_text("def test_backend_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (app_dir / ".gitignore").write_text("frontend/node_modules/\n", encoding="utf-8")
    (frontend_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "scripts": {
                    "typecheck": "echo typecheck",
                    "build": "echo build",
                    "lint": "echo lint",
                    "test": "echo vitest-ok",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=app_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "frontend base"], cwd=app_dir, check=True, capture_output=True)


def test_build_verifier_commit_attaches_and_detaches_frontend_junctions(
    tmp_path: Path, monkeypatch
) -> None:
    """Contract: Frozen --commit mode attaches and detaches node_modules and lightweight-charts links safely.

    Defect caught: cleanup_readonly_tree deleting live node_modules/upstream fork or missing junctions in %TEMP%.
    """
    _setup_repo_with_frontend(tmp_path)
    app_dir = tmp_path / "topstep-chart-app"
    live_node_modules = app_dir / "frontend" / "node_modules"
    live_node_modules.mkdir(parents=True)
    (live_node_modules / "pkg.txt").write_text("live-pkg", encoding="utf-8")

    live_lwc = tmp_path / "lightweight-charts"
    live_lwc.mkdir(parents=True)
    (live_lwc / "dist.txt").write_text("live-lwc", encoding="utf-8")

    observed: list[tuple[str, bool, bool]] = []
    probe_script = tmp_path / "probe_pnpm.py"
    probe_script.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                "args = sys.argv[1:]",
                "idx = args.index('--dir')",
                "frontend_dir = Path(args[idx + 1])",
                "script_name = args[-1]",
                "nm_ok = (frontend_dir / 'node_modules' / 'pkg.txt').is_file()",
                "lwc_ok = (frontend_dir.parent.parent / 'lightweight-charts' / 'dist.txt').is_file()",
                "print(f'{script_name}:nm={nm_ok}:lwc={lwc_ok}')",
                "sys.exit(0 if (nm_ok and lwc_ok) else 1)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    real_run_step = build_verifier._run_step

    def _spy_run_step(name: str, cmd: list[str], cwd: Path, **kwargs):
        res = real_run_step(name, cmd, cwd, **kwargs)
        if name.startswith("frontend_"):
            frozen_frontend = cwd / "frontend"
            observed.append(
                (
                    name,
                    (frozen_frontend / "node_modules" / "pkg.txt").is_file(),
                    (cwd.parent / "lightweight-charts" / "dist.txt").is_file(),
                )
            )
        return res

    monkeypatch.setattr(build_verifier, "_run_step", _spy_run_step)
    monkeypatch.setattr(build_verifier, "_resolve_backend_venv_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: (
            [sys.executable, str(probe_script)]
            if name == "pnpm"
            else [sys.executable, "-m", python_module]
            if python_module
            else [sys.executable, "-c", "pass"]
        ),
    )

    report = run_verifier(app_dir, phase=3, strict_clean=False, commit_sha="HEAD")
    assert report["status"] == "VERIFIED"
    assert [row[0] for row in observed] == [
        "frontend_typecheck",
        "frontend_build",
        "frontend_lint",
        "frontend_test",
    ]
    assert all(nm_ok and lwc_ok for _, nm_ok, lwc_ok in observed)
    assert (live_node_modules / "pkg.txt").exists()
    assert (live_lwc / "dist.txt").exists()

    # Also verify explicit frozen_dir (--out) mode detaches both links after completion
    frozen_out = tmp_path / "out" / "frozen" / "topstep-chart-app"
    report_out = run_verifier(
        app_dir,
        phase=3,
        strict_clean=False,
        commit_sha="HEAD",
        frozen_dir=frozen_out,
    )
    assert report_out["status"] == "VERIFIED"
    assert not (frozen_out / "frontend" / "node_modules").exists()
    assert not (frozen_out.parent / "lightweight-charts").exists()
    assert (live_node_modules / "pkg.txt").exists()
    assert (live_lwc / "dist.txt").exists()


def test_build_verifier_commit_detaches_frontend_junctions_on_step_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Contract: Teardown detaches both junctions before cleanup even when a frontend step fails.

    Defect caught: Exception or failure leaving live node_modules/lightweight-charts exposed to cleanup.
    """
    _setup_repo_with_frontend(tmp_path)
    app_dir = tmp_path / "topstep-chart-app"
    live_node_modules = app_dir / "frontend" / "node_modules"
    live_node_modules.mkdir(parents=True)
    (live_node_modules / "pkg.txt").write_text("live-pkg", encoding="utf-8")

    live_lwc = tmp_path / "lightweight-charts"
    live_lwc.mkdir(parents=True)
    (live_lwc / "dist.txt").write_text("live-lwc", encoding="utf-8")

    monkeypatch.setattr(build_verifier, "_resolve_backend_venv_python", lambda _root: Path(sys.executable))
    monkeypatch.setattr(
        build_verifier,
        "_resolve_tool",
        lambda name, python_module=None: (
            [sys.executable, "-c", "import sys; sys.exit(1)"]
            if name == "pnpm"
            else [sys.executable, "-m", python_module]
            if python_module
            else [sys.executable, "-c", "pass"]
        ),
    )

    frozen_out = tmp_path / "fail_out" / "frozen" / "topstep-chart-app"
    report = run_verifier(
        app_dir,
        phase=3,
        strict_clean=False,
        commit_sha="HEAD",
        frozen_dir=frozen_out,
    )
    assert report["status"] == "FAILED"
    assert not (frozen_out / "frontend" / "node_modules").exists()
    assert not (frozen_out.parent / "lightweight-charts").exists()
    assert (live_node_modules / "pkg.txt").exists()
    assert (live_lwc / "dist.txt").exists()
