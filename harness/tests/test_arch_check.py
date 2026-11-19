"""Value-first tests proving arch_check.py catches every architecture, security, and test-slop defect."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from harness.scripts.arch_check import run_checks

FIXED_TODAY = date(2026, 11, 20)


def _write(root: Path, rel: str, content: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _rule_ids(root: Path) -> set[str]:
    res = run_checks(root, use_baseline=False, today=FIXED_TODAY)
    return {v["rule_id"] for v in res["violations"]}


def test_clean_repository_passes_with_zero_violations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/app/engine/instruments.py",
        "from dataclasses import dataclass\n\n@dataclass(frozen=True)\nclass Spec:\n    tick: float\n",
    )
    _write(
        tmp_path,
        "frontend/src/drawings/fvg.ts",
        "export interface BoxSpec { high: number; low: number; }\n",
    )
    _write(
        tmp_path,
        "backend/tests/test_instruments.py",
        "def test_mes_tick_value():\n    assert 0.25 * 5.0 == 1.25\n",
    )
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    assert res["status"] == "PASSED"
    assert res["active_count"] == 0


def test_sec001_blocks_order_position_and_user_hub_endpoints(tmp_path: Path) -> None:
    _write(tmp_path, "backend/app/topstep/client.py", 'URL = "/api/Order/place"\n')
    _write(tmp_path, "frontend/src/api/hub.ts", 'export const HUB = "/hubs/user";\n')
    _write(tmp_path, "backend/app/api/orders.py", "def submit():\n    return placeOrder()\n")
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    sec001 = [v for v in res["violations"] if v["rule_id"] == "SEC-001"]
    assert res["status"] == "FAILED"
    assert len(sec001) == 3
    assert all("Read-Only" in v["fix"] for v in sec001)


def test_sec002_blocks_secrets_in_browser_and_localstorage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "frontend/src/auth.ts",
        'const key = "TOPSTEPX_API_KEY";\nlocalStorage.setItem("apiKey", key);\n',
    )
    _write(tmp_path, "index.html", "<script>const t = 'TELEGRAM_BOT_TOKEN';</script>\n")
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    sec002 = [v for v in res["violations"] if v["rule_id"] == "SEC-002"]
    assert len(sec002) == 3


def test_sec003_blocks_blind_all_interfaces_bind(tmp_path: Path) -> None:
    _write(tmp_path, "backend/app/main.py", 'uvicorn.run(app, host="0.0.0.0", port=8000)\n')
    _write(tmp_path, "server.mjs", "server.listen(PORT, () => {\n  console.log('up');\n});\n")
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    sec003 = [v for v in res["violations"] if v["rule_id"] == "SEC-003"]
    assert len(sec003) == 2


def test_layer_boundaries_arch001_to_arch005(tmp_path: Path) -> None:
    _write(tmp_path, "backend/app/engine/bad.py", "import fastapi\nfrom datetime import datetime\nnow = datetime.utcnow()\n")
    _write(tmp_path, "backend/app/storage/bad.py", "from app.api import routes\n")
    _write(tmp_path, "backend/app/topstep/bad.py", "from app.engine.backtester import run\n")
    _write(tmp_path, "frontend/src/drawings/bad.ts", "import { step } from '../replay/player';\n")
    ids = _rule_ids(tmp_path)
    assert {"ARCH-001", "ARCH-002", "ARCH-003", "ARCH-004", "ARCH-005"} <= ids


def test_arch006_catches_dirty_sibling_lightweight_charts(tmp_path: Path) -> None:
    app_root = tmp_path / "topstep-chart-app"
    lwc_root = tmp_path / "lightweight-charts"
    app_root.mkdir()
    lwc_root.mkdir()
    _write(lwc_root, "src/index.ts", "export const v = 1;\n")
    subprocess.run(["git", "-C", str(lwc_root), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(lwc_root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(lwc_root), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(lwc_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(lwc_root), "commit", "-m", "init"], check=True, capture_output=True)

    assert "ARCH-006" not in _rule_ids(app_root)

    _write(lwc_root, "src/index.ts", "export const v = 2;\n")
    assert "ARCH-006" in _rule_ids(app_root)


def test_slop001_and_slop002_enforce_exempt_expiry(tmp_path: Path) -> None:
    _write(tmp_path, "backend/app/a.py", "x = 1  # type: ignore\n")
    _write(
        tmp_path,
        "backend/app/b.py",
        "x = 1  # type: ignore EXEMPT(legacy, expires=2026-11-19, approved=neon)\n",
    )
    _write(
        tmp_path,
        "backend/app/c.py",
        "x = 1  # type: ignore EXEMPT(upstream stub, expires=2026-12-31, approved=neon)\n",
    )
    _write(tmp_path, "frontend/src/chart/bad.ts", "setTimeout(() => {}, 10);\n")
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    files_flagged = {(v["rule_id"], v["file"]) for v in res["violations"]}
    assert ("SLOP-001", "backend/app/a.py") in files_flagged
    assert ("SLOP-001", "backend/app/b.py") in files_flagged
    assert ("SLOP-001", "backend/app/c.py") not in files_flagged
    assert ("SLOP-002", "frontend/src/chart/bad.ts") in files_flagged


def test_test001_blocks_self_confirming_and_gate_weakening_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "backend/tests/test_bad.py",
        "\n".join(
            [
                "import pytest",
                "",
                "def test_tautology():",
                "    val = 42",
                "    assert True",
                "    assert val == val",
                "",
                "def test_swallow():",
                "    try:",
                "        1 / 0",
                "    except Exception:",
                "        pass",
                "",
                "@pytest.mark.skip(reason='broken')",
                "def test_skipped():",
                "    assert 1 == 2",
            ]
        )
        + "\n",
    )
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    test001 = [v for v in res["violations"] if v["rule_id"] == "TEST-001"]
    assert len(test001) == 4
