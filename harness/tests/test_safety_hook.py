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


def _invoke_hook_call(
    name: str,
    args: dict[str, object],
    cwd: str | None = None,
) -> dict[str, str]:
    payload: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "toolCall": {
            "id": "call_1",
            "name": name,
            "args": args,
        },
    }
    if cwd is not None:
        payload["cwd"] = cwd
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_hook_allows_safe_app_code() -> None:
    res = _invoke_hook(
        r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app\backend\app\engine\instruments.py",
        "MES_TICK_VALUE = 1.25\n",
    )
    assert res["decision"] == "allow"


def test_hook_denies_nc08_relative_and_swarm_worktree_readonly_bypasses() -> None:
    """Contract: Prop Firm Read-Only protection covers relative and swarm-worktree paths.

    Defect caught: NC-08 path-normalization bypass returning 'allow' on relative or worktree paths.
    """
    res_rel = _invoke_hook_call(
        "write_file",
        {
            "Path": "backend/app/api/routes_orders.py",
            "Content": 'await client.post("/api/Order/place")',
        },
        cwd=r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app",
    )
    assert res_rel["decision"] == "deny"

    res_wt = _invoke_hook_call(
        "write_file",
        {
            "Path": (
                r"C:\Users\Admin\.gemini\antigravity\worktrees\abc123\swarm-peer-r3"
                r"\backend\app\topstep\client.py"
            ),
            "Content": 'URL = "/api/Order/place"',
        },
        cwd=r"C:\Users\Admin\.gemini\antigravity\worktrees\abc123\swarm-peer-r3",
    )
    assert res_wt["decision"] == "deny"

    res_fe_rel = _invoke_hook_call(
        "edit_file",
        {
            "Path": "frontend/src/ws.ts",
            "Replacement": "export function submit() { return cancelOrder(); }",
        },
    )
    assert res_fe_rel["decision"] == "deny"

    res_mjs_rel = _invoke_hook_call(
        "write_file",
        {"Path": "server.mjs", "Content": 'const hub = "/hubs/user";'},
    )
    assert res_mjs_rel["decision"] == "deny"

    res_safe_rel = _invoke_hook_call(
        "write_file",
        {
            "Path": "backend/app/engine/calc.py",
            "Content": "def tick() -> float:\n    return 0.25\n",
        },
        cwd=r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app",
    )
    assert res_safe_rel["decision"] == "allow"


def test_hook_protects_peer_worktree_boundaries_and_model_policy() -> None:
    """Contract: Peer worktrees cannot edit protected harness/fork/env files or corrupt policy.

    Defect caught: Peer writing hooks.json, harness/runs, .env, fork, or invalid model-policy.
    """
    peer_cwd = r"C:\Users\Admin\.gemini\antigravity\worktrees\5e658d7c\swarm-peer-r3"
    coord_cwd = r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app"

    for protected_rel in (
        "../lightweight-charts/src/index.ts",
        ".env",
        ".agents/hooks.json",
        "harness/runs/R3/packet.md",
    ):
        res = _invoke_hook_call(
            "write_file",
            {"Path": protected_rel, "Content": "x"},
            cwd=peer_cwd,
        )
        assert res["decision"] == "deny", f"Expected deny for peer write to {protected_rel}"

    assert (
        _invoke_hook_call(
            "write_file",
            {"Path": ".agents/hooks.json", "Content": "{}"},
            cwd=coord_cwd,
        )["decision"]
        == "ask"
    )
    assert (
        _invoke_hook_call(
            "write_file",
            {"Path": "harness/runs/R3/packet.md", "Content": "# Packet"},
            cwd=coord_cwd,
        )["decision"]
        == "allow"
    )

    bad_policy = json.dumps(
        {
            "schema_version": "2.0.0",
            "excluded_model_prefixes": ["gemini-3.8-flash"],
            "roles": {"peer_default": {"selected_model": "gemini-3.8-flash-high"}},
        }
    )
    assert (
        _invoke_hook_call(
            "write_file",
            {"Path": "harness/model-policy.json", "Content": bad_policy},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "write_file",
            {"Path": "harness/model-policy.json", "Content": "{not-json"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )


def test_hook_guards_swarm_and_invoke_subagent_model_and_peer_dispatch() -> None:
    """Contract: Excluded models, missing swarm model, and peer-coder subagent dispatch are denied.

    Defect caught: Spawning Peer with excluded/missing model or legacy invoke_subagent flash tier.
    """
    coord_cwd = r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app"
    peer_cwd = r"C:\Users\Admin\.gemini\antigravity\worktrees\5e658d7c\swarm-peer-r3"

    assert (
        _invoke_hook_call(
            "swarm",
            {
                "action": "add",
                "name": "peer-1",
                "model": "gemini-3.8-flash-high",
                "workspace": "share",
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "swarm",
            {
                "action": "add",
                "name": "peer-1",
                "model": "gemini-4-argon-medium",
                "workspace": "share",
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "swarm",
            {"action": "add", "name": "peer-1", "workspace": "share"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "swarm",
            {
                "action": "add",
                "name": "peer-1",
                "model": "gemini-4-hydrogen-high",
                "workspace": "share",
            },
            cwd=coord_cwd,
        )["decision"]
        == "allow"
    )
    assert (
        _invoke_hook_call(
            "swarm",
            {
                "action": "add",
                "name": "helper",
                "model": "gemini-4-hydrogen-high",
                "workspace": "share",
            },
            cwd=peer_cwd,
        )["decision"]
        == "deny"
    )

    assert (
        _invoke_hook_call(
            "invoke_subagent",
            {"TypeName": "peer-coder", "Model": "gemini-4-hydrogen-high"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "invoke_subagent",
            {"TypeName": "general", "Role": "Peer coder", "Model": "gemini-4-hydrogen-high"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "invoke_subagent",
            {"TypeName": "general", "Model": "flash"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "invoke_subagent",
            {"TypeName": "browser", "Model": "flash"},
            cwd=coord_cwd,
        )["decision"]
        == "allow"
    )
    assert (
        _invoke_hook_call(
            "invoke_subagent",
            {"TypeName": "browser", "Model": "flash"},
            cwd=peer_cwd,
        )["decision"]
        == "deny"
    )


def test_hook_guards_run_command_without_blocking_search_or_tests() -> None:
    """Contract: run_command denies order network calls and protected writes, allows rg/pytest.

    Defect caught: Over-blocking diagnostic grep/pytest or allowing curl to /api/Order/place.
    """
    coord_cwd = r"D:\01_DCMvn\Workspace\Lightchart\topstep-chart-app"

    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "rg /api/Order backend"},
            cwd=coord_cwd,
        )["decision"]
        == "allow"
    )
    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "pytest harness/tests -q"},
            cwd=coord_cwd,
        )["decision"]
        == "allow"
    )
    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "curl -X POST http://127.0.0.1:8000/api/Order/place"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "echo '{}' > .agents/hooks.json"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "Set-Content ../lightweight-charts/src/x.ts bad"},
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
