"""Value-first tests proving arch_check.py catches every architecture, security, and test-slop defect."""

from __future__ import annotations

import json
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


def _write_valid_policy(root: Path) -> None:
    policy = {
        "schema_version": "2.0.0",
        "effective_date": "2026-11-24",
        "excluded_model_prefixes": [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "gemini-pro-orchestrator-argon",
            "gemini-3.7-flash",
            "gemini-2.5-pro",
        ],
        "discontinued_peer_subagent_tiers": ["flash", "flash_lite"],
        "surface_lifecycle_notes": {
            "antigravity_withdrawal": "Gemini 3.8 Flash and Gemini 4 Argon withdrawn 2026-11-30.",
            "gemini_api_lifecycle": "gemini-3.7-flash and gemini-2.5-pro redirects excluded.",
        },
        "roles": {
            "cli_coordinator": {"selected_model": "gemini-4-neon-medium"},
            "peer_default": {"dispatch_tool": "swarm", "selected_model": "gemini-4-hydrogen-high"},
            "independent_reviewer_fallbacks": {"qualified_routes": []},
        },
    }
    _write(root, "harness/model-policy.json", json.dumps(policy, indent=2) + "\n")


def test_mod001_detects_active_excluded_models_and_allows_prohibition_prose(tmp_path: Path) -> None:
    """Contract: MOD-001 flags active excluded models while allowing denylists/prohibitions.

    Defect caught: Active SKILL.md/docs recommending excluded IDs/display names or flash dispatch.
    """
    _write_valid_policy(tmp_path)
    _write(
        tmp_path,
        ".agents/skills/slp-orchestrator/SKILL.md",
        '# S-L-P Orchestrator\n- Default: `invoke_subagent(..., Model: "flash")`\n',
    )
    _write(
        tmp_path,
        ".agents/skills/other-skill/SKILL.md",
        '- Parallel: `swarm{action: "add", model: "gemini-3.8-flash-high"}`\n',
    )
    _write(
        tmp_path,
        ".agents/skills/display-skill/SKILL.md",
        "- Coordinator: Gemini 4 Argon Medium\n",
    )
    _write(
        tmp_path,
        ".agents/skills/orchestrator-argon-skill/SKILL.md",
        "- Mode: Orchestrator (Argon)\n- Inline: `Orchestrator (Argon)`\n",
    )
    _write(
        tmp_path,
        "harness/README.md",
        "Use `gemini-4-argon-high` for fallback.\n",
    )
    _write(
        tmp_path,
        "AGENTS.md",
        "Never select excluded models `gemini-3.8-flash`, `gemini-4-argon`, or `gemini-2.5-pro`.\n",
    )
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    mod001 = [v for v in res["violations"] if v["rule_id"] == "MOD-001"]
    flagged_pairs = {(v["file"], v["line"]) for v in mod001}
    assert (".agents/skills/slp-orchestrator/SKILL.md", 2) in flagged_pairs
    assert (".agents/skills/other-skill/SKILL.md", 1) in flagged_pairs
    assert (".agents/skills/display-skill/SKILL.md", 1) in flagged_pairs
    assert (".agents/skills/orchestrator-argon-skill/SKILL.md", 1) in flagged_pairs
    assert (".agents/skills/orchestrator-argon-skill/SKILL.md", 2) in flagged_pairs
    assert ("harness/README.md", 1) in flagged_pairs
    assert all(v["file"] not in {"AGENTS.md", "harness/model-policy.json"} for v in mod001)


def test_mod001_ast_denylist_skip_does_not_leak_after_escaped_paren_regex(tmp_path: Path) -> None:
    """Contract: Escaped regex parens in MOD001_* constants do not disable MOD-001 below them.

    Defect caught: P1-002 raw paren counter leaking skip state across the rest of a Python file.
    """
    _write_valid_policy(tmp_path)
    _write(
        tmp_path,
        "harness/scripts/sample.py",
        "\n".join(
            [
                "import re",
                'MOD001_SAMPLE_RE = re.compile(r"foo\\(")',
                'ACTIVE = "gemini-3.8-flash-high"',
            ]
        )
        + "\n",
    )
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    mod001 = [(v["file"], v["line"]) for v in res["violations"] if v["rule_id"] == "MOD-001"]
    assert ("harness/scripts/sample.py", 3) in mod001


def test_mod001_flags_excluded_model_in_active_policy_roles_or_routes(tmp_path: Path) -> None:
    """Contract: Valid excluded_model_prefixes cannot mask excluded models in active roles/routes.

    Defect caught: MOD-001 policy role/route validation skipped when prefixes list looks valid.
    """
    bad_policy = {
        "schema_version": "2.0.0",
        "excluded_model_prefixes": [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "gemini-pro-orchestrator-argon",
            "gemini-3.7-flash",
            "gemini-2.5-pro",
        ],
        "roles": {
            "app_coordinator": {"selected_mode": "Orchestrator (Argon)"},
            "peer_default": {"selected_model": "gemini-3.8-flash-high"},
            "independent_reviewer_fallbacks": {
                "qualified_routes": [{"model": "Gemini 4 Argon Medium"}]
            },
        },
    }
    _write(tmp_path, "harness/model-policy.json", json.dumps(bad_policy, indent=2) + "\n")
    res = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    mod001 = [v for v in res["violations"] if v["rule_id"] == "MOD-001"]
    assert len(mod001) == 3
    assert all(v["file"] == "harness/model-policy.json" for v in mod001)


def test_mod001_flags_missing_or_corrupt_model_policy_json(tmp_path: Path) -> None:
    """Contract: Missing or corrupt harness/model-policy.json triggers MOD-001 violation.

    Defect caught: Unnoticed policy deletion or malformed JSON bypassing model governance.
    """
    _write(tmp_path, "AGENTS.md", "# Governed Repository\n")
    res_missing = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    assert any(v["rule_id"] == "MOD-001" for v in res_missing["violations"])

    _write(tmp_path, "harness/model-policy.json", "{corrupt json")
    res_corrupt = run_checks(tmp_path, use_baseline=False, today=FIXED_TODAY)
    assert any(v["rule_id"] == "MOD-001" for v in res_corrupt["violations"])


def test_mod001_passes_on_repository_root() -> None:
    """Contract: Repository skills, docs, and policy contain zero active MOD-001 violations.

    Defect caught: Pre-fix SKILL.md:15-16 and missing model-policy.json failing MOD-001 gate.
    """
    repo_root = Path(__file__).resolve().parents[2]
    res = run_checks(repo_root, use_baseline=False, today=FIXED_TODAY)
    mod001 = [v for v in res["violations"] if v["rule_id"] == "MOD-001"]
    assert mod001 == []
