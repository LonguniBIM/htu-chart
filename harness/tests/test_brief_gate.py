"""Tests for brief_gate.py G0.5 Layer 1 & Layer 2 validation and receipt compatibility."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from harness.scripts import brief_gate, receipt
from harness.scripts.schema_utils import run_managed_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> str:
    git_bin = shutil.which("git") or "git"
    proc = run_managed_cmd([git_bin, *args], cwd=cwd, timeout_sec=30.0)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _init_git_repo(root: Path, extra_files: dict[str, str] | None = None) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    prompt_path = (
        root / ".agents" / "skills" / "slp-orchestrator" / "references" / "peer-system-prompt.md"
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("# Peer System Prompt\nStay strictly read-only.\n", encoding="utf-8")

    existing = root / "backend" / "app" / "domain.py"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("def calc() -> int:\n    return 1\n", encoding="utf-8")

    if extra_files:
        for rel, content in extra_files.items():
            fp = root / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")

    _git(root, "add", ".")
    _git(root, "commit", "-m", "base commit")
    return _git(root, "rev-parse", "HEAD")


def _render_packet(
    base_sha: str,
    *,
    root_task_id: str = "T-PURE-01",
    task_id: str = "T-PURE-01",
    packet_version: str = "1",
    phase: str = "1",
    is_bugfix: str = "No",
    risk_tags: str = "None",
    budget_usd: str = "4.00",
    owned_lines: list[str] | None = None,
    denied_lines: list[str] | None = None,
    extra_sections: str = "",
    sensitivity_line: str = (
        '- "When pure domain calc runs, if formula regresses, '
        'test fails at assert calc() == 1, because contract requires 1."'
    ),
    checks_section: str = "## 4. Required Local Checks\n- `pytest backend/tests -q`\n",
    peer_prompt_sha256: str | None = None,
    omit_root_task_id: bool = False,
) -> str:
    owned = owned_lines or ["  - `backend/app/domain.py` (modify)"]
    denied = denied_lines or ["  - `frontend/**`", "  - `../lightweight-charts/**`"]
    lines = [f"# Task Packet: `{task_id}`", ""]
    if not omit_root_task_id:
        lines.append(f"- **Root Task ID**: `{root_task_id}`")
    lines.extend(
        [
            f"- **Task ID**: `{task_id}`",
            f"- **Packet Version**: `{packet_version}`",
            f"- **Phase**: `{phase}`",
            "- **Audit Refs**: `None`",
            f"- **Base SHA**: `{base_sha}`",
            f"- **Is Bugfix**: `{is_bugfix}`",
            f"- **Risk Tags**: `{risk_tags}`",
            f"- **Budget USD**: `{budget_usd}`",
        ]
    )
    if peer_prompt_sha256 is not None:
        lines.append(f"- **Peer Prompt SHA256**: `{peer_prompt_sha256}`")
    lines.extend(
        [
            "",
            "## 1. Outcome & Behavioral Contract",
            "Pure domain behavior.",
            extra_sections,
            "",
            "## 2. Write Scope & Do-Not-Touch",
            "- **Owned Paths (may create/edit)**:",
            *owned,
            "- **Denied Paths**:",
            *denied,
            "",
            "## 3. Acceptance & Test Contract",
            sensitivity_line,
            "",
            checks_section,
        ]
    )
    return "\n".join(lines)


def test_missing_empty_and_whitespace_packets_return_blocked_brief(tmp_path: Path) -> None:
    """Missing, 0-byte, and whitespace-only packets must halt at G0.5 with rc=2."""
    base_sha = _init_git_repo(tmp_path)
    missing = tmp_path / "missing.md"
    assert brief_gate.main(["--packet", str(missing), "--repo-root", str(tmp_path)]) == 2

    zero = tmp_path / "zero.md"
    zero.write_bytes(b"")
    out_zero = tmp_path / "out_zero.json"
    rc_zero = brief_gate.main(
        ["--packet", str(zero), "--repo-root", str(tmp_path), "--out", str(out_zero)]
    )
    assert rc_zero == 2
    res_zero = json.loads(out_zero.read_text(encoding="utf-8"))
    assert res_zero["status"] == "BLOCKED_BRIEF"
    assert res_zero["base_sha"] == ""

    ws = tmp_path / "whitespace.md"
    ws.write_text("   \n\t \n", encoding="utf-8")
    out_ws = tmp_path / "out_ws.json"
    rc_ws = brief_gate.main(
        ["--packet", str(ws), "--repo-root", str(tmp_path), "--out", str(out_ws)]
    )
    assert rc_ws == 2
    assert json.loads(out_ws.read_text(encoding="utf-8"))["status"] == "BLOCKED_BRIEF"
    assert base_sha


def test_layer1_structural_and_scope_overlap_block(tmp_path: Path) -> None:
    """Missing headers, unreachable SHA, overlap, traversal, and bad policy block at rc=2."""
    base_sha = _init_git_repo(tmp_path)

    # 1. Missing Root Task ID
    p_no_root = tmp_path / "p_no_root.md"
    p_no_root.write_text(_render_packet(base_sha, omit_root_task_id=True), encoding="utf-8")
    res_no_root = brief_gate.validate_brief(p_no_root, repo_root=tmp_path)
    assert res_no_root["status"] == "BLOCKED_BRIEF"

    # 2. Unreachable base_sha
    p_bad_sha = tmp_path / "p_bad_sha.md"
    p_bad_sha.write_text(_render_packet("f" * 40), encoding="utf-8")
    assert brief_gate.main(["--packet", str(p_bad_sha), "--repo-root", str(tmp_path)]) == 2

    # 3. Missing (new)/(modify) marker
    p_no_mode = tmp_path / "p_no_mode.md"
    p_no_mode.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/app/domain.py`"]),
        encoding="utf-8",
    )
    assert brief_gate.main(["--packet", str(p_no_mode), "--repo-root", str(tmp_path)]) == 2

    # 4. Overlap owned vs denied glob
    p_overlap = tmp_path / "p_overlap.md"
    p_overlap.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `backend/app/domain.py` (modify)"],
            denied_lines=["  - `backend/**`"],
        ),
        encoding="utf-8",
    )
    res_overlap = brief_gate.validate_brief(p_overlap, repo_root=tmp_path)
    assert res_overlap["status"] == "BLOCKED_BRIEF"
    assert any("overlap" in err.lower() for err in res_overlap["errors"])

    # 5. Traversal in owned_paths
    p_trav = tmp_path / "p_trav.md"
    p_trav.write_text(
        _render_packet(base_sha, owned_lines=["  - `../escape.py` (new)"]),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_trav, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # 6. Peer Prompt SHA256 mismatch
    p_prompt = tmp_path / "p_prompt.md"
    p_prompt.write_text(_render_packet(base_sha, peer_prompt_sha256="0" * 64), encoding="utf-8")
    assert brief_gate.validate_brief(p_prompt, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # 7. Invalid --policy JSON
    bad_policy = tmp_path / "bad_policy.json"
    bad_policy.write_text("{broken json", encoding="utf-8")
    p_ok = tmp_path / "p_ok.md"
    p_ok.write_text(_render_packet(base_sha), encoding="utf-8")
    rc_pol = brief_gate.main(
        ["--packet", str(p_ok), "--repo-root", str(tmp_path), "--policy", str(bad_policy)]
    )
    assert rc_pol == 2


def test_git_blob_path_mode_mismatch_returns_needs_replan(tmp_path: Path) -> None:
    """(modify) missing in base_sha or (new) already committed must return NEEDS_REPLAN (rc=1)."""
    base_sha = _init_git_repo(tmp_path)

    p_missing_mod = tmp_path / "p_missing_mod.md"
    p_missing_mod.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/app/does_not_exist.py` (modify)"]),
        encoding="utf-8",
    )
    out1 = tmp_path / "r1.json"
    rc1 = brief_gate.main(
        ["--packet", str(p_missing_mod), "--repo-root", str(tmp_path), "--out", str(out1)]
    )
    assert rc1 == 1
    assert json.loads(out1.read_text(encoding="utf-8"))["status"] == "NEEDS_REPLAN"

    p_dup_new = tmp_path / "p_dup_new.md"
    p_dup_new.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/app/domain.py` (new)"]),
        encoding="utf-8",
    )
    out2 = tmp_path / "r2.json"
    rc2 = brief_gate.main(
        ["--packet", str(p_dup_new), "--repo-root", str(tmp_path), "--out", str(out2)]
    )
    assert rc2 == 1
    assert json.loads(out2.read_text(encoding="utf-8"))["status"] == "NEEDS_REPLAN"


def test_layer2_risk_contracts_and_model_eligibility(tmp_path: Path) -> None:
    """Risk-flagged packets require contracts and exclude gemini-4-hydrogen-low."""
    base_sha = _init_git_repo(
        tmp_path,
        extra_files={
            "backend/app/ws_broadcaster.py": "def run():\n    pass\n",
            "backend/app/session_calendar.py": "def rth():\n    pass\n",
            "backend/app/security.py": "def check():\n    pass\n",
        },
    )

    # Concurrency path without Atomicity/Rollback/Concurrency Contract
    p_ws = tmp_path / "p_ws.md"
    p_ws.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/app/ws_broadcaster.py` (modify)"]),
        encoding="utf-8",
    )
    out_ws = tmp_path / "out_ws.json"
    rc_ws = brief_gate.main(
        ["--packet", str(p_ws), "--repo-root", str(tmp_path), "--out", str(out_ws)]
    )
    res_ws = json.loads(out_ws.read_text(encoding="utf-8"))
    assert rc_ws == 1
    assert res_ws["status"] == "NEEDS_REPLAN"
    assert res_ws["risk_flags"]["touches_concurrency_or_ws"] is True
    assert "gemini-4-hydrogen-low" not in res_ws["eligible_peer_models"]
    assert "gemini-4-hydrogen-high" in res_ws["eligible_peer_models"]

    # Bugfix without Pre-Fix RED section
    p_bug = tmp_path / "p_bug.md"
    p_bug.write_text(_render_packet(base_sha, is_bugfix="Yes"), encoding="utf-8")
    res_bug = brief_gate.validate_brief(p_bug, repo_root=tmp_path)
    assert res_bug["status"] == "NEEDS_REPLAN"
    assert res_bug["risk_flags"]["is_bugfix"] is True
    assert "gemini-4-hydrogen-low" not in res_bug["eligible_peer_models"]

    # Security + Session Calendar missing contracts
    p_sec_cal = tmp_path / "p_sec_cal.md"
    p_sec_cal.write_text(
        _render_packet(
            base_sha,
            owned_lines=[
                "  - `backend/app/security.py` (modify)",
                "  - `backend/app/session_calendar.py` (modify)",
            ],
        ),
        encoding="utf-8",
    )
    res_sc = brief_gate.validate_brief(p_sec_cal, repo_root=tmp_path)
    assert res_sc["status"] == "NEEDS_REPLAN"
    assert res_sc["risk_flags"]["touches_security_or_auth"] is True
    assert res_sc["risk_flags"]["touches_session_calendar"] is True
    assert "gemini-4-hydrogen-low" not in res_sc["eligible_peer_models"]

    # Adding all required contracts turns status VALID while still blocking hydrogen-low
    contracts = (
        "\n### Atomicity / Rollback / Concurrency Contract\n"
        "Use asyncio locks and cancellation rollback.\n"
        "### Security / Read-Only Invariants\n"
        "Enforce SEC-001 and F01 loopback bind.\n"
        "### Session Calendar Contract\n"
        "Use America/New_York 18:00 Globex boundary (R05).\n"
    )
    p_full = tmp_path / "p_full.md"
    p_full.write_text(
        _render_packet(
            base_sha,
            owned_lines=[
                "  - `backend/app/ws_broadcaster.py` (modify)",
                "  - `backend/app/security.py` (modify)",
                "  - `backend/app/session_calendar.py` (modify)",
            ],
            extra_sections=contracts,
        ),
        encoding="utf-8",
    )
    res_full = brief_gate.validate_brief(p_full, repo_root=tmp_path)
    assert res_full["status"] == "VALID"
    assert res_full["eligible_peer_models"] == ["gemini-4-hydrogen-high"]


def test_pure_domain_valid_write_once_and_receipt_manifest_compat(tmp_path: Path) -> None:
    """Pure domain packet passes G0.5, grants hydrogen-low, writes once, passes receipt."""
    base_sha = _init_git_repo(tmp_path)
    pkt = tmp_path / "packet.md"
    pkt.write_text(
        _render_packet(
            base_sha,
            root_task_id="P1-PURE",
            task_id="P1-PURE",
            owned_lines=[
                "  - `backend/app/domain.py` (modify)",
                "  - `backend/app/pure_helper.py` (new)",
            ],
            denied_lines=[
                "  - `backend/app/other.py`",
                "  - `frontend/**`",
                "  - `../lightweight-charts/**`",
            ],
        ),
        encoding="utf-8",
    )

    out_path = tmp_path / "runs" / "P1-PURE" / "readiness.json"
    rc = brief_gate.main(
        ["--packet", str(pkt), "--repo-root", str(tmp_path), "--out", str(out_path)]
    )
    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["status"] == "VALID"
    assert payload["is_bugfix"] is False
    assert all(flag is False for flag in payload["risk_flags"].values())
    assert "gemini-4-hydrogen-high" in payload["eligible_peer_models"]
    assert "gemini-4-hydrogen-low" in payload["eligible_peer_models"]

    # Write-once refusal must fail with exit 2 and not overwrite existing file
    original_bytes = out_path.read_bytes()
    rc_dup = brief_gate.main(
        ["--packet", str(pkt), "--repo-root", str(tmp_path), "--out", str(out_path)]
    )
    assert rc_dup == 2
    assert out_path.read_bytes() == original_bytes

    # Direct receipt._validate_task_manifest compatibility
    ns = argparse.Namespace(bugfix=False, base_sha=base_sha, task_id="P1-PURE")
    assert receipt._validate_task_manifest(out_path, ns) is None


def test_self_validate_r2_brief_gate_runner_schema_and_cli(tmp_path: Path) -> None:
    """Self-validate a packet with the exact R2-BRIEF-GATE-RUNNER schema via CLI."""
    base_sha = _init_git_repo(
        tmp_path,
        extra_files={
            ".agents/skills/slp-orchestrator/references/task-packet-template.md": "# T\n",
        },
    )
    pkt = tmp_path / "r2_packet.md"
    pkt.write_text(
        _render_packet(
            base_sha,
            root_task_id="R2-BRIEF-GATE-RUNNER",
            task_id="R2-BRIEF-GATE-RUNNER",
            phase="0",
            risk_tags="persistence, concurrency",
            budget_usd="6.00",
            owned_lines=[
                "  - `harness/scripts/brief_gate.py` (new)",
                "  - `harness/scripts/task_runner.py` (new)",
                "  - `.agents/skills/slp-orchestrator/references/task-packet-template.md` (modify)",
                "  - `harness/tests/test_brief_gate.py` (new)",
                "  - `harness/tests/test_task_runner.py` (new)",
            ],
            denied_lines=[
                "  - `backend/**`",
                "  - `frontend/**`",
                "  - `../lightweight-charts/**`",
                "  - `.references/**`",
                "  - `.env*`",
                "  - `harness/scripts/schema_utils.py`",
                "  - `harness/scripts/receipt.py`",
            ],
            extra_sections=(
                "\n1. **Atomicity / Rollback / Concurrency Contract**:\n"
                "   - Persist state atomically with fsync + os.replace.\n"
            ),
            checks_section=(
                "## 4. Required Local Checks Before `CANDIDATE_READY`\n"
                "- `python harness/scripts/arch_check.py --root .`\n"
            ),
        ),
        encoding="utf-8",
    )
    proc = run_managed_cmd(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "scripts" / "brief_gate.py"),
            "--packet",
            str(pkt),
            "--repo-root",
            str(tmp_path),
            "--json",
        ],
        timeout_sec=30.0,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["status"] == "VALID"
    assert data["eligible_peer_models"] == ["gemini-4-hydrogen-high"]


def test_p2_004_glob_dot_env_and_case_insensitive_overlap_block(tmp_path: Path) -> None:
    """P2-004: Reject globs, //, internal '.', .env secrets, and case-variant overlaps."""
    base_sha = _init_git_repo(tmp_path)

    # 1. Glob in owned_paths
    p_glob = tmp_path / "p_glob.md"
    p_glob.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/app/*.py` (modify)"]),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_glob, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # 2. Internal '.' or '//'
    p_dot = tmp_path / "p_dot.md"
    p_dot.write_text(
        _render_packet(base_sha, owned_lines=["  - `backend/./app//domain.py` (modify)"]),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_dot, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # 3. Forbidden .env in owned_paths (while .env.example is allowed if valid)
    p_env = tmp_path / "p_env.md"
    p_env.write_text(
        _render_packet(base_sha, owned_lines=["  - `.env.local` (new)"]),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_env, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # 4. Case-insensitive overlap (Backend/App/Domain.py vs backend/**)
    p_case = tmp_path / "p_case.md"
    p_case.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `Backend/App/Domain.py` (modify)"],
            denied_lines=["  - `backend/**`", "  - `../lightweight-charts/**`"],
        ),
        encoding="utf-8",
    )
    res_case = brief_gate.validate_brief(p_case, repo_root=tmp_path)
    assert res_case["status"] == "BLOCKED_BRIEF"
    assert any("overlap" in err.lower() for err in res_case["errors"])


def test_p2_007_strips_html_comments_and_rejects_template_placeholders(tmp_path: Path) -> None:
    """P2-007: HTML-commented contracts cannot satisfy Layer 2; template placeholders fail."""
    base_sha = _init_git_repo(
        tmp_path,
        extra_files={"backend/app/ws_broadcaster.py": "def run():\n    pass\n"},
    )
    commented_contract = (
        "\n<!--\n### Atomicity / Rollback / Concurrency Contract\n"
        "Locking inside comment must be ignored.\n-->\n"
    )
    p_comment = tmp_path / "p_comment.md"
    p_comment.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `backend/app/ws_broadcaster.py` (modify)"],
            extra_sections=commented_contract,
        ),
        encoding="utf-8",
    )
    res_comment = brief_gate.validate_brief(p_comment, repo_root=tmp_path)
    assert res_comment["status"] == "NEEDS_REPLAN"

    # Sensitivity placeholder must fail even when other headers are valid
    p_placeholder = tmp_path / "p_placeholder.md"
    p_placeholder.write_text(
        _render_packet(
            base_sha,
            sensitivity_line=(
                '- "When <scenario>, if <specific defect>, '
                'test fails at <assertion>, because contract requires <expected>."'
            ),
        ),
        encoding="utf-8",
    )
    res_ph = brief_gate.validate_brief(p_placeholder, repo_root=tmp_path)
    assert res_ph["status"] == "NEEDS_REPLAN"

    # Unedited task-packet-template.md must never validate as VALID
    tpl = REPO_ROOT / ".agents" / "skills" / "slp-orchestrator" / "references"
    tpl_file = tpl / "task-packet-template.md"
    res_tpl = brief_gate.validate_brief(tpl_file, repo_root=tmp_path)
    assert res_tpl["status"] != "VALID"


def test_p1_003_zero_depth_globstar_and_nested_headings_and_policy_union(tmp_path: Path) -> None:
    """Zero-depth **/ globstars overlap, nested headings stay intact, policy keeps defaults."""
    base_sha = _init_git_repo(
        tmp_path,
        extra_files={
            "secret.py": "S = 1\n",
            "backend/cfg.py": "C = 1\n",
            "backend/app/security.py": "SEC = 1\n",
            "backend_extra/x.py": "X = 1\n",
        },
    )

    # Zero-depth **/ globstars: **/secret.py vs secret.py and backend/**/cfg.py vs backend/cfg.py
    p_glob1 = tmp_path / "p_glob1.md"
    p_glob1.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `secret.py` (modify)"],
            denied_lines=["  - `**/secret.py`", "  - `../lightweight-charts/**`"],
        ),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_glob1, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    p_glob2 = tmp_path / "p_glob2.md"
    p_glob2.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `backend/cfg.py` (modify)"],
            denied_lines=["  - `backend/**/cfg.py`", "  - `../lightweight-charts/**`"],
        ),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_glob2, repo_root=tmp_path)["status"] == "BLOCKED_BRIEF"

    # backend/** must NOT block backend_extra/x.py
    p_extra = tmp_path / "p_extra.md"
    p_extra.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `backend_extra/x.py` (modify)"],
            denied_lines=["  - `backend/**`", "  - `../lightweight-charts/**`"],
        ),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_extra, repo_root=tmp_path)["status"] == "VALID"

    # Nested heading + numbered bold item must not pop enclosing ### Security heading
    nested_sec = (
        "\n### Security / Read-Only Invariants\n"
        "1. **Boundary**: Loopback bind only.\n"
        "2. **Read-Only**: Enforce SEC-001.\n"
    )
    p_nested = tmp_path / "p_nested.md"
    p_nested.write_text(
        _render_packet(
            base_sha,
            owned_lines=["  - `backend/app/security.py` (modify)"],
            extra_sections=nested_sec,
        ),
        encoding="utf-8",
    )
    assert brief_gate.validate_brief(p_nested, repo_root=tmp_path)["status"] == "VALID"

    # Policy with excluded_model_prefixes=[] still retains DEFAULT_EXCLUDED_MODEL_PREFIXES
    policy = tmp_path / "harness" / "model-policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"excluded_model_prefixes": ["custom-banned"]}), encoding="utf-8")
    prefixes, sha, err = brief_gate._load_policy(tmp_path, policy)
    assert err is None and sha is not None
    assert "gemini-3.8-flash" in prefixes
    assert "custom-banned" in prefixes

    # Template with Layer 1 headers filled but Layer 2 boilerplate left unedited -> NEEDS_REPLAN
    tpl_file = (
        REPO_ROOT / ".agents" / "skills" / "slp-orchestrator" / "references"
        / "task-packet-template.md"
    )
    tpl_text = tpl_file.read_text(encoding="utf-8")
    filled = (
        tpl_text.replace("<TASK_ID>", "T-TPL-01")
        .replace("<ROOT_TASK_ID>", "T-TPL-01")
        .replace("<0..6>", "1")
        .replace("<F01..F08 / R01..R06 / None>", "None")
        .replace("<40-char lowercase git SHA>", base_sha)
        .replace("<Yes | No>", "Yes")
        .replace(
            "<None | persistence, concurrency, websocket, security, auth, session_calendar>",
            "concurrency",
        )
        .replace("<positive float, e.g. 6.00>", "6.00")
        .replace("  - `<path/to/new_file.py>` (new)\n", "")
        .replace("  - `backend/**`\n", "")
        .replace("<path/to/existing_file.py>", "backend/app/domain.py")
    )
    p_tpl_l1 = tmp_path / "p_tpl_l1.md"
    p_tpl_l1.write_text(filled, encoding="utf-8")
    rc_tpl = brief_gate.main(["--packet", str(p_tpl_l1), "--repo-root", str(tmp_path)])
    assert rc_tpl == 1
