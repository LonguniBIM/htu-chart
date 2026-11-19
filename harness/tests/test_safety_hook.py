"""End-to-end stdin/stdout tests for Antigravity PreToolUse safety_hook.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "safety_hook.py"


def _invoke_hook(path: str, content: str = "") -> dict[str, str]:
    payload = {
        "hookEventName": "PreToolUse",
        "toolCall": {
            "id": "call_1",
            "name": "write_file",
            "args": {
                "Path": path,
                "Content": content,
            },
        },
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_hook_asks_before_modifying_lightweight_charts_or_env() -> None:
    res_lwc = _invoke_hook(r"D:\01_DCMvn\Workspace\Lightchart\lightweight-charts\src\index.ts", "x")
    assert res_lwc["decision"] == "ask"

    res_env = _invoke_hook(r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\.env", "TOPSTEPX_API_KEY=x")
    assert res_env["decision"] == "ask"

    res_prod_env = _invoke_hook(r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\.env.production", "X=1")
    assert res_prod_env["decision"] == "ask"

    res_example = _invoke_hook(r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\.env.example", "X=placeholder")
    assert res_example["decision"] == "allow"


def test_hook_denies_prop_firm_order_endpoints_in_app_code() -> None:
    res_be = _invoke_hook(
        r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\backend\app\topstep\client.py",
        'await client.post("/api/Order/place")',
    )
    assert res_be["decision"] == "deny"

    res_fe = _invoke_hook(
        r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\frontend\src\ws.ts",
        'const url = "wss://rtc.topstepx.com/hubs/user";',
    )
    assert res_fe["decision"] == "deny"


def test_hook_allows_safe_app_code() -> None:
    res = _invoke_hook(
        r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\backend\app\engine\instruments.py",
        "MES_TICK_VALUE = 1.25\n",
    )
    assert res["decision"] == "allow"
