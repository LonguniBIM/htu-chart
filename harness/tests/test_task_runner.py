"""Tests for task_runner.py durable ledger, circuit breakers, and budget accounting."""

from __future__ import annotations

import concurrent.futures
import math
import shutil
import subprocess
import sys
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
