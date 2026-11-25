"""Tests for task_runner.py durable ledger, circuit breakers, and budget accounting."""

from __future__ import annotations

import concurrent.futures
import json
import math
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.scripts import brief_gate, task_runner
from harness.scripts.schema_utils import run_managed_cmd


def _git(cwd: Path, *args: str) -> str:
    git_bin = shutil.which("git") or "git"
    proc = run_managed_cmd([git_bin, *args], cwd=cwd, timeout_sec=30.0)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _setup_repo_with_valid_packet(
    root: Path,
    *,
    root_task_id: str = "T-RUN-01",
    packet_version: int = 1,
    budget_usd: float = 6.0,
) -> tuple[str, Path, Path]:
    if not (root / ".git").exists():
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test User")
        prompt = (
            root
            / ".agents"
            / "skills"
            / "slp-orchestrator"
            / "references"
            / "peer-system-prompt.md"
        )
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("# Peer\nRead-only.\n", encoding="utf-8")

        owned = root / "backend" / "app" / "feature.py"
        owned.parent.mkdir(parents=True, exist_ok=True)
        owned.write_text("VALUE = 1\n", encoding="utf-8")

        denied = root / "backend" / "app" / "protected.py"
        denied.write_text("SECRET = 1\n", encoding="utf-8")

        _git(root, "add", ".")
        _git(root, "commit", "-m", "base")

    base_sha = _git(root, "rev-parse", "HEAD")
    pkt = root / f"packet_v{packet_version}.md"
    pkt.write_text(
        "\n".join(
            [
                f"# Task Packet: `{root_task_id}`",
                "",
                f"- **Root Task ID**: `{root_task_id}`",
                f"- **Task ID**: `{root_task_id}`",
                f"- **Packet Version**: `{packet_version}`",
                "- **Phase**: `1`",
                "- **Audit Refs**: `None`",
                f"- **Base SHA**: `{base_sha}`",
                "- **Is Bugfix**: `No`",
                "- **Risk Tags**: `None`",
                f"- **Budget USD**: `{budget_usd:.2f}`",
                "",
                "## 1. Outcome & Behavioral Contract",
                "Update feature.py value.",
                "",
                "## 2. Write Scope & Do-Not-Touch",
                "- **Owned Paths (may create/edit)**:",
                "  - `backend/app/feature.py` (modify)",
                "- **Denied Paths**:",
                "  - `backend/app/protected.py`",
                "  - `../lightweight-charts/**`",
                "",
                "## 3. Acceptance & Test Contract",
                '- "When feature runs, if wrong value, test fails at assert VALUE == 2."',
                "",
                "## 4. Required Local Checks",
                "- `pytest backend/tests -q`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    readiness = root / f"readiness_v{packet_version}.json"
    rc = brief_gate.main(
        ["--packet", str(pkt), "--repo-root", str(root), "--out", str(readiness)]
    )
    assert rc == 0
    return base_sha, pkt, readiness


def test_atomic_ledger_write_preserves_prior_file_and_root_id(tmp_path: Path) -> None:
    """Failed serialization keeps prior ledger intact; mismatched root_task_id is rejected."""
    ledger_path = tmp_path / "runs" / "T-ATOMIC" / "ledger.json"
    ledger = task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-ATOMIC",
        base_sha="1" * 40,
        budget_usd=6.0,
    )
    original_bytes = ledger_path.read_bytes()

    broken = dict(ledger)
    broken["bad_obj"] = {1, 2, 3}
    with pytest.raises(TypeError):
        task_runner.save_ledger(ledger_path, broken)
    assert ledger_path.read_bytes() == original_bytes

    with pytest.raises(ValueError, match="Root task mismatch"):
        task_runner.init_or_load_ledger(ledger_path, root_task_id="T-OTHER")


def test_check_dispatch_blocks_stale_packet_and_excluded_models(tmp_path: Path) -> None:
    """Edited packet.md after readiness.json and excluded models must block dispatch (rc=2)."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )

    # Excluded / opaque model -> BLOCKED (rc=2)
    rc_model = task_runner.main(
        [
            "check-dispatch",
            "--ledger",
            str(ledger_path),
            "--readiness",
            str(readiness),
            "--packet",
            str(pkt),
            "--peer-model",
            "flash",
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc_model == 2
    assert task_runner.load_ledger(ledger_path)["state"] == "BLOCKED"

    # Reset to fresh ledger and verify stale packet.md blocks with BLOCKED_BRIEF
    ledger_path2 = tmp_path / "ledger2.json"
    task_runner.init_or_load_ledger(
        ledger_path2,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )
    pkt.write_text(pkt.read_text(encoding="utf-8") + "\n# Tampered\n", encoding="utf-8")
    rc_stale = task_runner.main(
        [
            "check-dispatch",
            "--ledger",
            str(ledger_path2),
            "--readiness",
            str(readiness),
            "--packet",
            str(pkt),
            "--peer-model",
            "gemini-4-hydrogen-high",
            "--repo-root",
            str(tmp_path),
        ]
    )
    state = task_runner.load_ledger(ledger_path2)["state"]
    assert state == "BLOCKED_BRIEF"
    assert rc_stale == 2


@pytest.mark.parametrize("signal", ["BLOCKED", "REOPEN_REQUEST", "DEPENDENCY_REQUEST"])
def test_peer_escalations_route_to_needs_replan_without_candidate_increment(
    tmp_path: Path,
    signal: str,
) -> None:
    """Peer escalations transition to NEEDS_REPLAN with zero failure counter increments."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path)
    ledger_path = tmp_path / f"ledger_{signal}.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )
    ok, _, rc, _ = task_runner.check_dispatch(
        ledger_path, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert ok and rc == 0

    outcome, ledger, rec_rc = task_runner.record_peer_outcome(
        ledger_path,
        signal=signal,
        signal_payload={"evidence": "feature.py:1", "reason": "needs replan"},
        repo_root=tmp_path,
    )
    assert rec_rc == 0
    assert outcome == signal
    assert ledger["state"] == "NEEDS_REPLAN"
    assert ledger["candidates_in_packet"] == 0
    assert ledger["total_candidates"] == 0
    assert ledger["consecutive_failed_candidates"] == 0

    # Automatic Peer retry is forbidden from NEEDS_REPLAN until Coordinator replans
    ok_retry, _, rc_retry, _ = task_runner.check_dispatch(
        ledger_path, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert not ok_retry and rc_retry == 2


def test_zero_token_startup_error_allows_one_retry_then_blocks_infra(tmp_path: Path) -> None:
    """0-token startup error classifies as INFRA_STARTUP_ERROR, allows 1 retry, then blocks."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )
    ok, _, _, _ = task_runner.check_dispatch(
        ledger_path, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert ok

    first_outcome, l1, rc1 = task_runner.record_peer_outcome(
        ledger_path,
        status="ERROR",
        exit_code=1,
        total_tokens=0,
        error_message="failed to start conversation: backend returned an empty conversation id",
        repo_root=tmp_path,
    )
    assert first_outcome == "INFRA_STARTUP_ERROR"
    assert rc1 == 0
    assert l1["state"] == "ACTIVE_PEER_G1"
    assert l1["transient_retries_in_stage"] == 1
    assert l1["total_candidates"] == 0
    assert l1["consecutive_failed_candidates"] == 0

    _, l2, rc2 = task_runner.record_peer_outcome(
        ledger_path,
        status="ERROR",
        exit_code=1,
        total_tokens=0,
        error_message="failed to start conversation: backend returned an empty conversation id",
        repo_root=tmp_path,
    )
    second_state = l2["state"]
    assert second_state == "BLOCKED_INFRA"
    assert rc2 == 2


def test_scope_violation_circuit_breakers_and_replan_durability(tmp_path: Path) -> None:
    """Enforce 2-fail reassess, 1-auto-replan, and 6-total-candidate breakers across restarts."""
    base_sha, pkt1, readiness1 = _setup_repo_with_valid_packet(tmp_path, packet_version=1)
    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=10.0,
    )

    # Candidate 1: touches denied file -> SCOPE_VIOLATION (1st failure, still ACTIVE_PEER_G1)
    ok_d1, _, _, _ = task_runner.check_dispatch(
        ledger_path, readiness1, pkt1, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert ok_d1
    (tmp_path / "backend" / "app" / "protected.py").write_text("SECRET = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "backend/app/protected.py")
    _git(tmp_path, "commit", "-m", "bad scope")
    c1 = _git(tmp_path, "rev-parse", "HEAD")

    out1, l1, rc1 = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=c1,
        readiness_path=readiness1,
        packet_path=pkt1,
        base_sha=base_sha,
        repo_root=tmp_path,
    )
    assert out1 == "SCOPE_VIOLATION"
    assert rc1 == 1
    assert l1["state"] == "ACTIVE_PEER_G1"
    assert l1["candidate_sha"] is None
    assert l1["total_candidates"] == 1
    assert l1["consecutive_failed_candidates"] == 1

    # Candidate 2: valid scope, fails at G3 -> consecutive_failed_candidates == 2 -> NEEDS_REPLAN
    _git(tmp_path, "reset", "--hard", base_sha)
    (tmp_path / "backend" / "app" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "backend/app/feature.py")
    _git(tmp_path, "commit", "-m", "valid scope cand 2")
    c2 = _git(tmp_path, "rev-parse", "HEAD")

    out2, l2, rc2 = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=c2,
        readiness_path=readiness1,
        packet_path=pkt1,
        base_sha=base_sha,
        repo_root=tmp_path,
    )
    assert out2 == "CANDIDATE_READY"
    assert rc2 == 0
    assert l2["candidate_sha"] == c2
    assert l2["state"] == "VERIFY_FROZEN_G3"

    lg3, rc_g3 = task_runner.record_gate_outcome(
        ledger_path, gate="G3", outcome="FAILED", candidate_sha=c2
    )
    assert rc_g3 == 1
    assert lg3["state"] == "NEEDS_REPLAN"
    assert lg3["consecutive_failed_candidates"] == 2
    assert lg3["total_candidates"] == 2

    # Apply auto-replan #1 (packet_version=2): preserves total_candidates=2, resets per-packet
    _git(tmp_path, "reset", "--hard", base_sha)
    b2, pkt2, readiness2 = _setup_repo_with_valid_packet(tmp_path, packet_version=2)
    l_rep1, rc_rep1, _ = task_runner.record_replan(ledger_path, readiness2, packet_path=pkt2)
    assert rc_rep1 == 0
    assert l_rep1["auto_replans_used"] == 1
    assert l_rep1["total_candidates"] == 2
    assert l_rep1["candidates_in_packet"] == 0
    assert l_rep1["consecutive_failed_candidates"] == 0
    assert l_rep1["state"] == "ACTIVE_PEER_G1"

    # Attempt auto-replan #2 when NOT in NEEDS_REPLAN / auto-replan exhausted -> rejected (rc=2)
    b3, pkt3, readiness3 = _setup_repo_with_valid_packet(tmp_path, packet_version=3)
    l_rep2, rc_rep2, _ = task_runner.record_replan(
        ledger_path, readiness3, packet_path=pkt3, human_approved=False
    )
    assert rc_rep2 == 2
    assert l_rep2["state"] == "ACTIVE_PEER_G1"

    # Human-approved replan resumes, then hitting 6 total_candidates triggers PAUSED_BUDGET
    l_hum, rc_hum, _ = task_runner.record_replan(
        ledger_path, readiness3, packet_path=pkt3, human_approved=True
    )
    assert rc_hum == 0 and l_hum["state"] == "ACTIVE_PEER_G1"
    active_base = b3
    active_pkt = pkt3
    active_readiness = readiness3
    for idx in range(3, 7):
        (tmp_path / "backend" / "app" / "feature.py").write_text(
            f"VALUE = {idx}\n", encoding="utf-8"
        )
        _git(tmp_path, "add", "backend/app/feature.py")
        _git(tmp_path, "commit", "-m", f"cand {idx}")
        c_sha = _git(tmp_path, "rev-parse", "HEAD")
        _, _, _ = task_runner.record_peer_outcome(
            ledger_path,
            signal="CANDIDATE_READY",
            candidate_sha=c_sha,
            readiness_path=active_readiness,
            packet_path=active_pkt,
            base_sha=active_base,
            repo_root=tmp_path,
        )
        _, rc_p3 = task_runner.record_gate_outcome(
            ledger_path, gate="G3", outcome="PASSED", candidate_sha=c_sha
        )
        assert rc_p3 == 0
        l_fail, rc_fail = task_runner.record_gate_outcome(
            ledger_path, gate="G4", outcome="CHANGES_REQUESTED", candidate_sha=c_sha
        )
        if idx == 4:
            assert l_fail["state"] == "NEEDS_REPLAN"
            b4, pkt4, readiness4 = _setup_repo_with_valid_packet(tmp_path, packet_version=4)
            # 2nd auto-replan from NEEDS_REPLAN when auto_replans_used==1 -> PAUSED_BUDGET
            l_pause, rc_pause, _ = task_runner.record_replan(
                ledger_path, readiness4, packet_path=pkt4, human_approved=False
            )
            assert rc_pause == 2 and l_pause["state"] == "PAUSED_BUDGET"
            l_hum2, rc_hum2, _ = task_runner.record_replan(
                ledger_path, readiness4, packet_path=pkt4, human_approved=True
            )
            assert rc_hum2 == 0 and l_hum2["state"] == "ACTIVE_PEER_G1"
            assert l_hum2["total_candidates"] == 4
            active_base = b4
            active_pkt = pkt4
            active_readiness = readiness4
        if idx == 6:
            assert rc_fail == 2
            assert l_fail["total_candidates"] == 6
            assert l_fail["state"] == "PAUSED_BUDGET"
            assert b2 == base_sha


def test_cumulative_usage_dedup_and_80_100_budget_reservation(tmp_path: Path) -> None:
    """Cumulative conversation_id counters bill only positive deltas and enforce 80%/100%."""
    base_sha, _, _ = _setup_repo_with_valid_packet(tmp_path, budget_usd=6.0)
    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )

    # Turn 1 (gemini-4-neon: $3/1M input, $15/1M output+thinking, $0.30/1M cache)
    # 1,000,000 input -> $3.00
    l1, d1, rc1 = task_runner.record_usage(
        ledger_path,
        model="gemini-4-neon-high",
        input_tokens=1_000_000,
        output_tokens=0,
        thinking_tokens=0,
        cache_read_tokens=0,
        conversation_id="conv-abc",
        counter_mode="cumulative",
    )
    assert rc1 == 0
    assert d1 == pytest.approx(3.0)
    assert l1["spent_api_equiv_usd"] == pytest.approx(3.0)
    assert l1["budget_warning"] is False

    # Turn 2 cumulative on same conv-abc: 1.5M input, 0.1M output -> delta 0.5M in + 0.1M out
    # Expected delta = $1.50 + $1.50 = $3.00? Wait: keep under $6.00 to test 80% ($4.80) first:
    # Use 1.2M input (+0.2M = $0.60) + 0.08M output ($1.20) -> delta $1.80 -> total $4.80 (80%!)
    l2, d2, rc2 = task_runner.record_usage(
        ledger_path,
        model="gemini-4-neon-high",
        input_tokens=1_200_000,
        output_tokens=80_000,
        thinking_tokens=0,
        cache_read_tokens=0,
        conversation_id="conv-abc",
        counter_mode="cumulative",
    )
    assert rc2 == 0
    assert d2 == pytest.approx(1.80)
    assert l2["spent_api_equiv_usd"] == pytest.approx(4.80)
    assert l2["budget_warning"] is True

    # Replay same cumulative totals -> 0.00 delta billed
    l3, d3, rc3 = task_runner.record_usage(
        ledger_path,
        model="gemini-4-neon-high",
        input_tokens=1_200_000,
        output_tokens=80_000,
        conversation_id="conv-abc",
        counter_mode="cumulative",
    )
    assert rc3 == 0 and d3 == pytest.approx(0.0)
    assert l3["spent_api_equiv_usd"] == pytest.approx(4.80)

    # Atomic reservation within remaining ($4.80 + $1.00 = $5.80 <= $6.00) succeeds
    ok_res, l_res, rc_res = task_runner.reserve_budget(ledger_path, "pass1", 1.00)
    assert ok_res and rc_res == 0
    assert l_res["active_reservations"]["pass1"] == pytest.approx(1.00)

    # Reservation exceeding 100% ($5.80 + $0.50 = $6.30 > $6.00) rejects & sets PAUSED_BUDGET
    ok_over, l_over, rc_over = task_runner.reserve_budget(ledger_path, "pass2", 0.50)
    assert not ok_over and rc_over == 2
    assert l_over["state"] == "PAUSED_BUDGET"
    assert "pass2" not in l_over["active_reservations"]

    # Release pass1 lowers reserved back to 0
    l_rel = task_runner.release_reservation(ledger_path, "pass1")
    assert l_rel["active_reservations"] == {}


def test_wall_clock_deadline_and_managed_subprocess_timeout(tmp_path: Path) -> None:
    """Expired root_task_wall_clock pauses dispatch; managed stage kills timed-out subprocess."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    ledger = task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )
    ledger["created_at_utc"] = (datetime.now(UTC) - timedelta(seconds=4000)).isoformat()
    task_runner.save_ledger(ledger_path, ledger)

    ok, l_exp, rc_exp, _ = task_runner.check_dispatch(
        ledger_path, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert not ok and rc_exp == 2
    assert l_exp["state"] == "PAUSED_BUDGET"

    with pytest.raises(subprocess.TimeoutExpired):
        task_runner.run_managed_stage_cmd(
            "brief_generation",
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_override=0.2,
        )


def test_p2_001_002_003_state_preconditions_and_path_normalization(tmp_path: Path) -> None:
    """P2-001/002/003: Enforce dotfile normalization, state preconditions, and G5 SHA."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path,
        root_task_id="T-RUN-01",
        base_sha=base_sha,
        budget_usd=6.0,
    )

    # P2-001: leading dot on .env / .agents must NOT be stripped by normalization
    scope_errs = task_runner._check_write_scope(
        [".env.local"],
        ["backend/app/feature.py"],
        [".env*"],
    )
    assert scope_errs and any("Forbidden secret env" in e or "Denied" in e for e in scope_errs)

    # P2-003: record_peer_outcome must fail while still in INTAKE_G0 (before check_dispatch)
    with pytest.raises(ValueError, match="ACTIVE_PEER_G1"):
        task_runner.record_peer_outcome(
            ledger_path,
            signal="CANDIDATE_READY",
            candidate_sha="2" * 40,
            readiness_path=readiness,
            packet_path=pkt,
            repo_root=tmp_path,
        )

    # P2-002: G3/G4/G5 from INTAKE_G0 must return rc=2 and leave state untouched
    l_bad_gate, rc_bad_gate = task_runner.record_gate_outcome(
        ledger_path, gate="G5", outcome="VERIFIED", candidate_sha="2" * 40
    )
    assert rc_bad_gate == 2
    assert l_bad_gate["state"] == "INTAKE_G0"

    # Dispatch -> CANDIDATE_READY -> G3 PASSED -> G4 PASSED -> G5 VERIFIED
    ok, _, rc_d, _ = task_runner.check_dispatch(
        ledger_path, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert ok and rc_d == 0

    (tmp_path / "backend" / "app" / "feature.py").write_text("VALUE = 99\n", encoding="utf-8")
    _git(tmp_path, "add", "backend/app/feature.py")
    _git(tmp_path, "commit", "-m", "good cand")
    cand_sha = _git(tmp_path, "rev-parse", "HEAD")

    # P2-003: diverged base_sha must be rejected
    with pytest.raises(ValueError, match="diverges from readiness"):
        task_runner.record_peer_outcome(
            ledger_path,
            signal="CANDIDATE_READY",
            candidate_sha=cand_sha,
            readiness_path=readiness,
            packet_path=pkt,
            base_sha="9" * 40,
            repo_root=tmp_path,
        )

    out_c, l_c, rc_c = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=cand_sha,
        readiness_path=readiness,
        packet_path=pkt,
        repo_root=tmp_path,
    )
    assert out_c == "CANDIDATE_READY" and rc_c == 0
    assert l_c["state"] == "VERIFY_FROZEN_G3"

    # G4 cannot run before G3 passes
    l_skip_g3, rc_skip = task_runner.record_gate_outcome(
        ledger_path, gate="G4", outcome="PASSED", candidate_sha=cand_sha
    )
    assert rc_skip == 2 and l_skip_g3["state"] == "VERIFY_FROZEN_G3"

    l_g3, rc_g3 = task_runner.record_gate_outcome(
        ledger_path, gate="G3", outcome="PASSED", candidate_sha=cand_sha
    )
    assert rc_g3 == 0 and l_g3["state"] == "REVIEW_FROZEN_G4"

    l_g4, rc_g4 = task_runner.record_gate_outcome(
        ledger_path, gate="G4", outcome="PASSED", candidate_sha=cand_sha
    )
    assert rc_g4 == 0 and l_g4["state"] == "READY_FOR_ACCEPTANCE"

    # G5 without candidate_sha or mismatched SHA must NOT transition to VERIFIED
    l_no_sha, rc_no_sha = task_runner.record_gate_outcome(
        ledger_path, gate="G5", outcome="VERIFIED", candidate_sha=None
    )
    assert rc_no_sha == 2 and l_no_sha["state"] == "READY_FOR_ACCEPTANCE"

    l_g5, rc_g5 = task_runner.record_gate_outcome(
        ledger_path, gate="G5", outcome="VERIFIED", candidate_sha=cand_sha
    )
    assert rc_g5 == 0 and l_g5["state"] == "VERIFIED"

    # Neither human_approved=False nor human_approved=True can overwrite VERIFIED
    _, pkt2, r2 = _setup_repo_with_valid_packet(tmp_path, packet_version=2)
    l_rep_v, rc_rep_v, _ = task_runner.record_replan(
        ledger_path, r2, packet_path=pkt2, human_approved=True
    )
    assert rc_rep_v == 2 and l_rep_v["state"] == "VERIFIED"


def test_p2_005_006_concurrent_reservations_and_nan_bool_guards(tmp_path: Path) -> None:
    """P2-005/006: Concurrent reserve_budget never exceeds 100%; NaN/Inf/bool rejected."""
    base_sha, _, _ = _setup_repo_with_valid_packet(tmp_path, budget_usd=5.0)
    ledger_path = tmp_path / "ledger.json"

    bad_bool_true: object = True
    bad_bool_false: object = False
    with pytest.raises(ValueError):
        task_runner.init_or_load_ledger(
            ledger_path,
            root_task_id="T-BAD",
            base_sha=base_sha,
            budget_usd=bad_bool_true,  # non-numeric bool
        )
    with pytest.raises(ValueError):
        task_runner.init_or_load_ledger(
            ledger_path, root_task_id="T-BAD", base_sha=base_sha, budget_usd=math.nan
        )

    task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-CONC", base_sha=base_sha, budget_usd=5.0
    )
    with pytest.raises(ValueError):
        task_runner.reserve_budget(ledger_path, "bad-inf", math.inf)
    with pytest.raises(ValueError):
        task_runner.reserve_budget(ledger_path, "bad-bool", bad_bool_false)

    # 8 concurrent workers each trying to reserve $1.00 against $5.00 budget -> max 5 succeed
    def _worker(idx: int) -> bool:
        ok, _, _ = task_runner.reserve_budget(ledger_path, f"worker-{idx}", 1.0)
        return ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_worker, range(8)))

    final_ledger = task_runner.load_ledger(ledger_path)
    total_reserved = sum(final_ledger["active_reservations"].values())
    assert sum(1 for r in results if r) == 5
    assert total_reserved == pytest.approx(5.0)
    assert total_reserved <= 5.0
    assert final_ledger["state"] == "PAUSED_BUDGET"

    # P2-002: auto-replan (human_approved=False) must NOT overwrite PAUSED_BUDGET
    _, pkt2, r2 = _setup_repo_with_valid_packet(tmp_path, packet_version=2)
    l_refuse, rc_refuse, _ = task_runner.record_replan(
        ledger_path, r2, packet_path=pkt2, human_approved=False
    )
    assert rc_refuse == 2
    assert l_refuse["state"] == "PAUSED_BUDGET"


def test_p1_001_003_004_005_006_007_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover 0-token BLOCKED stderr, prose signal, crash, exact scope, policy, idempotency."""
    base_sha, pkt, readiness = _setup_repo_with_valid_packet(tmp_path, budget_usd=3.0)

    # P1-006: root_task_id path traversal rejected
    with pytest.raises(ValueError, match="Invalid root_task_id"):
        task_runner.default_ledger_path("../escape", repo_root=tmp_path)

    # P1-003: exact owned-file match rejects parent/prefix paths
    scope_errs = task_runner._check_write_scope(
        ["backend/app"],
        ["backend/app/feature.py"],
        ["**/secret.py"],
    )
    assert scope_errs and any("outside Owned Paths" in e for e in scope_errs)
    glob_errs = task_runner._check_write_scope(
        ["secret.py"],
        ["secret.py"],
        ["**/secret.py"],
    )
    assert glob_errs and any("Denied Paths" in e for e in glob_errs)

    ledger_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=3.0
    )

    # P1-005: idempotent reservation_id does not double-count on retry
    ok1, l_d1, rc_d1, _ = task_runner.check_dispatch(
        ledger_path,
        readiness,
        pkt,
        "gemini-4-hydrogen-high",
        repo_root=tmp_path,
        reserve_usd=2.0,
        reservation_id="peer-r1",
    )
    assert ok1 and rc_d1 == 0
    assert l_d1["active_reservations"]["peer-r1"] == pytest.approx(2.0)
    ok2, l_d2, rc_d2, _ = task_runner.check_dispatch(
        ledger_path,
        readiness,
        pkt,
        "gemini-4-hydrogen-high",
        repo_root=tmp_path,
        reserve_usd=2.0,
        reservation_id="peer-r1",
    )
    assert ok2 and rc_d2 == 0
    assert sum(l_d2["active_reservations"].values()) == pytest.approx(2.0)

    # P1-001: 0-token startup error with "BLOCKED" in stderr still classifies as INFRA_STARTUP_ERROR
    out_inf, l_inf, rc_inf = task_runner.record_peer_outcome(
        ledger_path,
        raw_output="ERROR: BLOCKED by backend empty conversation id",
        exit_code=1,
        total_tokens=0,
        repo_root=tmp_path,
    )
    assert out_inf == "INFRA_STARTUP_ERROR" and rc_inf == 0
    assert l_inf["state"] == "ACTIVE_PEER_G1"

    # P1-001: non-zero-token Peer crash classifies as PEER_ROUND_FAILED and counts failure
    out_crash, l_crash, rc_crash = task_runner.record_peer_outcome(
        ledger_path,
        raw_output="Traceback: crashed mid-round",
        exit_code=1,
        total_tokens=250,
        repo_root=tmp_path,
    )
    assert out_crash == "PEER_ROUND_FAILED" and rc_crash == 1
    assert l_crash["total_candidates"] == 1
    assert l_crash["consecutive_failed_candidates"] == 1

    # P1-001: prose mentioning REOPEN_REQUEST must NOT override CANDIDATE_READY + candidate_sha
    (tmp_path / "backend" / "app" / "feature.py").write_text("VALUE = 7\n", encoding="utf-8")
    _git(tmp_path, "add", "backend/app/feature.py")
    _git(tmp_path, "commit", "-m", "cand prose")
    c_sha = _git(tmp_path, "rev-parse", "HEAD")

    out_prose, l_prose, rc_prose = task_runner.record_peer_outcome(
        ledger_path,
        raw_output="Note: do not raise REOPEN_REQUEST lightly.\nCANDIDATE_READY",
        candidate_sha=c_sha,
        readiness_path=readiness,
        packet_path=pkt,
        repo_root=tmp_path,
    )
    assert out_prose == "CANDIDATE_READY" and rc_prose == 0
    assert l_prose["state"] == "VERIFY_FROZEN_G3"

    # P1-004: stale SHA at G3 is rejected; G3 BLOCKED transitions to BLOCKED
    l_stale_g3, rc_stale_g3 = task_runner.record_gate_outcome(
        ledger_path, gate="G3", outcome="PASSED", candidate_sha="0" * 40
    )
    assert rc_stale_g3 == 2 and l_stale_g3["state"] == "VERIFY_FROZEN_G3"

    l_blk_g3, rc_blk_g3 = task_runner.record_gate_outcome(
        ledger_path, gate="G3", outcome="BLOCKED", candidate_sha=c_sha
    )
    assert rc_blk_g3 == 2 and l_blk_g3["state"] == "BLOCKED"

    # P1-005: post-VERIFIED record_usage keeps state == VERIFIED & reserve_budget refuses
    l_v = task_runner.load_ledger(ledger_path)
    l_v["state"] = "VERIFIED"
    task_runner.save_ledger(ledger_path, l_v)
    l_post_u, _, rc_post_u = task_runner.record_usage(
        ledger_path, model="gemini-4-neon-high", input_tokens=2_000_000
    )
    assert rc_post_u == 0 and l_post_u["state"] == "VERIFIED"
    ok_post_r, l_post_r, rc_post_r = task_runner.reserve_budget(ledger_path, "after-done", 0.1)
    assert not ok_post_r and rc_post_r == 2 and l_post_r["state"] == "VERIFIED"

    # P1-007: malformed model-policy.json fails closed in check_dispatch
    ledger_pol = tmp_path / "ledger_pol.json"
    task_runner.init_or_load_ledger(
        ledger_pol, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=3.0
    )
    pol_file = tmp_path / "harness" / "model-policy.json"
    pol_file.parent.mkdir(parents=True, exist_ok=True)
    pol_file.write_text("{invalid", encoding="utf-8")
    ok_pol, l_pol, rc_pol, _ = task_runner.check_dispatch(
        ledger_pol, readiness, pkt, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert not ok_pol and rc_pol == 2 and l_pol["state"] == "BLOCKED"

    # P1-006: run_review_gate_step command construction & outer timeout
    captured: list[tuple[list[str], float]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append((cmd, float(_kwargs.get("timeout_sec", 0.0))))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(task_runner, "run_managed_cmd", _fake_run)
    task_runner.run_brief_gate_step(
        pkt, tmp_path / "r_out.json", repo_root=tmp_path, ledger=l_v
    )
    task_runner.run_verifier_gate_step(
        phase=0, commit_sha=c_sha, out_json=tmp_path / "v.json", repo_root=tmp_path, ledger=l_v
    )
    task_runner.run_review_gate_step(
        commit_sha=c_sha,
        task_id="T-RUN-01",
        packet_path=pkt,
        out_dir=tmp_path / "rev_out",
        base_sha=base_sha,
        repo_root=tmp_path,
        ledger=l_v,
    )
    task_runner.run_receipt_gate_step(
        task_id="T-RUN-01",
        phase=0,
        base_sha=base_sha,
        candidate_sha=c_sha,
        verifier_json=tmp_path / "v.json",
        review_json=tmp_path / "rev_out" / "consolidated-review.json",
        readiness_json=readiness,
        out_dir=tmp_path / "rec_out",
        repo_root=tmp_path,
        ledger=l_v,
    )
    brief_cmd, _ = captured[0]
    ver_cmd, _ = captured[1]
    rev_cmd, rev_timeout = captured[2]
    rec_cmd, _ = captured[3]
    assert "--out" in brief_cmd and "--json" in brief_cmd
    assert "--strict-clean" in ver_cmd and "--commit" in ver_cmd
    assert "--repo-root" in rev_cmd and "--base" in rev_cmd
    assert "--timeout" in rev_cmd and "--idle-timeout" in rev_cmd
    assert rev_timeout >= 1260.0
    assert "--task-manifest" in rec_cmd and "--candidate-sha" in rec_cmd


def test_a4_redos_crash_renames_policy_e2e_and_missing_ledger_cli(tmp_path: Path) -> None:
    """P2-001/P2-002/F1-01/F1-02/F1-05: Linear regex, crash handling, git mv, --policy, CLI."""
    # P2-001: 80 '#' chars must evaluate linearly without ReDoS
    t0 = time.monotonic()
    assert task_runner._extract_peer_signal("#" * 80) is None
    assert (time.monotonic() - t0) < 0.1
    assert task_runner._extract_peer_signal("[receipt] BLOCKED — gate failed") is None

    # F1-05: missing --ledger CLI exits 2 cleanly
    rc_missing = task_runner.main(["status", "--ledger", str(tmp_path / "does_not_exist.json")])
    assert rc_missing == 2

    # F1-02: custom --policy propagates via readiness.json through check_dispatch & record_replan
    base_sha, pkt1, _ = _setup_repo_with_valid_packet(tmp_path, packet_version=1, budget_usd=5.0)
    custom_pol = tmp_path / "custom-policy.json"
    custom_pol.write_text(
        json.dumps({"excluded_model_prefixes": ["banned-custom"]}),
        encoding="utf-8",
    )
    r_pol = tmp_path / "readiness_pol.json"
    assert (
        brief_gate.main(
            [
                "--packet",
                str(pkt1),
                "--repo-root",
                str(tmp_path),
                "--policy",
                str(custom_pol),
                "--out",
                str(r_pol),
            ]
        )
        == 0
    )
    ledger_path = tmp_path / "ledger_e2e.json"
    task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=5.0
    )
    ok_d, _, rc_d, _ = task_runner.check_dispatch(
        ledger_path, r_pol, pkt1, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert ok_d and rc_d == 0

    # P2-002: exit_code=1 with total_tokens=200 & CANDIDATE_READY/[receipt] BLOCKED -> failed
    out_cr, l_cr, rc_cr = task_runner.record_peer_outcome(
        ledger_path,
        raw_output="[receipt] BLOCKED\nCANDIDATE_READY",
        exit_code=1,
        total_tokens=200,
        repo_root=tmp_path,
    )
    assert out_cr == "PEER_ROUND_FAILED" and rc_cr == 1
    assert l_cr["total_candidates"] == 1 and l_cr["consecutive_failed_candidates"] == 1

    # P2-002: CANDIDATE_READY with candidate_sha=None -> PEER_ROUND_FAILED & persists counters
    out_none, l_none, rc_none = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=None,
        repo_root=tmp_path,
    )
    assert out_none == "PEER_ROUND_FAILED" and rc_none == 1
    assert l_none["total_candidates"] == 2 and l_none["state"] == "NEEDS_REPLAN"

    # Replan with custom --policy and bumped budget on human_approved=True
    _, pkt2, _ = _setup_repo_with_valid_packet(tmp_path, packet_version=2, budget_usd=9.5)
    r_pol2 = tmp_path / "readiness_pol2.json"
    assert (
        brief_gate.main(
            [
                "--packet",
                str(pkt2),
                "--repo-root",
                str(tmp_path),
                "--policy",
                str(custom_pol),
                "--out",
                str(r_pol2),
            ]
        )
        == 0
    )
    l_rep, rc_rep, _ = task_runner.record_replan(
        ledger_path, r_pol2, human_approved=True, repo_root=tmp_path
    )
    assert rc_rep == 0 and l_rep["budget_limit_usd"] == pytest.approx(9.5)

    # F1-01: git mv from denied file into owned path must fail as SCOPE_VIOLATION (--no-renames)
    _git(tmp_path, "rm", "-f", "backend/app/feature.py")
    _git(tmp_path, "mv", "backend/app/protected.py", "backend/app/feature.py")
    _git(tmp_path, "commit", "-m", "sneaky rename from denied")
    rename_sha = _git(tmp_path, "rev-parse", "HEAD")

    out_mv, l_mv, rc_mv = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=rename_sha,
        readiness_path=r_pol2,
        packet_path=pkt2,
        repo_root=tmp_path,
    )
    assert out_mv == "SCOPE_VIOLATION" and rc_mv == 1
    assert l_mv["candidate_sha"] is None


def test_g4_circuit_breakers_ledger_backfill_replan_and_idempotent_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify G4 cap defaults/backfill, dispatch/replan counters, idempotent gates, and --ledger."""
    base_sha, pkt1, r1 = _setup_repo_with_valid_packet(tmp_path, packet_version=1, budget_usd=6.0)
    ledger_path = tmp_path / "harness" / "runs" / "T-RUN-01" / "ledger.json"
    ledger = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=6.0
    )
    assert ledger["g4_rounds_total"] == 0
    assert ledger["g4_rounds_in_packet"] == 0
    assert ledger["g4_invocations_by_sha"] == {}
    assert ledger["circuit_breakers"]["max_g4_rounds_per_root_task"] == 6
    assert ledger["circuit_breakers"]["max_g4_rounds_per_packet_version"] == 3
    assert ledger["circuit_breakers"]["max_g4_invocations_per_candidate_sha"] == 2

    # Legacy ledger missing new G4 fields must backfill cleanly on load_ledger.
    legacy = json.loads(ledger_path.read_text(encoding="utf-8"))
    for k in (
        "g4_rounds_total",
        "g4_rounds_in_packet",
        "g4_invocations_by_sha",
        "last_g4_candidate_sha",
        "last_g4_status",
    ):
        legacy.pop(k, None)
    for k in (
        "max_g4_rounds_per_root_task",
        "max_g4_rounds_per_packet_version",
        "max_g4_invocations_per_candidate_sha",
    ):
        legacy["circuit_breakers"].pop(k, None)
    ledger_path.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    backfilled = task_runner.load_ledger(ledger_path)
    assert backfilled["g4_rounds_total"] == 0
    assert backfilled["circuit_breakers"]["max_g4_rounds_per_root_task"] == 6

    # check_dispatch enforces g4_rounds_in_packet >= 3 -> NEEDS_REPLAN; replan resets packet cap.
    backfilled["g4_rounds_total"] = 3
    backfilled["g4_rounds_in_packet"] = 3
    task_runner.save_ledger(ledger_path, backfilled)
    ok_pkt, l_pkt, rc_pkt, _ = task_runner.check_dispatch(
        ledger_path, r1, pkt1, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert not ok_pkt and rc_pkt == 2 and l_pkt["state"] == "NEEDS_REPLAN"

    _, pkt2, r2 = _setup_repo_with_valid_packet(tmp_path, packet_version=2, budget_usd=6.0)
    l_rep, rc_rep, _ = task_runner.record_replan(
        ledger_path, r2, packet_path=pkt2, human_approved=False, repo_root=tmp_path
    )
    assert rc_rep == 0
    assert l_rep["g4_rounds_in_packet"] == 0
    assert l_rep["g4_rounds_total"] == 3

    # check_dispatch enforces g4_rounds_total >= 6 -> PAUSED_BUDGET.
    l_rep["g4_rounds_total"] = 6
    task_runner.save_ledger(ledger_path, l_rep)
    ok_root, l_root, rc_root, _ = task_runner.check_dispatch(
        ledger_path, r2, pkt2, "gemini-4-hydrogen-high", repo_root=tmp_path
    )
    assert not ok_root and rc_root == 2 and l_root["state"] == "PAUSED_BUDGET"

    # Idempotent record_gate_outcome for G4 PASSED and G5 VERIFIED on same SHA only.
    cand_sha = "a" * 40
    l_root["state"] = "READY_FOR_ACCEPTANCE"
    l_root["candidate_sha"] = cand_sha
    task_runner.save_ledger(ledger_path, l_root)
    l_g4_idem, rc_g4_idem = task_runner.record_gate_outcome(
        ledger_path, gate="G4", outcome="PASSED", candidate_sha=cand_sha
    )
    assert rc_g4_idem == 0 and l_g4_idem["state"] == "READY_FOR_ACCEPTANCE"
    _, rc_g4_bad = task_runner.record_gate_outcome(
        ledger_path, gate="G4", outcome="PASSED", candidate_sha="b" * 40
    )
    assert rc_g4_bad == 2

    l_g5, rc_g5 = task_runner.record_gate_outcome(
        ledger_path, gate="G5", outcome="VERIFIED", candidate_sha=cand_sha
    )
    assert rc_g5 == 0 and l_g5["state"] == "VERIFIED"
    l_g5_idem, rc_g5_idem = task_runner.record_gate_outcome(
        ledger_path, gate="G5", outcome="VERIFIED", candidate_sha=cand_sha
    )
    assert rc_g5_idem == 0 and l_g5_idem["state"] == "VERIFIED"

    # run_receipt_gate_step includes --ledger when available.
    seen_cmds: list[list[str]] = []
    monkeypatch.setattr(
        task_runner,
        "run_managed_cmd",
        lambda cmd, **_kw: (
            seen_cmds.append(cmd)
            or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        ),
    )
    task_runner.run_receipt_gate_step(
        task_id="T-RUN-01",
        phase=2,
        base_sha=base_sha,
        candidate_sha=cand_sha,
        verifier_json=tmp_path / "v.json",
        review_json=tmp_path / "r.json",
        readiness_json=r2,
        out_dir=tmp_path / "rec_out",
        repo_root=tmp_path,
        ledger=l_g5_idem,
    )
    assert "--ledger" in seen_cmds[0]
    assert str(ledger_path) in seen_cmds[0]


def test_human_approved_ceiling_overrides_and_root_wrapper_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ceiling overrides require --human-approved, avoid duplicate candidate count, forward root."""
    base_sha, pkt1, _ = _setup_repo_with_valid_packet(tmp_path, packet_version=1, budget_usd=6.0)
    ledger_path = tmp_path / "harness" / "runs" / "ROOT" / "ledger.json"
    led = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="ROOT", base_sha=base_sha, budget_usd=6.0
    )
    led["total_candidates"] = 6
    led["g4_rounds_total"] = 6
    led["state"] = "PAUSED_BUDGET"
    task_runner.save_ledger(ledger_path, led)

    pkt2_text = (
        pkt1.read_text(encoding="utf-8")
        .replace("- **Root Task ID**: `T-RUN-01`", "- **Root Task ID**: `ROOT`")
        .replace("- **Task ID**: `T-RUN-01`", "- **Task ID**: `CHILD-V2`")
        .replace("- **Packet Version**: `1`", "- **Packet Version**: `2`")
    )
    pkt2 = tmp_path / "packet_root_v2.md"
    pkt2.write_text(pkt2_text, encoding="utf-8")
    r2 = tmp_path / "readiness_root_v2.json"
    assert (
        brief_gate.main(
            [
                "--packet",
                str(pkt2),
                "--repo-root",
                str(tmp_path),
                "--out",
                str(r2),
            ]
        )
        == 0
    )

    # Without --human-approved, ceiling override is rejected with rc == 2.
    _, rc_no_human, msg_no_human = task_runner.record_replan(
        ledger_path,
        r2,
        packet_path=pkt2,
        human_approved=False,
        max_total_candidates=7,
        repo_root=tmp_path,
    )
    assert rc_no_human == 2
    assert "human-approved" in msg_no_human

    # With --human-approved via CLI, max_total_candidates=7 and auto-bumped max_g4_rounds=7 apply.
    rc_cli = task_runner.main(
        [
            "record-replan",
            "--ledger",
            str(ledger_path),
            "--readiness",
            str(r2),
            "--packet",
            str(pkt2),
            "--human-approved",
            "--max-total-candidates",
            "7",
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc_cli == 0
    updated = task_runner.load_ledger(ledger_path)
    assert updated["task_id"] == "CHILD-V2"
    assert updated["circuit_breakers"]["max_total_candidates_per_root_task"] == 7
    assert updated["circuit_breakers"]["max_g4_rounds_per_root_task"] == 7

    # Duplicate CANDIDATE_READY on the same SHA must not double-increment total_candidates.
    (tmp_path / "backend" / "app" / "feature.py").write_text("x = 42\n", encoding="utf-8")
    _git(tmp_path, "add", "backend/app/feature.py")
    _git(tmp_path, "commit", "-m", "cand same sha")
    same_sha = _git(tmp_path, "rev-parse", "HEAD")

    out1, led1, rc1 = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=same_sha,
        readiness_path=r2,
        packet_path=pkt2,
        repo_root=tmp_path,
    )
    assert out1 == "CANDIDATE_READY" and rc1 == 0
    assert led1["total_candidates"] == 7

    led1["state"] = "ACTIVE_PEER_G1"
    task_runner.save_ledger(ledger_path, led1)
    out2, led2, rc2 = task_runner.record_peer_outcome(
        ledger_path,
        signal="CANDIDATE_READY",
        candidate_sha=same_sha,
        readiness_path=r2,
        packet_path=pkt2,
        repo_root=tmp_path,
    )
    assert out2 == "CANDIDATE_READY" and rc2 == 0
    assert led2["total_candidates"] == 7

    # Both run_review_gate_step & run_receipt_gate_step forward ROOT/ledger.json for CHILD-V2.
    seen_cmds: list[list[str]] = []
    monkeypatch.setattr(
        task_runner,
        "run_managed_cmd",
        lambda cmd, **_kw: (
            seen_cmds.append(cmd)
            or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        ),
    )
    task_runner.run_review_gate_step(
        commit_sha=same_sha,
        task_id="CHILD-V2",
        packet_path=pkt2,
        out_dir=tmp_path / "rev_child",
        base_sha=base_sha,
        repo_root=tmp_path,
        ledger=None,
    )
    task_runner.run_receipt_gate_step(
        task_id="CHILD-V2",
        phase=2,
        base_sha=base_sha,
        candidate_sha=same_sha,
        verifier_json=tmp_path / "v.json",
        review_json=tmp_path / "r.json",
        readiness_json=r2,
        out_dir=tmp_path / "rec_child",
        repo_root=tmp_path,
        ledger=None,
    )
    assert "--ledger" in seen_cmds[0] and str(ledger_path.resolve()) in seen_cmds[0]
    assert "--ledger" in seen_cmds[1] and str(ledger_path.resolve()) in seen_cmds[1]


def test_record_replan_refuses_in_flight_g4_and_forwards_task_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record_replan refuses replans during in-flight G4; wrapper forwards --task-packet."""
    base_sha, pkt1, _ = _setup_repo_with_valid_packet(tmp_path, packet_version=1, budget_usd=6.0)
    pkt2_text = pkt1.read_text(encoding="utf-8").replace(
        "- **Packet Version**: `1`", "- **Packet Version**: `2`"
    )
    pkt2 = tmp_path / "packet_inflight_v2.md"
    pkt2.write_text(pkt2_text, encoding="utf-8")
    r2 = tmp_path / "readiness_inflight_v2.json"
    assert (
        brief_gate.main(
            ["--packet", str(pkt2), "--repo-root", str(tmp_path), "--out", str(r2)]
        )
        == 0
    )

    ledger_path = tmp_path / "harness" / "runs" / "T-RUN-01" / "ledger.json"
    led = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=6.0
    )
    led["state"] = "REVIEW_FROZEN_G4"
    led["active_g4_reservations"] = {"res-live": {"candidate_sha": base_sha}}
    task_runner.save_ledger(ledger_path, led)

    _, rc_active, msg_active = task_runner.record_replan(
        ledger_path, r2, packet_path=pkt2, human_approved=True, repo_root=tmp_path
    )
    assert rc_active == 2
    assert "active in flight" in msg_active

    # Also block when ledger active_g4_reservations is empty yet g4-tracker in_flight is set.
    led["active_g4_reservations"] = {}
    task_runner.save_ledger(ledger_path, led)
    t_path = task_runner.default_g4_tracker_path("T-RUN-01", repo_root=tmp_path)
    t_data = task_runner.load_g4_tracker(t_path, task_id="T-RUN-01")
    t_data["in_flight"]["res-trk"] = {"candidate_sha": base_sha}
    task_runner.save_g4_tracker(t_path, t_data)

    _, rc_trk, msg_trk = task_runner.record_replan(
        ledger_path, r2, packet_path=pkt2, human_approved=True, repo_root=tmp_path
    )
    assert rc_trk == 2
    assert "active in flight" in msg_trk

    # run_receipt_gate_step forwards --task-packet when passed.
    cmds: list[list[str]] = []
    monkeypatch.setattr(
        task_runner,
        "run_managed_cmd",
        lambda cmd, **_kw: (
            cmds.append(cmd)
            or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        ),
    )
    task_runner.run_receipt_gate_step(
        task_id="T-RUN-01",
        phase=2,
        base_sha=base_sha,
        candidate_sha=base_sha,
        verifier_json=tmp_path / "v.json",
        review_json=tmp_path / "r.json",
        readiness_json=r2,
        out_dir=tmp_path / "rec_pkt",
        task_packet=pkt2,
        repo_root=tmp_path,
        ledger=None,
    )
    assert "--task-packet" in cmds[0] and str(pkt2) in cmds[0]


def test_packet_epoch_discover_child_runs_and_merge_packet_rounds(tmp_path: Path) -> None:
    """extract_packet_epoch, discover_child_runs_for_root, and merge preserve epochs."""
    p1 = tmp_path / "p1.md"
    p1.write_text("# Packet\n\n- **Packet Version**: `1`\nBody A\n", encoding="utf-8")
    p1_edit = tmp_path / "p1_edit.md"
    p1_edit.write_text("# Packet\n\n- **Packet Version**: `1`\nBody B\n", encoding="utf-8")
    p2 = tmp_path / "p2.md"
    p2.write_text("# Packet\n\n- **Packet Version**: `2`\nBody A\n", encoding="utf-8")

    e1 = task_runner.extract_packet_epoch(p1)
    e1_edit = task_runner.extract_packet_epoch(p1_edit)
    e2 = task_runner.extract_packet_epoch(p2)
    assert e1 is not None and e1.startswith("v1:")
    assert e1_edit is not None and e1_edit != e1
    assert e2 is not None and e2.startswith("v2:")
    assert task_runner.extract_packet_epoch(tmp_path / "missing.md") is None

    runs = tmp_path / "harness" / "runs"
    (runs / "ROOT").mkdir(parents=True)
    (runs / "SIB-PKT" / "attempts").mkdir(parents=True)
    (runs / "SIB-PKT" / "packet-v2.md").write_text(
        "# P\n\n- **Root Task ID**: `ROOT`\n", encoding="utf-8"
    )
    (runs / "SIB-READY").mkdir(parents=True)
    (runs / "SIB-READY" / "readiness.json").write_text(
        json.dumps({"root_task_id": "ROOT"}), encoding="utf-8"
    )
    (runs / "OTHER").mkdir(parents=True)
    (runs / "OTHER" / "packet.md").write_text(
        "# P\n\n- **Root Task ID**: `OTHER`\n", encoding="utf-8"
    )
    found = task_runner.discover_child_runs_for_root(
        "ROOT", repo_root=tmp_path, exclude_ids={"SELF"}
    )
    assert [p.name for p in found] == ["SIB-PKT", "SIB-READY"]

    t_root = task_runner.load_g4_tracker(runs / "ROOT" / "g4-tracker.json", task_id="ROOT")
    t_root["completed_attempts"]["att-1"] = {
        "candidate_sha": "1" * 40,
        "status": "CHANGES_REQUESTED",
        "packet_epoch": e1,
    }
    t_root["packet_rounds"][e1] = 2
    t_sib = task_runner.load_g4_tracker(runs / "SIB-PKT" / "g4-tracker.json", task_id="SIB-PKT")
    t_sib["completed_attempts"]["att-2"] = {
        "candidate_sha": "2" * 40,
        "status": "PASSED",
        "packet_epoch": e1,
    }
    t_sib["in_flight"]["res-3"] = {"candidate_sha": "3" * 40, "packet_epoch": e1}
    merged = task_runner.merge_g4_trackers(t_root, t_sib)
    assert merged is not None
    assert merged["packet_rounds"][e1] == 3
    assert task_runner.count_tracker_packet_rounds(merged, e1) == 4


def test_merge_g4_trackers_idempotent_gcounter_and_rejects_corrupt_types(
    tmp_path: Path,
) -> None:
    """CRDT GCounter merges counter-only rounds/packets/SHAs idempotently; bad types raise."""
    pkt = tmp_path / "p_idemp.md"
    pkt.write_text("# P\n\n- **Packet Version**: `1`\n", encoding="utf-8")
    epoch = task_runner.extract_packet_epoch(pkt)
    assert epoch is not None
    sha1 = "a" * 40
    sha2 = "b" * 40

    root = task_runner.load_g4_tracker(tmp_path / "root_trk.json", task_id="ROOT")
    root["completed_rounds"] = 2
    root["packet_rounds"][epoch] = 2
    root["sha_invocations"][sha1] = 1

    child = task_runner.load_g4_tracker(tmp_path / "child_trk.json", task_id="CHILD-V1")
    child["completed_rounds"] = 1
    child["packet_rounds"][epoch] = 1
    child["sha_invocations"][sha1] = 1
    child["sha_invocations"][sha2] = 1

    m1 = task_runner.merge_g4_trackers(root, child)
    assert m1 is not None
    assert m1["completed_rounds"] == 3
    assert m1["packet_rounds"][epoch] == 3
    assert m1["sha_invocations"][sha1] == 2
    assert m1["sha_invocations"][sha2] == 1

    m2 = task_runner.merge_g4_trackers(m1, child)
    m3 = task_runner.merge_g4_trackers(m2, child)
    assert m2 == m1 and m3 == m1

    # Valid-JSON wrong-type fields must raise ValueError instead of being reset to {}.
    for bad_payload in (
        {"schema_version": 1, "task_id": "ROOT", "in_flight": ["x"]},
        {"schema_version": 1, "task_id": "ROOT", "packet_rounds": []},
        {"schema_version": 1, "task_id": "ROOT", "sha_invocations": {sha1: True}},
        {"schema_version": 1, "task_id": "", "completed_rounds": 0},
        {"schema_version": True, "task_id": "ROOT"},
    ):
        bad_path = tmp_path / "bad_trk.json"
        bad_path.write_text(json.dumps(bad_payload), encoding="utf-8")
        with pytest.raises(ValueError):
            task_runner.load_g4_tracker(bad_path)

    # record_replan refuses corrupt g4-tracker.json fail-closed.
    replan_repo = tmp_path / "replan_repo"
    replan_repo.mkdir()
    _, pkt1, _ = _setup_repo_with_valid_packet(replan_repo, packet_version=1)
    pkt2_text = pkt1.read_text(encoding="utf-8").replace(
        "- **Packet Version**: `1`", "- **Packet Version**: `2`"
    )
    pkt2 = tmp_path / "replan_pkt_v2.md"
    pkt2.write_text(pkt2_text, encoding="utf-8")
    r2 = tmp_path / "replan_r2.json"
    assert (
        brief_gate.main(
            ["--packet", str(pkt2), "--repo-root", str(replan_repo), "--out", str(r2)]
        )
        == 0
    )
    led_path = replan_repo / "harness" / "runs" / "T-RUN-01" / "ledger.json"
    base_sha = _git(replan_repo, "rev-parse", "HEAD")
    task_runner.init_or_load_ledger(
        led_path, root_task_id="T-RUN-01", base_sha=base_sha, budget_usd=6.0
    )
    trk_path = task_runner.default_g4_tracker_path("T-RUN-01", repo_root=replan_repo)
    trk_path.write_text(
        json.dumps({"schema_version": 1, "task_id": "T-RUN-01", "in_flight": ["x"]}),
        encoding="utf-8",
    )
    _, rc_rep, msg_rep = task_runner.record_replan(
        led_path,
        r2,
        packet_path=pkt2,
        human_approved=True,
        repo_root=replan_repo,
    )
    assert rc_rep == 2
    assert "corrupt g4-tracker.json" in msg_rep


def test_ensure_ledger_defaults_rejects_wrong_typed_maps_and_counters(tmp_path: Path) -> None:
    """load_ledger rejects malformed map/counter field types instead of resetting them."""
    led_path = tmp_path / "ledger.json"
    task_runner.init_or_load_ledger(
        led_path, root_task_id="T-BAD-LED", base_sha="0" * 40, budget_usd=6.0
    )
    base_data = json.loads(led_path.read_text(encoding="utf-8"))
    for bad_patch in (
        {"active_g4_reservations": ["live"]},
        {"g4_invocations_by_sha": []},
        {"circuit_breakers": []},
        {"g4_rounds_total": "1"},
        {"g4_rounds_in_packet": True},
        {"g4_invocations_by_sha": {"abc": -1}},
        {"active_g4_reservations": {"r1": "bad"}},
        {"active_reservations": {"r1": True}},
        {"packet_version": True},
        {"auto_replans_used": -1},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": "100"}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": True}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": None}},
        {"circuit_breakers": {"max_g4_rounds_per_root_task": 0}},
    ):
        corrupted = dict(base_data)
        corrupted.update(bad_patch)
        bad_file = tmp_path / "bad_ledger.json"
        bad_file.write_text(json.dumps(corrupted), encoding="utf-8")
        with pytest.raises(ValueError):
            task_runner.load_ledger(bad_file)

    missing_spent = dict(base_data)
    missing_spent.pop("spent_api_equiv_usd", None)
    ms_file = tmp_path / "missing_spent.json"
    ms_file.write_text(json.dumps(missing_spent), encoding="utf-8")
    with pytest.raises(ValueError):
        task_runner.load_ledger(ms_file)

    pol_path = tmp_path / "pol_zero_cap.json"
    pol_path.write_text(
        json.dumps(
            {
                "circuit_breakers": {
                    "max_g4_rounds_per_root_task": 0,
                    "max_transient_retries_per_provider_pass": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    cb = task_runner.load_circuit_breakers(policy_path=pol_path)
    assert cb["max_g4_rounds_per_root_task"] == 6
    assert cb["max_transient_retries_per_provider_pass"] == 0


def test_run_review_gate_step_outer_timeout_handles_late_quota_and_sequential_agy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_review_gate_step scales outer_timeout for late Codex quota + sequential 2-pass AGY."""
    import os
    from harness.scripts import codex_room_sync

    # 1. Verify outer_timeout formula scales with qualified_routes and default pass_total=600.
    repo_scale = tmp_path / "repo_scale"
    (repo_scale / "harness").mkdir(parents=True)
    (repo_scale / "harness" / "model-policy.json").write_text(
        json.dumps(
            {
                "roles": {
                    "independent_reviewer_fallbacks": {
                        "qualified_routes": ["gemini-4-carbon-high", "gemini-4-neon-high"]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pkt_scale = tmp_path / "pkt_scale.md"
    pkt_scale.write_text("# Task Packet\n", encoding="utf-8")
    seen_timeouts: list[float] = []

    def _spy_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_timeouts.append(float(kwargs.get("timeout_sec", 0.0)))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with monkeypatch.context() as mp:
        mp.setattr(task_runner, "run_managed_cmd", _spy_run)
        task_runner.run_review_gate_step(
            commit_sha="a" * 40,
            task_id="T-SCALE",
            packet_path=pkt_scale,
            out_dir=tmp_path / "scale_out",
            repo_root=repo_scale,
            ledger={"deadlines_sec": {"reviewer_pass_total": 600, "reviewer_pass_idle": 180}},
        )
    assert seen_timeouts == [pytest.approx(6120.0)]

    # 2. Live integration: reviewer_pass_total=4 (new 24.8s ceiling vs 20.8s old ceiling):
    # Codex hits transient -> quota (2 * 3.55s), and both sequential AGY passes hit
    # transient -> success (4 * 3.55s = 21.30s active sleep > 20.8s old ceiling, < 24.8s).
    repo_dir = tmp_path / "repo_live_timeout"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "peer@example.com")
    _git(repo_dir, "config", "user.name", "Peer")
    (repo_dir / "harness").mkdir(parents=True)
    (repo_dir / "harness" / "model-policy.json").write_text(
        json.dumps(
            {
                "excluded_model_prefixes": list(task_runner.DEFAULT_EXCLUDED_MODEL_PREFIXES),
                "roles": {
                    "independent_reviewer": {"primary": "gpt-6.1-sol"},
                    "independent_reviewer_fallbacks": {
                        "qualified_routes": ["gemini-4-carbon-high"]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (repo_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "init")
    sha = _git(repo_dir, "rev-parse", "HEAD")

    pkt = tmp_path / "pkt_live.md"
    pkt.write_text("# Task Packet\n", encoding="utf-8")

    fake_home = tmp_path / "fake_home"
    rt_root = fake_home / ".codex-runtime"
    for role in ("pass1", "pass2"):
        r_dir = rt_root / f"topstep-review-{role}"
        r_dir.mkdir(parents=True, exist_ok=True)
        (r_dir / "config.toml").write_text(
            (
                f'model = "{codex_room_sync.EXPECTED_REVIEW_MODEL}"\n'
                f'model_reasoning_effort = "{codex_room_sync.EXPECTED_REASONING_EFFORT}"\n'
                'sandbox_mode = "read-only"\n'
                'approval_policy = "never"\n'
                "[features]\n"
                "multi_agent = false\n"
                "multi_agent_v2 = false\n"
                "agent_message_board = false\n"
            ),
            encoding="utf-8",
        )

    codex_state_dir = tmp_path / "codex_retry_state"
    codex_state_dir.mkdir(parents=True)
    codex_py = tmp_path / "codex_stub.py"
    codex_py.write_text(
        (
            "import pathlib, sys, time\n"
            "if sys.argv[1:2] == ['app-server']:\n"
            "    sys.exit(0)\n"
            "args = sys.argv[1:]\n"
            "out_name = pathlib.Path(args[args.index('-o') + 1]).stem\n"
            "_ = sys.stdin.read()\n"
            f"sf = pathlib.Path({str(codex_state_dir)!r}) / (out_name + '.cnt')\n"
            "n = int(sf.read_text()) + 1 if sf.is_file() else 1\n"
            "sf.write_text(str(n))\n"
            "time.sleep(3.55)\n"
            "if n == 1:\n"
            "    sys.stderr.write('error: temporary network connection reset\\n')\n"
            "else:\n"
            "    sys.stderr.write('error: usage_limit_reached 429\\n')\n"
            "sys.exit(1)\n"
        ),
        encoding="utf-8",
    )
    agy_state_dir = tmp_path / "agy_retry_state"
    agy_state_dir.mkdir(parents=True)
    agy_py = tmp_path / "agy_stub.py"
    agy_py.write_text(
        (
            "import json, pathlib, sys, time\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            "lens = 'security_and_invariants' if 'security_and_invariants' in prompt "
            "else 'correctness_and_tests'\n"
            f"sf = pathlib.Path({str(agy_state_dir)!r}) / (lens + '.cnt')\n"
            "n = int(sf.read_text()) + 1 if sf.is_file() else 1\n"
            "sf.write_text(str(n))\n"
            "time.sleep(3.55)\n"
            "if n == 1:\n"
            "    sys.stderr.write('error: temporary network connection reset\\n')\n"
            "    sys.exit(1)\n"
            f"payload = {{'revision_sha': {sha!r}, 'lens': lens, 'verdict': 'APPROVE', "
            "'summary': 'agy ok', 'findings': [], 'test_quality_issues': [], "
            "'unverified_areas': []}\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', "
            "'model': 'gemini-4-carbon-high', "
            "'usage': {'input_tokens': 4000, 'output_tokens': 800}, "
            "'structured_output': payload}}), flush=True)\n"
        ),
        encoding="utf-8",
    )

    worktree_root = str(Path(task_runner.__file__).resolve().parents[2])
    launcher_py = tmp_path / "review_launcher.py"
    launcher_py.write_text(
        (
            "import sys\n"
            f"sys.path.insert(0, {worktree_root!r})\n"
            "from harness.scripts import codex_room_review\n"
            "codex_room_review.verify_role = "
            "lambda role, root, **_kw: {'ok': True, 'errors': []}\n"
            f"codex_room_review.resolve_codex_command = "
            f"lambda: [sys.executable, '-u', {str(codex_py)!r}]\n"
            f"codex_room_review.resolve_agy_command = "
            f"lambda: [sys.executable, '-u', {str(agy_py)!r}]\n"
            "sys.exit(codex_room_review.main(sys.argv[1:]))\n"
        ),
        encoding="utf-8",
    )

    orig_run_managed = task_runner.run_managed_cmd
    seen_live_timeouts: list[float] = []

    def _run_with_launcher(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_live_timeouts.append(float(kwargs.get("timeout_sec", 0.0)))
        wrapped_cmd = [cmd[0], str(launcher_py), *cmd[2:]]
        return orig_run_managed(wrapped_cmd, **kwargs)

    monkeypatch.setattr(task_runner, "run_managed_cmd", _run_with_launcher)
    monkeypatch.setenv("HOME", str(fake_home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(fake_home))

    ledger_path = task_runner.default_ledger_path("T-OUTER-TIMEOUT", repo_root=repo_dir)
    ledger = task_runner.init_or_load_ledger(
        ledger_path, root_task_id="T-OUTER-TIMEOUT", base_sha=sha, budget_usd=6.0
    )
    ledger["state"] = "REVIEW_FROZEN_G4"
    ledger["candidate_sha"] = sha
    ledger["deadlines_sec"]["reviewer_pass_total"] = 4
    ledger["deadlines_sec"]["reviewer_pass_idle"] = 6
    task_runner.save_ledger(ledger_path, ledger)

    proc = task_runner.run_review_gate_step(
        commit_sha=sha,
        task_id="T-OUTER-TIMEOUT",
        packet_path=pkt,
        out_dir=tmp_path / "live_timeout_out",
        base_sha=sha,
        repo_root=repo_dir,
        ledger=ledger,
        ledger_path=ledger_path,
    )
    assert seen_live_timeouts == [pytest.approx(24.8)]
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    led_after = task_runner.load_ledger(ledger_path)
    assert led_after["active_g4_reservations"] == {}
    assert led_after["spent_api_equiv_usd"] == pytest.approx(0.044)


def test_g4_retry_count_matches_ledger_override_across_wrapper_and_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger retry=0 overrides policy retry=1 in both run_review_gate_step and reviewer."""
    import os
    from harness.scripts import codex_room_review, codex_room_sync

    repo_dir = tmp_path / "repo_retry_parity"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "peer@example.com")
    _git(repo_dir, "config", "user.name", "Peer")
    (repo_dir / "harness").mkdir(parents=True)
    (repo_dir / "harness" / "model-policy.json").write_text(
        json.dumps(
            {
                "excluded_model_prefixes": list(task_runner.DEFAULT_EXCLUDED_MODEL_PREFIXES),
                "roles": {
                    "independent_reviewer": {"primary": "gpt-6.1-sol"},
                    "independent_reviewer_fallbacks": {
                        "qualified_routes": ["gemini-4-carbon-high"]
                    },
                },
                "circuit_breakers": {
                    "max_transient_retries_per_provider_pass": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (repo_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "init")
    sha = _git(repo_dir, "rev-parse", "HEAD")

    pkt = tmp_path / "pkt_parity.md"
    pkt.write_text("# Task Packet\n", encoding="utf-8")

    fake_home = tmp_path / "fake_home"
    rt_root = fake_home / ".codex-runtime"
    for role in ("pass1", "pass2"):
        r_dir = rt_root / f"topstep-review-{role}"
        r_dir.mkdir(parents=True, exist_ok=True)
        (r_dir / "config.toml").write_text(
            (
                f'model = "{codex_room_sync.EXPECTED_REVIEW_MODEL}"\n'
                f'model_reasoning_effort = "{codex_room_sync.EXPECTED_REASONING_EFFORT}"\n'
                'sandbox_mode = "read-only"\n'
                'approval_policy = "never"\n'
                "[features]\n"
                "multi_agent = false\n"
                "multi_agent_v2 = false\n"
                "agent_message_board = false\n"
            ),
            encoding="utf-8",
        )

    calls_dir = tmp_path / "parity_calls"
    calls_dir.mkdir(parents=True)
    codex_py = tmp_path / "codex_transient_stub.py"
    codex_py.write_text(
        (
            "import json, pathlib, sys\n"
            "if sys.argv[1:2] == ['app-server']:\n"
            "    sys.exit(1)\n"
            "args = sys.argv[1:]\n"
            "out_stem = pathlib.Path(args[args.index('-o') + 1]).stem\n"
            "_ = sys.stdin.read()\n"
            f"cf = pathlib.Path({str(calls_dir)!r}) / (out_stem + '.cnt')\n"
            "cnt = int(cf.read_text()) + 1 if cf.is_file() else 1\n"
            "cf.write_text(str(cnt))\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 1000000, 'cached_input_tokens': 0, "
            "'output_tokens': 0}}), flush=True)\n"
            "sys.stderr.write('error: temporary network connection reset\\n')\n"
            "sys.exit(1)\n"
        ),
        encoding="utf-8",
    )

    # 1. Direct codex_room_review.main --ledger (no --max-transient-retries flag) uses ledger=0.
    direct_ledger_path = task_runner.default_ledger_path("T-PARITY-DIRECT", repo_root=repo_dir)
    direct_ledger = task_runner.init_or_load_ledger(
        direct_ledger_path, root_task_id="T-PARITY-DIRECT", base_sha=sha, budget_usd=6.0
    )
    direct_ledger["state"] = "REVIEW_FROZEN_G4"
    direct_ledger["candidate_sha"] = sha
    direct_ledger["circuit_breakers"]["max_transient_retries_per_provider_pass"] = 0
    task_runner.save_ledger(direct_ledger_path, direct_ledger)

    out_direct = tmp_path / "out_parity_direct"
    with monkeypatch.context() as mp:
        mp.setattr(
            codex_room_review, "verify_role", lambda role, root: {"ok": True, "errors": []}
        )
        mp.setattr(
            codex_room_review,
            "resolve_codex_command",
            lambda: [sys.executable, "-u", str(codex_py)],
        )
        rc_direct = codex_room_review.main(
            [
                "--commit",
                sha,
                "--task-id",
                "T-PARITY-DIRECT",
                "--ledger",
                str(direct_ledger_path),
                "--out-dir",
                str(out_direct),
                "--repo-root",
                str(repo_dir),
                "--backend",
                "codex",
            ]
        )

    pass1_direct = int((calls_dir / "pass1.cnt").read_text())
    pass2_direct = int((calls_dir / "pass2.cnt").read_text())
    rep_direct = json.loads((out_direct / "consolidated-review.json").read_text(encoding="utf-8"))
    led_direct_after = task_runner.load_ledger(direct_ledger_path)
    assert rc_direct == 2
    assert pass1_direct == 1 and pass2_direct == 1
    assert rep_direct["review_gate_status"] == "BLOCKED_INFRA"
    assert led_direct_after["active_g4_reservations"] == {}
    assert led_direct_after["spent_api_equiv_usd"] == pytest.approx(5.0)

    # 2. Wrapper run_review_gate_step (ledger=None, loads disk ledger) passes retry=0 & deadline.
    for f in calls_dir.glob("*.cnt"):
        f.unlink()
    wrap_ledger_path = task_runner.default_ledger_path("T-PARITY-WRAP", repo_root=repo_dir)
    wrap_ledger = task_runner.init_or_load_ledger(
        wrap_ledger_path, root_task_id="T-PARITY-WRAP", base_sha=sha, budget_usd=6.0
    )
    wrap_ledger["state"] = "REVIEW_FROZEN_G4"
    wrap_ledger["candidate_sha"] = sha
    wrap_ledger["circuit_breakers"]["max_transient_retries_per_provider_pass"] = 0
    task_runner.save_ledger(wrap_ledger_path, wrap_ledger)

    worktree_root = str(Path(task_runner.__file__).resolve().parents[2])
    launcher_py = tmp_path / "parity_launcher.py"
    launcher_py.write_text(
        (
            "import sys\n"
            f"sys.path.insert(0, {worktree_root!r})\n"
            "from harness.scripts import codex_room_review\n"
            "codex_room_review.verify_role = "
            "lambda role, root, **_kw: {'ok': True, 'errors': []}\n"
            f"codex_room_review.resolve_codex_command = "
            f"lambda: [sys.executable, '-u', {str(codex_py)!r}]\n"
            "sys.exit(codex_room_review.main(sys.argv[1:]))\n"
        ),
        encoding="utf-8",
    )
    orig_run_managed = task_runner.run_managed_cmd
    seen_wrap_timeouts: list[float] = []
    seen_wrap_cmds: list[list[str]] = []

    def _wrap_launcher(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_wrap_timeouts.append(float(kwargs.get("timeout_sec", 0.0)))
        seen_wrap_cmds.append(list(cmd))
        return orig_run_managed([cmd[0], str(launcher_py), *cmd[2:]], **kwargs)

    monkeypatch.setattr(task_runner, "run_managed_cmd", _wrap_launcher)
    monkeypatch.setenv("HOME", str(fake_home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(fake_home))

    out_wrap = tmp_path / "out_parity_wrap"
    proc = task_runner.run_review_gate_step(
        commit_sha=sha,
        task_id="T-PARITY-WRAP",
        packet_path=pkt,
        out_dir=out_wrap,
        base_sha=sha,
        repo_root=repo_dir,
        ledger=None,
        ledger_path=wrap_ledger_path,
    )

    pass1_wrap = int((calls_dir / "pass1.cnt").read_text())
    pass2_wrap = int((calls_dir / "pass2.cnt").read_text())
    rep_wrap = json.loads((out_wrap / "consolidated-review.json").read_text(encoding="utf-8"))
    led_wrap_after = task_runner.load_ledger(wrap_ledger_path)
    assert proc.returncode == 2
    assert seen_wrap_timeouts == [pytest.approx(1920.0)]
    assert "--max-transient-retries" in seen_wrap_cmds[0]
    idx_flag = seen_wrap_cmds[0].index("--max-transient-retries")
    assert seen_wrap_cmds[0][idx_flag + 1] == "0"
    assert pass1_wrap == 1 and pass2_wrap == 1
    assert rep_wrap["review_gate_status"] == "BLOCKED_INFRA"
    assert led_wrap_after["active_g4_reservations"] == {}
    assert led_wrap_after["spent_api_equiv_usd"] == pytest.approx(5.0)







