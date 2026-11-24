"""End-to-end stdin/stdout tests for Antigravity PreToolUse safety_hook.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "harness" / "scripts" / "safety_hook.py"


def _invoke_hook_with_script(
    script: Path,
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
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def _invoke_hook(path: str, content: str = "", cwd: str | None = None) -> dict[str, str]:
    return _invoke_hook_with_script(
        HOOK_SCRIPT,
        "write_file",
        {"Path": path, "Content": content},
        cwd=cwd,
    )


def _invoke_hook_call(
    name: str,
    args: dict[str, object],
    cwd: str | None = None,
) -> dict[str, str]:
    return _invoke_hook_with_script(HOOK_SCRIPT, name, args, cwd=cwd)


def test_hook_asks_before_modifying_lightweight_charts_or_env(tmp_path: Path) -> None:
    coord_cwd = str(tmp_path / "topstep-chart-app")
    lwc_file = str(tmp_path / "lightweight-charts" / "src" / "index.ts")
    env_file = str(tmp_path / "topstep-chart-app" / ".env")
    prod_env_file = str(tmp_path / "topstep-chart-app" / ".env.production")
    example_env_file = str(tmp_path / "topstep-chart-app" / ".env.example")

    assert _invoke_hook(lwc_file, "x", cwd=coord_cwd)["decision"] == "ask"
    assert _invoke_hook(env_file, "TOPSTEPX_API_KEY=x", cwd=coord_cwd)["decision"] == "ask"
    assert _invoke_hook(prod_env_file, "X=1", cwd=coord_cwd)["decision"] == "ask"
    assert _invoke_hook(example_env_file, "X=placeholder", cwd=coord_cwd)["decision"] == "allow"


def test_hook_denies_prop_firm_order_endpoints_in_app_code(tmp_path: Path) -> None:
    coord_cwd = str(tmp_path / "topstep-chart-app")
    res_be = _invoke_hook(
        str(tmp_path / "topstep-chart-app" / "backend" / "app" / "topstep" / "client.py"),
        'await client.post("/api/Order/place")',
        cwd=coord_cwd,
    )
    assert res_be["decision"] == "deny"

    res_fe = _invoke_hook(
        str(tmp_path / "topstep-chart-app" / "frontend" / "src" / "ws.ts"),
        'const url = "wss://rtc.topstepx.com/hubs/user";',
        cwd=coord_cwd,
    )
    assert res_fe["decision"] == "deny"


def test_hook_allows_safe_app_code(tmp_path: Path) -> None:
    coord_cwd = str(tmp_path / "topstep-chart-app")
    res = _invoke_hook(
        str(tmp_path / "topstep-chart-app" / "backend" / "app" / "engine" / "instruments.py"),
        "MES_TICK_VALUE = 1.25\n",
        cwd=coord_cwd,
    )
    assert res["decision"] == "allow"


def test_hook_denies_nc08_relative_and_swarm_worktree_readonly_bypasses(tmp_path: Path) -> None:
    """Contract: Prop Firm Read-Only protection covers relative and swarm-worktree paths.

    Defect caught: NC-08 path-normalization bypass returning 'allow' on relative or worktree paths.
    """
    coord_cwd = str(tmp_path / "topstep-chart-app")
    peer_cwd = str(tmp_path / "worktrees" / "abc123" / "swarm-peer-r3")

    res_rel = _invoke_hook_call(
        "write_file",
        {
            "Path": "backend/app/api/routes_orders.py",
            "Content": 'await client.post("/api/Order/place")',
        },
        cwd=coord_cwd,
    )
    assert res_rel["decision"] == "deny"

    res_wt = _invoke_hook_call(
        "write_file",
        {
            "Path": str(Path(peer_cwd) / "backend" / "app" / "topstep" / "client.py"),
            "Content": 'URL = "/api/Order/place"',
        },
        cwd=peer_cwd,
    )
    assert res_wt["decision"] == "deny"

    res_fe_rel = _invoke_hook_call(
        "edit_file",
        {
            "Path": "frontend/src/ws.ts",
            "Replacement": "export function submit() { return cancelOrder(); }",
        },
        cwd=coord_cwd,
    )
    assert res_fe_rel["decision"] == "deny"

    res_mjs_rel = _invoke_hook_call(
        "write_file",
        {"Path": "server.mjs", "Content": 'const hub = "/hubs/user";'},
        cwd=coord_cwd,
    )
    assert res_mjs_rel["decision"] == "deny"

    res_safe_rel = _invoke_hook_call(
        "write_file",
        {
            "Path": "backend/app/engine/calc.py",
            "Content": "def tick() -> float:\n    return 0.25\n",
        },
        cwd=coord_cwd,
    )
    assert res_safe_rel["decision"] == "allow"


def test_hook_denies_readonly_inside_frozen_harness_runs_checkout(tmp_path: Path) -> None:
    """Contract: Frozen checkout nested under harness/runs/ still enforces SEC-001 on app code.

    Defect caught: G3-001 ancestor '/harness/runs/' short-circuit returning 'allow' on frozen tree.
    """
    frozen_root = tmp_path / "harness" / "runs" / "attempt-1" / "frozen" / "topstep-chart-app"
    copied_hook = frozen_root / "harness" / "scripts" / "safety_hook.py"
    copied_hook.parent.mkdir(parents=True, exist_ok=True)
    copied_hook.write_text(HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    res = _invoke_hook_with_script(
        copied_hook,
        "write_file",
        {
            "Path": "backend/app/api/routes_orders.py",
            "Content": 'await client.post("/api/Order/place")',
        },
        cwd=str(frozen_root),
    )
    assert res["decision"] == "deny"


def test_hook_omitted_or_relative_cwd_in_peer_worktree_enforces_peer_deny(tmp_path: Path) -> None:
    """Contract: Omitted or relative cwd inside a Peer worktree still sets is_peer=True.

    Defect caught: P1-005 raw cwd_str check missing peer worktree status when cwd is omitted/'.'.
    """
    peer_root = tmp_path / "worktrees" / "run1" / "swarm-peer-x"
    copied_hook = peer_root / "harness" / "scripts" / "safety_hook.py"
    copied_hook.parent.mkdir(parents=True, exist_ok=True)
    copied_hook.write_text(HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    for cwd_arg in (None, "."):
        res = _invoke_hook_with_script(
            copied_hook,
            "write_file",
            {"Path": "../lightweight-charts/src/index.ts", "Content": "x"},
            cwd=cwd_arg,
        )
        assert res["decision"] == "deny"


def test_hook_protects_peer_worktree_boundaries_and_model_policy(tmp_path: Path) -> None:
    """Contract: Peer worktrees cannot edit protected harness/fork/env files or corrupt policy.

    Defect caught: Peer writing hooks.json, harness/runs, .env, fork, or invalid model-policy.
    """
    peer_cwd = str(tmp_path / "worktrees" / "5e658d7c" / "swarm-peer-r3")
    coord_root = tmp_path / "topstep-chart-app"
    coord_cwd = str(coord_root)

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

    valid_policy = {
        "schema_version": "2.0.0",
        "excluded_model_prefixes": [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "gemini-pro-orchestrator-argon",
            "gemini-3.7-flash",
            "gemini-2.5-pro",
        ],
        "roles": {
            "peer_default": {"selected_model": "gemini-4-hydrogen-high"},
            "peer_low_risk": {"selected_model": "gemini-4-hydrogen-low"},
        },
    }
    policy_path = coord_root / "harness" / "model-policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps(valid_policy, indent=2) + "\n", encoding="utf-8")

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
    # P1-002: Append=True, single-block replace, AllowMultiple=True, and missing target
    assert (
        _invoke_hook_call(
            "write_file",
            {
                "Path": "harness/model-policy.json",
                "Content": json.dumps(valid_policy),
                "Append": True,
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "replace_file_content",
            {
                "TargetFile": "harness/model-policy.json",
                "TargetContent": '"gemini-4-hydrogen-high"',
                "ReplacementContent": '"gemini-3.8-flash-high"',
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "multi_replace_file_content",
            {
                "TargetFile": "harness/model-policy.json",
                "ReplacementChunks": [
                    {
                        "TargetContent": "gemini-4-hydrogen",
                        "ReplacementContent": "gemini-4-argon",
                        "AllowMultiple": True,
                    }
                ],
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )
    assert (
        _invoke_hook_call(
            "replace_file_content",
            {
                "TargetFile": "harness/model-policy.json",
                "TargetContent": '"nonexistent-target"',
                "ReplacementContent": '"gemini-4-hydrogen-high"',
            },
            cwd=coord_cwd,
        )["decision"]
        == "deny"
    )


def test_hook_guards_swarm_and_invoke_subagent_model_and_peer_dispatch(tmp_path: Path) -> None:
    """Contract: Excluded models, missing swarm model, and peer-coder subagent dispatch are denied.

    Defect caught: Spawning Peer with excluded/missing/display-name model or legacy flash tier.
    """
    coord_cwd = str(tmp_path / "topstep-chart-app")
    peer_cwd = str(tmp_path / "worktrees" / "5e658d7c" / "swarm-peer-r3")

    for bad_model in (
        "gemini-3.8-flash-high",
        "gemini-4-argon-medium",
        "Gemini 4 Argon Medium",
        "Gemini 3.8 Flash (High)",
        "Orchestrator (Argon)",
    ):
        assert (
            _invoke_hook_call(
                "swarm",
                {
                    "action": "add",
                    "name": "peer-1",
                    "model": bad_model,
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
            {"TypeName": "research", "Model": "Gemini 4 Argon Medium"},
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


def test_hook_guards_run_command_without_blocking_search_or_tests(tmp_path: Path) -> None:
    """Contract: run_command denies order network calls and protected writes, allows safe cmds.

    Defect caught: Over-blocking rg/pytest/git commit/2>&1 or allowing curl/redirection to secrets.
    """
    coord_cwd = str(tmp_path / "topstep-chart-app")

    for safe_cmd in (
        "rg /api/Order backend",
        "pytest harness/tests -q",
        'git commit -m "Deploy .agents/hooks.json (RED -> GREEN)"',
        "git status ../lightweight-charts 2>&1",
    ):
        assert (
            _invoke_hook_call(
                "run_command",
                {"CommandLine": safe_cmd},
                cwd=coord_cwd,
            )["decision"]
            == "allow"
        )

    for bad_cmd in (
        "curl -X POST http://127.0.0.1:8000/api/Order/place",
        "echo '{}' > .agents/hooks.json",
        "echo '{}' > .agents//hooks.json",
        "echo '{}' > harness/model-policy.json",
        "echo x >> .env",
        'git commit -m "ok" && echo bad > .env',
        'git commit -m "ok"\ncurl -X POST http://127.0.0.1:8000/api/Order/place',
        'git commit -m "$(Invoke-RestMethod http://127.0.0.1:8000/api/Order/place)"',
        'git commit -m "ok" > .agents/hooks.json',
        "git -C ../lightweight-charts status",
        "cp .env .env.leak",
        "Tee-Object .agents/hooks.json",
        "Set-Content ../lightweight-charts/src/x.ts bad",
    ):
        assert (
            _invoke_hook_call(
                "run_command",
                {"CommandLine": bad_cmd},
                cwd=coord_cwd,
            )["decision"]
            == "deny"
        )

    peer_cwd = str(tmp_path / "worktrees" / "5e658d7c" / "swarm-peer-r3")
    assert (
        _invoke_hook_call(
            "run_command",
            {"CommandLine": "echo x > harness/runs/R3/packet.md"},
            cwd=peer_cwd,
        )["decision"]
        == "deny"
    )


def test_hook_fails_closed_on_corrupt_disk_policy_and_non_object_payload(
    tmp_path: Path,
) -> None:
    """Contract: Corrupt on-disk model-policy.json and non-dict stdin payloads fail closed.

    Defect caught: Corrupt policy bypassed or AttributeError on JSON array/null stdin payload.
    """
    coord_root = tmp_path / "topstep-chart-app"
    pol = coord_root / "harness" / "model-policy.json"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text("{corrupt", encoding="utf-8")

    res_corrupt = _invoke_hook_call(
        "write_file",
        {"Path": "backend/app/engine/calc.py", "Content": "X = 1\n"},
        cwd=str(coord_root),
    )
    assert res_corrupt["decision"] == "deny"

    for raw_stdin in ("[]", "null"):
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=raw_stdin,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(proc.stdout.strip())["decision"] == "deny"

    wt_mjs = _invoke_hook_call(
        "write_file",
        {
            "Path": str(tmp_path / "worktrees" / "run9" / "topic" / "server.mjs"),
            "Content": 'const u = "/hubs/user";',
        },
        cwd=str(coord_root),
    )
    assert wt_mjs["decision"] == "deny"
