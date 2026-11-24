"""Durable Circuit-Breaker & Budget State Runner for S-L-P orchestration.

Persists per-root-task state atomically in ``ledger.json``, enforces G0.5
readiness hash & model policy checks before Peer dispatch, classifies Peer
escalations vs 0-token infrastructure startup errors, enforces circuit breakers,
and deduplicates cumulative conversation token usage for USD budgeting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from harness.scripts.brief_gate import (
        DEFAULT_EXCLUDED_MODEL_PREFIXES,
        _normalize_rel_path,
        _paths_overlap,
        _validate_rel_path,
    )
    from harness.scripts.schema_utils import (
        is_hex_sha40,
        is_strict_int,
        resolve_git_toplevel,
        run_managed_cmd,
    )
except ImportError:
    from brief_gate import (
        DEFAULT_EXCLUDED_MODEL_PREFIXES,
        _normalize_rel_path,
        _paths_overlap,
        _validate_rel_path,
    )
    from schema_utils import (
        is_hex_sha40,
        is_strict_int,
        resolve_git_toplevel,
        run_managed_cmd,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DEADLINES_SEC: dict[str, int] = {
    "brief_generation": 300,
    "peer_round": 600,
    "verifier_step": 180,
    "verifier_total": 420,
    "reviewer_pass_total": 600,
    "reviewer_pass_idle": 180,
    "root_task_wall_clock": 3600,
}

DEFAULT_CIRCUIT_BREAKERS: dict[str, int] = {
    "reassess_after_failed_candidates": 2,
    "max_candidates_per_packet_version": 3,
    "max_auto_replans_per_root_task": 1,
    "max_total_candidates_per_root_task": 6,
    "max_g4_rounds_per_root_task": 6,
    "max_g4_rounds_per_packet_version": 3,
    "max_g4_invocations_per_candidate_sha": 2,
    "max_transient_retries_per_provider_pass": 1,
}


def load_circuit_breakers(
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, int]:
    """Load integer circuit-breaker caps from model-policy.json with safe defaults."""
    breakers = dict(DEFAULT_CIRCUIT_BREAKERS)
    policy_file = (
        policy_path.resolve()
        if policy_path is not None
        else ((repo_root or REPO_ROOT).resolve() / "harness" / "model-policy.json")
    )
    if not policy_file.is_file():
        return breakers
    try:
        data = json.loads(policy_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return breakers
    raw_cb = data.get("circuit_breakers") if isinstance(data, dict) else None
    if not isinstance(raw_cb, dict):
        return breakers
    for key in DEFAULT_CIRCUIT_BREAKERS:
        val = raw_cb.get(key)
        if is_strict_int(val) and val >= 0:
            breakers[key] = int(val)
    return breakers


def _read_optional_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def count_review_attempts(
    attempts_dirs: list[Path],
    *,
    candidate_sha: str | None = None,
) -> tuple[int, int]:
    """Count completed/locked G4 attempts and prior invocations for ``candidate_sha``."""
    seen_dirs: set[Path] = set()
    prior_rounds = 0
    same_sha = 0
    completed_statuses = {"PASSED", "CHANGES_REQUESTED", "BLOCKED"}

    for raw_dir in attempts_dirs:
        resolved_dir = raw_dir.resolve()
        if resolved_dir in seen_dirs or not resolved_dir.is_dir():
            continue
        seen_dirs.add(resolved_dir)

        for child in sorted(resolved_dir.iterdir()):
            if not child.is_dir():
                continue
            lock_data = _read_optional_json_dict(child / ".review-attempt.lock")
            rev_data = _read_optional_json_dict(child / "consolidated-review.json")
            if lock_data is None and rev_data is None:
                continue
            if rev_data is None and lock_data is not None and lock_data.get("dry_run") is True:
                continue

            if rev_data is not None:
                status = rev_data.get("review_gate_status")
                if status in completed_statuses or not rev_data:
                    prior_rounds += 1
            elif lock_data is not None:
                prior_rounds += 1

            if candidate_sha:
                lock_sha = lock_data.get("candidate_sha") if lock_data is not None else None
                rev_sha = rev_data.get("revision_sha") if rev_data is not None else None
                matched_sha = (
                    lock_sha == candidate_sha
                    or rev_sha == candidate_sha
                    or (
                        lock_sha is None
                        and rev_sha is None
                        and child.name.endswith(f"-{candidate_sha[:12]}")
                    )
                )
                if matched_sha:
                    same_sha += 1

    return prior_rounds, same_sha


def _ensure_ledger_defaults(
    data: dict[str, Any],
    *,
    policy_breakers: dict[str, int] | None = None,
) -> dict[str, Any]:
    defaults = policy_breakers or DEFAULT_CIRCUIT_BREAKERS
    breakers = data.setdefault("circuit_breakers", {})
    if isinstance(breakers, dict):
        for key, val in defaults.items():
            breakers.setdefault(key, val)
    data.setdefault("g4_rounds_total", 0)
    data.setdefault("g4_rounds_in_packet", 0)
    if not isinstance(data.get("g4_invocations_by_sha"), dict):
        data["g4_invocations_by_sha"] = {}
    if not isinstance(data.get("active_g4_reservations"), dict):
        data["active_g4_reservations"] = {}
    data.setdefault("last_g4_candidate_sha", None)
    data.setdefault("last_g4_status", None)
    return data

DEFAULT_PRICING_PER_1M: dict[str, dict[str, float]] = {
    "gemini-4-neon-": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    "gemini-4-carbon-": {"input": 2.50, "output": 15.00, "cache_read": 0.25},
    "gemini-4-hydrogen-": {"input": 0.50, "output": 3.00, "cache_read": 0.05},
    "claude-opus-5-5-": {"input": 5.00, "output": 25.00, "cache_read": 0.50},
    "gpt-6.1-sol": {"input": 2.50, "output": 12.00, "cache_read": 0.25},
}

OPAQUE_MODEL_ALIASES = frozenset({"flash", "flash_lite", "pro", "inherit"})
DISPATCH_ALLOWED_STATES = frozenset({"INTAKE_G0", "BRIEF_GATE_G05", "ACTIVE_PEER_G1"})
PEER_ESCALATION_SIGNALS = frozenset({"REOPEN_REQUEST", "DEPENDENCY_REQUEST", "BLOCKED"})
SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SIGNAL_HEADER_RE = re.compile(
    r"^(?:@\S+[ \t]+)?(?:\[(?i:done|blocked|reopen|dependency|peer)\][ \t]+)?"
    r"[#*` \t]*\b(CANDIDATE_READY|REOPEN_REQUEST|DEPENDENCY_REQUEST|BLOCKED)\b"
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_LOCK_STATE = threading.local()


def _require_finite_number(
    value: Any,
    name: str,
    *,
    allow_zero: bool = True,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, got {value!r}.")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"{name} must be finite (not NaN/Inf), got {value!r}.")
    if positive and num <= 0.0:
        raise ValueError(f"{name} must be > 0, got {value!r}.")
    if not allow_zero and num <= 0.0:
        raise ValueError(f"{name} must be > 0, got {value!r}.")
    if num < 0.0:
        raise ValueError(f"{name} must be non-negative, got {value!r}.")
    return num


def _get_thread_lock(resolved: Path) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[resolved] = lock
        return lock


def _acquire_os_lock(fh: Any, timeout_sec: float = 10.0) -> None:
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + timeout_sec
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out acquiring Windows ledger file lock.") from exc
                time.sleep(0.01)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _release_os_lock(fh: Any) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def _ledger_lock(ledger_path: Path) -> Iterator[None]:
    resolved = ledger_path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tlock = _get_thread_lock(resolved)
    depths: dict[Path, int] = getattr(_LOCK_STATE, "depths", {})
    if not hasattr(_LOCK_STATE, "depths"):
        _LOCK_STATE.depths = depths

    with tlock:
        depth = depths.get(resolved, 0)
        if depth > 0:
            depths[resolved] = depth + 1
            try:
                yield
            finally:
                depths[resolved] = depth
            return

        lock_path = resolved.parent / f"{resolved.name}.lock"
        fh = lock_path.open("a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            _acquire_os_lock(fh)
            depths[resolved] = 1
            try:
                yield
            finally:
                depths.pop(resolved, None)
                try:
                    _release_os_lock(fh)
                except OSError:
                    pass
        finally:
            fh.close()


def _validate_safe_task_id(task_id: str, label: str = "root_task_id") -> str:
    cleaned = task_id.strip() if isinstance(task_id, str) else ""
    if not cleaned or not SAFE_TASK_ID_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid {label} {task_id!r} (must match {SAFE_TASK_ID_RE.pattern})."
        )
    return cleaned


def default_ledger_path(root_task_id: str, repo_root: Path | None = None) -> Path:
    safe_id = _validate_safe_task_id(root_task_id, "root_task_id")
    root = (repo_root or REPO_ROOT).resolve()
    return root / "harness" / "runs" / safe_id / "ledger.json"


def default_g4_tracker_path(task_id: str, repo_root: Path | None = None) -> Path:
    """Return canonical ``harness/runs/<task_id>/g4-tracker.json`` path."""
    safe_id = _validate_safe_task_id(task_id, "task_id")
    root = (repo_root or REPO_ROOT).resolve()
    return root / "harness" / "runs" / safe_id / "g4-tracker.json"


def _atomic_write_json(target_path: Path, data: dict[str, Any]) -> None:
    resolved = target_path.resolve()
    payload = json.dumps(data, indent=2, allow_nan=False) + "\n"
    with _ledger_lock(resolved):
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = f".{resolved.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp_path = resolved.parent / tmp_name
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(10):
                try:
                    os.replace(tmp_path, resolved)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise


def load_g4_tracker(tracker_path: Path, *, task_id: str = "") -> dict[str, Any]:
    """Load canonical ``g4-tracker.json`` or initialize an empty tracker payload."""
    resolved = tracker_path.resolve()
    data: dict[str, Any] = {}
    if resolved.is_file():
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid g4-tracker payload at {resolved}")
        data = loaded
    data.setdefault("schema_version", 1)
    if task_id:
        data.setdefault("task_id", task_id)
    data.setdefault("completed_rounds", 0)
    if not isinstance(data.get("sha_invocations"), dict):
        data["sha_invocations"] = {}
    if not isinstance(data.get("in_flight"), dict):
        data["in_flight"] = {}
    if not isinstance(data.get("attempts"), dict):
        data["attempts"] = {}
    return data


def save_g4_tracker(tracker_path: Path, tracker: dict[str, Any]) -> None:
    """Atomically persist ``g4-tracker.json`` under its OS lock."""
    tracker["updated_at_utc"] = _now_utc().isoformat()
    _atomic_write_json(tracker_path, tracker)


def save_ledger(ledger_path: Path, ledger: dict[str, Any]) -> None:
    """Atomically write ``ledger`` to ``ledger_path`` via fsync + os.replace."""
    _require_finite_number(ledger.get("budget_limit_usd"), "budget_limit_usd", positive=True)
    _require_finite_number(ledger.get("spent_api_equiv_usd", 0.0), "spent_api_equiv_usd")
    for res_id, res_amt in ledger.get("active_reservations", {}).items():
        _require_finite_number(res_amt, f"active_reservations[{res_id!r}]")

    ledger["updated_at_utc"] = _now_utc().isoformat()
    _atomic_write_json(ledger_path, ledger)


def load_ledger(
    ledger_path: Path,
    *,
    expected_root_task_id: str | None = None,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Load, backfill schema defaults, and validate an existing ``ledger.json``."""
    resolved = ledger_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Ledger not found: {resolved}")
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("root_task_id"):
        raise ValueError(f"Invalid ledger payload at {resolved}")
    _require_finite_number(data.get("budget_limit_usd"), "budget_limit_usd", positive=True)
    if expected_root_task_id is not None and data["root_task_id"] != expected_root_task_id:
        raise ValueError(
            f"Root task mismatch: ledger belongs to {data['root_task_id']!r}, "
            f"cannot bind {expected_root_task_id!r}."
        )
    policy_breakers = load_circuit_breakers(repo_root=repo_root, policy_path=policy_path)
    return _ensure_ledger_defaults(data, policy_breakers=policy_breakers)


def init_or_load_ledger(
    ledger_path: Path,
    *,
    root_task_id: str,
    task_id: str | None = None,
    packet_version: int = 1,
    base_sha: str = "",
    budget_usd: float = 6.0,
    deadlines_sec: dict[str, int] | None = None,
    circuit_breakers: dict[str, int] | None = None,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Create a new ledger keyed by ``root_task_id`` or load the existing one intact."""
    safe_root_id = _validate_safe_task_id(root_task_id, "root_task_id")
    if task_id is not None:
        _validate_safe_task_id(task_id, "task_id")
    valid_budget = _require_finite_number(budget_usd, "budget_usd", positive=True)
    if not is_strict_int(packet_version) or packet_version < 1:
        raise ValueError(f"packet_version must be integer >= 1, got {packet_version!r}.")

    resolved = ledger_path.resolve()
    with _ledger_lock(resolved):
        if resolved.is_file():
            return load_ledger(
                resolved,
                expected_root_task_id=safe_root_id,
                repo_root=repo_root,
                policy_path=policy_path,
            )

        now_iso = _now_utc().isoformat()
        merged_deadlines = dict(DEFAULT_DEADLINES_SEC)
        if deadlines_sec:
            merged_deadlines.update(deadlines_sec)
        merged_breakers = load_circuit_breakers(repo_root=repo_root, policy_path=policy_path)
        if circuit_breakers:
            merged_breakers.update(circuit_breakers)

        ledger: dict[str, Any] = {
            "schema_version": 2,
            "root_task_id": safe_root_id,
            "task_id": task_id or safe_root_id,
            "packet_version": int(packet_version),
            "base_sha": base_sha,
            "candidate_sha": None,
            "state": "INTAKE_G0",
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
            "deadlines_sec": merged_deadlines,
            "circuit_breakers": merged_breakers,
            "total_candidates": 0,
            "candidates_in_packet": 0,
            "consecutive_failed_candidates": 0,
            "g4_rounds_total": 0,
            "g4_rounds_in_packet": 0,
            "g4_invocations_by_sha": {},
            "active_g4_reservations": {},
            "last_g4_candidate_sha": None,
            "last_g4_status": None,
            "auto_replans_used": 0,
            "transient_retries_in_stage": 0,
            "budget_limit_usd": valid_budget,
            "spent_api_equiv_usd": 0.0,
            "active_reservations": {},
            "budget_warning": False,
            "usage_by_conversation": {},
            "events": [],
        }
        save_ledger(resolved, ledger)
        return ledger


def _append_event(
    ledger: dict[str, Any],
    kind: str,
    detail: dict[str, Any] | None = None,
) -> None:
    event: dict[str, Any] = {
        "ts_utc": _now_utc().isoformat(),
        "kind": kind,
        "state": ledger.get("state"),
    }
    if detail:
        event["detail"] = detail
    ledger.setdefault("events", []).append(event)


def run_managed_stage_cmd(
    stage: str,
    cmd: list[str],
    *,
    ledger: dict[str, Any] | None = None,
    cwd: Path | None = None,
    timeout_override: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a harness/git command under process-tree containment with stage timeout."""
    deadlines = ledger.get("deadlines_sec", {}) if ledger else DEFAULT_DEADLINES_SEC
    timeout_sec = float(
        timeout_override
        if timeout_override is not None
        else deadlines.get(stage, DEFAULT_DEADLINES_SEC.get(stage, 180))
    )
    return run_managed_cmd(cmd, cwd=cwd, timeout_sec=timeout_sec, text=True)


def run_brief_gate_step(
    packet_path: Path,
    readiness_out: Path,
    *,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
    ledger: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``brief_gate.py`` via managed subprocess with ``brief_generation`` deadline."""
    root = (repo_root or REPO_ROOT).resolve()
    script = Path(__file__).resolve().parent / "brief_gate.py"
    cmd = [
        sys.executable,
        str(script),
        "--packet",
        str(packet_path),
        "--repo-root",
        str(root),
        "--out",
        str(readiness_out),
        "--json",
    ]
    if policy_path is not None:
        cmd.extend(["--policy", str(policy_path)])
    return run_managed_stage_cmd("brief_generation", cmd, ledger=ledger, cwd=root)


def run_verifier_gate_step(
    *,
    phase: int,
    commit_sha: str,
    out_json: Path,
    repo_root: Path | None = None,
    ledger: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``build_verifier.py`` via managed subprocess with ``verifier_total`` deadline."""
    root = (repo_root or REPO_ROOT).resolve()
    script = Path(__file__).resolve().parent / "build_verifier.py"
    step_timeout = int(
        (ledger or {}).get("deadlines_sec", DEFAULT_DEADLINES_SEC).get("verifier_step", 180)
    )
    total_timeout = int(
        (ledger or {}).get("deadlines_sec", DEFAULT_DEADLINES_SEC).get("verifier_total", 420)
    )
    cmd = [
        sys.executable,
        str(script),
        "--root",
        str(root),
        "--phase",
        str(phase),
        "--strict-clean",
        "--commit",
        commit_sha,
        "--step-timeout",
        str(step_timeout),
        "--total-timeout",
        str(total_timeout),
        "--out",
        str(out_json),
        "--json",
    ]
    return run_managed_stage_cmd("verifier_total", cmd, ledger=ledger, cwd=root)


def run_review_gate_step(
    *,
    commit_sha: str,
    task_id: str,
    packet_path: Path,
    out_dir: Path,
    base_sha: str | None = None,
    repo_root: Path | None = None,
    ledger: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``codex_room_review.py`` via managed subprocess with reviewer deadlines."""
    root = (repo_root or REPO_ROOT).resolve()
    script = Path(__file__).resolve().parent / "codex_room_review.py"
    deadlines = (ledger or {}).get("deadlines_sec", DEFAULT_DEADLINES_SEC)
    pass_total = int(deadlines.get("reviewer_pass_total", 600))
    pass_idle = int(deadlines.get("reviewer_pass_idle", 180))
    effective_base = base_sha or (str(ledger.get("base_sha", "")) if ledger else "")
    cmd = [
        sys.executable,
        str(script),
        "--commit",
        commit_sha,
        "--task-id",
        task_id,
        "--task-packet",
        str(packet_path),
        "--repo-root",
        str(root),
        "--out-dir",
        str(out_dir),
        "--timeout",
        str(pass_total),
        "--idle-timeout",
        str(pass_idle),
        "--fail-on-blocking",
    ]
    if effective_base:
        cmd.extend(["--base", effective_base])
    outer_timeout = float(max(pass_total * 2 + 60, pass_total + 60))
    return run_managed_stage_cmd(
        "reviewer_pass_total",
        cmd,
        ledger=ledger,
        cwd=root,
        timeout_override=outer_timeout,
    )


def run_receipt_gate_step(
    *,
    task_id: str,
    phase: int,
    base_sha: str,
    candidate_sha: str,
    verifier_json: Path,
    review_json: Path,
    readiness_json: Path,
    out_dir: Path,
    is_bugfix: bool = False,
    red_proof_json: Path | None = None,
    repo_root: Path | None = None,
    ledger: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``receipt.py`` via managed subprocess with ``verifier_step`` deadline."""
    root = (repo_root or REPO_ROOT).resolve()
    script = Path(__file__).resolve().parent / "receipt.py"
    cmd = [
        sys.executable,
        str(script),
        "--task-id",
        task_id,
        "--phase",
        str(phase),
        "--base-sha",
        base_sha,
        "--candidate-sha",
        candidate_sha,
        "--verifier-json",
        str(verifier_json),
        "--review-json",
        str(review_json),
        "--task-manifest",
        str(readiness_json),
        "--repo-root",
        str(root),
        "--out-dir",
        str(out_dir),
    ]
    if is_bugfix:
        cmd.append("--bugfix")
    if red_proof_json is not None:
        cmd.extend(["--red-proof-json", str(red_proof_json)])
    effective_ledger = ledger_path
    if effective_ledger is None:
        candidate_ledger = root / "harness" / "runs" / task_id / "ledger.json"
        if candidate_ledger.is_file():
            effective_ledger = candidate_ledger
    if effective_ledger is not None:
        cmd.extend(["--ledger", str(effective_ledger)])
    return run_managed_stage_cmd("verifier_step", cmd, ledger=ledger, cwd=root)


def _active_reserved_usd(ledger: dict[str, Any]) -> float:
    reservations = ledger.get("active_reservations", {})
    if not isinstance(reservations, dict):
        return 0.0
    return sum(float(v) for v in reservations.values())


def _refresh_budget_warning(ledger: dict[str, Any]) -> float:
    spent = float(ledger.get("spent_api_equiv_usd", 0.0))
    reserved = _active_reserved_usd(ledger)
    limit = float(ledger.get("budget_limit_usd", 0.0))
    committed = round(spent + reserved, 8)
    if limit > 0 and committed >= round(0.8 * limit, 8):
        ledger["budget_warning"] = True
    return committed


def _is_model_excluded(
    model_name: str,
    excluded_prefixes: tuple[str, ...] = DEFAULT_EXCLUDED_MODEL_PREFIXES,
) -> bool:
    norm = model_name.strip().lower()
    if not norm or norm in OPAQUE_MODEL_ALIASES:
        return True
    return any(norm.startswith(p.lower()) for p in excluded_prefixes)


def _load_policy_state(
    repo_root: Path,
    policy_path: Path | None = None,
) -> tuple[tuple[str, ...], str | None, str | None]:
    policy_file = (
        policy_path.resolve()
        if policy_path is not None
        else (repo_root / "harness" / "model-policy.json").resolve()
    )
    if policy_path is not None and not policy_file.is_file():
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, f"Policy file not found: {policy_file}"
    if not policy_file.is_file():
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, None
    try:
        raw_bytes = policy_file.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return (
            DEFAULT_EXCLUDED_MODEL_PREFIXES,
            None,
            f"Malformed model-policy.json ({policy_file}): {exc}",
        )
    if not isinstance(data, dict):
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, "model-policy.json must be a JSON object."

    prefixes = data.get("excluded_model_prefixes", [])
    if not isinstance(prefixes, list) or not all(
        isinstance(p, str) and p.strip() for p in prefixes
    ):
        return (
            DEFAULT_EXCLUDED_MODEL_PREFIXES,
            None,
            "model-policy.json 'excluded_model_prefixes' must be a list of non-empty strings.",
        )

    merged: list[str] = list(DEFAULT_EXCLUDED_MODEL_PREFIXES)
    seen = {p.lower() for p in merged}
    for raw_p in prefixes:
        cleaned = raw_p.strip()
        if cleaned.lower() not in seen:
            merged.append(cleaned)
            seen.add(cleaned.lower())
    return tuple(merged), _sha256_bytes(raw_bytes), None


def check_dispatch(
    ledger_path: Path,
    readiness_path: Path,
    packet_path: Path,
    peer_model: str,
    *,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
    reserve_usd: float = 0.0,
    reservation_id: str | None = None,
    now_utc: datetime | None = None,
) -> tuple[bool, dict[str, Any], int, str]:
    """Validate state, readiness hash, model policy, breakers, and budget before Peer launch."""
    root = (repo_root or REPO_ROOT).resolve()
    valid_reserve = _require_finite_number(reserve_usd, "reserve_usd")
    if valid_reserve > 0 and (not reservation_id or not reservation_id.strip()):
        raise ValueError("reservation_id must be non-empty when reserve_usd > 0.")

    with _ledger_lock(ledger_path):
        return _check_dispatch_locked(
            ledger_path,
            readiness_path,
            packet_path,
            peer_model,
            root=root,
            policy_path=policy_path,
            reserve_usd=valid_reserve,
            reservation_id=reservation_id,
            now_utc=now_utc,
        )


def _check_dispatch_locked(
    ledger_path: Path,
    readiness_path: Path,
    packet_path: Path,
    peer_model: str,
    *,
    root: Path,
    policy_path: Path | None,
    reserve_usd: float,
    reservation_id: str | None,
    now_utc: datetime | None,
) -> tuple[bool, dict[str, Any], int, str]:
    ledger = load_ledger(ledger_path)

    if ledger["state"] not in DISPATCH_ALLOWED_STATES:
        msg = f"Dispatch forbidden in state {ledger['state']!r}."
        _append_event(ledger, "DISPATCH_REJECTED", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if not readiness_path.is_file():
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Missing readiness.json at {readiness_path}."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    try:
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Invalid readiness.json: {exc}"
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if not isinstance(readiness, dict):
        ledger["state"] = "BLOCKED_BRIEF"
        msg = "readiness.json must be a JSON object."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if readiness.get("root_task_id") != ledger["root_task_id"]:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = (
            f"readiness root_task_id ({readiness.get('root_task_id')!r}) != "
            f"ledger root_task_id ({ledger['root_task_id']!r})."
        )
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if readiness.get("packet_version") != ledger["packet_version"]:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = (
            f"readiness packet_version ({readiness.get('packet_version')!r}) != "
            f"ledger packet_version ({ledger['packet_version']!r})."
        )
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    r_status = readiness.get("status")
    if r_status != "VALID":
        new_state = r_status if r_status in {"BLOCKED_BRIEF", "NEEDS_REPLAN"} else "BLOCKED_BRIEF"
        ledger["state"] = new_state
        msg = f"readiness status is {r_status!r}, expected 'VALID'."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if not packet_path.is_file():
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Task packet missing at {packet_path}."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    raw_packet = packet_path.read_bytes()
    if len(raw_packet) == 0 or not raw_packet.decode("utf-8", errors="replace").strip():
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Task packet at {packet_path} is empty."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    current_packet_sha = _sha256_bytes(raw_packet)
    if current_packet_sha != readiness.get("packet_sha256"):
        ledger["state"] = "BLOCKED_BRIEF"
        msg = (
            f"Stale readiness.json: packet SHA-256 mismatch "
            f"({current_packet_sha[:12]} != {str(readiness.get('packet_sha256'))[:12]})."
        )
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    base_sha = readiness.get("base_sha", "")
    if not is_hex_sha40(base_sha):
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Invalid readiness base_sha {base_sha!r}."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    ledger_base = str(ledger.get("base_sha", ""))
    if ledger_base and ledger_base != base_sha:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"readiness base_sha ({base_sha}) != ledger base_sha ({ledger_base})."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg
    if not ledger_base:
        ledger["base_sha"] = base_sha

    try:
        git_top = resolve_git_toplevel(root, timeout_sec=30.0)
        rev = run_managed_stage_cmd(
            "brief_generation",
            ["git", "-C", str(git_top), "rev-parse", "--verify", f"{base_sha}^{{commit}}"],
            ledger=ledger,
            timeout_override=30.0,
        )
    except (ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Unable to verify base_sha {base_sha} in git: {exc}"
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if rev.returncode != 0:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = f"Base SHA {base_sha} does not resolve in git."
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    effective_policy = policy_path or (
        Path(str(readiness["policy_path"])) if readiness.get("policy_path") else None
    )
    excluded_prefixes, current_policy_sha, policy_err = _load_policy_state(
        root, effective_policy
    )
    if policy_err:
        ledger["state"] = "BLOCKED"
        _append_event(ledger, "MODEL_POLICY_BLOCKED", {"reason": policy_err})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, policy_err

    if readiness.get("policy_sha256") != current_policy_sha:
        ledger["state"] = "BLOCKED_BRIEF"
        msg = (
            f"Stale readiness.json: policy_sha256 mismatch "
            f"({readiness.get('policy_sha256')!r} != {current_policy_sha!r})."
        )
        _append_event(ledger, "DISPATCH_BLOCKED_BRIEF", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    eligible = readiness.get("eligible_peer_models", [])
    if (
        _is_model_excluded(peer_model, excluded_prefixes)
        or not isinstance(eligible, list)
        or peer_model not in eligible
    ):
        ledger["state"] = "BLOCKED"
        msg = (
            f"Model policy violation: {peer_model!r} is excluded or not in "
            f"eligible_peer_models={eligible!r}."
        )
        _append_event(ledger, "MODEL_POLICY_BLOCKED", {"peer_model": peer_model, "reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    breakers = ledger["circuit_breakers"]
    if int(ledger["total_candidates"]) >= int(breakers["max_total_candidates_per_root_task"]):
        ledger["state"] = "PAUSED_BUDGET"
        msg = "Circuit breaker: max_total_candidates_per_root_task reached."
        _append_event(ledger, "CIRCUIT_BREAKER_PAUSED", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    g4_in_flight = len(ledger.get("active_g4_reservations", {}))
    max_g4_root = int(breakers.get("max_g4_rounds_per_root_task", 6))
    if int(ledger.get("g4_rounds_total", 0)) + g4_in_flight >= max_g4_root:
        if g4_in_flight == 0:
            ledger["state"] = "PAUSED_BUDGET"
            msg = "Circuit breaker: max_g4_rounds_per_root_task reached."
            _append_event(ledger, "CIRCUIT_BREAKER_PAUSED", {"reason": msg})
            save_ledger(ledger_path, ledger)
        else:
            msg = "Circuit breaker: max_g4_rounds_per_root_task reached by in-flight G4."
        return False, ledger, 2, msg

    if int(ledger["candidates_in_packet"]) >= int(breakers["max_candidates_per_packet_version"]):
        ledger["state"] = "NEEDS_REPLAN"
        msg = "Circuit breaker: max_candidates_per_packet_version reached."
        _append_event(ledger, "CIRCUIT_BREAKER_REPLAN", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    max_g4_pkt = int(breakers.get("max_g4_rounds_per_packet_version", 3))
    if int(ledger.get("g4_rounds_in_packet", 0)) + g4_in_flight >= max_g4_pkt:
        if g4_in_flight == 0:
            ledger["state"] = "NEEDS_REPLAN"
            msg = "Circuit breaker: max_g4_rounds_per_packet_version reached."
            _append_event(ledger, "CIRCUIT_BREAKER_REPLAN", {"reason": msg})
            save_ledger(ledger_path, ledger)
        else:
            msg = "Circuit breaker: max_g4_rounds_per_packet_version reached by in-flight G4."
        return False, ledger, 2, msg

    if int(ledger["consecutive_failed_candidates"]) >= int(
        breakers["reassess_after_failed_candidates"]
    ):
        ledger["state"] = "NEEDS_REPLAN"
        msg = "Circuit breaker: reassess_after_failed_candidates reached."
        _append_event(ledger, "CIRCUIT_BREAKER_REPLAN", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    current_time = now_utc or _now_utc()
    created_at = datetime.fromisoformat(str(ledger["created_at_utc"]))
    elapsed_sec = (current_time - created_at).total_seconds()
    wall_limit = float(ledger["deadlines_sec"].get("root_task_wall_clock", 3600))
    if elapsed_sec > wall_limit:
        ledger["state"] = "PAUSED_BUDGET"
        msg = f"Root task wall-clock deadline exceeded ({elapsed_sec:.1f}s > {wall_limit:.1f}s)."
        _append_event(ledger, "WALL_CLOCK_EXCEEDED", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    _refresh_budget_warning(ledger)
    spent = float(ledger.get("spent_api_equiv_usd", 0.0))
    reservations = ledger.get("active_reservations", {})
    other_reserved = sum(
        float(v) for k, v in reservations.items() if k != reservation_id
    )
    projected_committed = round(spent + other_reserved + reserve_usd, 8)
    limit = float(ledger["budget_limit_usd"])
    if projected_committed > round(limit, 8):
        ledger["state"] = "PAUSED_BUDGET"
        msg = (
            f"Budget exceeded: committed ${spent + other_reserved:.4f} + "
            f"reserve ${reserve_usd:.4f} > limit ${limit:.4f}."
        )
        _append_event(ledger, "BUDGET_EXCEEDED", {"reason": msg})
        save_ledger(ledger_path, ledger)
        return False, ledger, 2, msg

    if reservation_id and reserve_usd > 0:
        ledger["active_reservations"][reservation_id] = float(reserve_usd)
        _refresh_budget_warning(ledger)

    ledger["state"] = "ACTIVE_PEER_G1"
    _append_event(ledger, "DISPATCH_APPROVED", {"peer_model": peer_model})
    save_ledger(ledger_path, ledger)
    return True, ledger, 0, "Dispatch approved."


def _apply_candidate_failure_breakers(ledger: dict[str, Any]) -> str:
    breakers = ledger["circuit_breakers"]
    max_g4_root = int(breakers.get("max_g4_rounds_per_root_task", 6))
    max_g4_pkt = int(breakers.get("max_g4_rounds_per_packet_version", 3))
    if (
        int(ledger["total_candidates"]) >= int(breakers["max_total_candidates_per_root_task"])
        or int(ledger.get("g4_rounds_total", 0)) >= max_g4_root
    ):
        ledger["state"] = "PAUSED_BUDGET"
    elif (
        int(ledger["consecutive_failed_candidates"])
        >= int(breakers["reassess_after_failed_candidates"])
        or int(ledger["candidates_in_packet"]) >= int(breakers["max_candidates_per_packet_version"])
        or int(ledger.get("g4_rounds_in_packet", 0)) >= max_g4_pkt
    ):
        ledger["state"] = "NEEDS_REPLAN"
    else:
        ledger["state"] = "ACTIVE_PEER_G1"
    return str(ledger["state"])


def _extract_peer_signals(raw_output: str | None) -> list[str]:
    if not raw_output:
        return []
    stripped = raw_output.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict) and isinstance(payload.get("signal"), str):
                sig_val = payload["signal"].strip().upper()
                allowed_sigs = PEER_ESCALATION_SIGNALS | {
                    "CANDIDATE_READY",
                    "INFRA_STARTUP_ERROR",
                }
                if sig_val in allowed_sigs:
                    return [sig_val]
        except json.JSONDecodeError:
            pass

    found: list[str] = []
    for line in raw_output.splitlines():
        l_strip = line.strip()
        if l_strip.startswith("{"):
            try:
                payload = json.loads(l_strip)
                if isinstance(payload, dict) and isinstance(payload.get("signal"), str):
                    sig_val = payload["signal"].strip().upper()
                    if sig_val in (
                        PEER_ESCALATION_SIGNALS | {"CANDIDATE_READY", "INFRA_STARTUP_ERROR"}
                    ):
                        found.append(sig_val)
                        continue
            except json.JSONDecodeError:
                pass
        match = SIGNAL_HEADER_RE.match(line.lstrip())
        if match:
            found.append(match.group(1).upper())
    return found


def _extract_peer_signal(raw_output: str | None, *, has_candidate_sha: bool = False) -> str | None:
    signals = _extract_peer_signals(raw_output)
    if not signals:
        return None
    if has_candidate_sha:
        if "CANDIDATE_READY" in signals:
            return "CANDIDATE_READY"
        for sig in signals:
            if sig in PEER_ESCALATION_SIGNALS:
                return sig
    return signals[0]


def _normalize_owned_and_denied(
    readiness: dict[str, Any] | None,
    owned_paths: list[str] | None,
    denied_paths: list[str] | None,
) -> tuple[list[str], list[str]]:
    owned: list[str] = []
    if owned_paths is not None:
        owned = [_normalize_rel_path(p) for p in owned_paths]
    elif readiness and isinstance(readiness.get("owned_paths"), list):
        for item in readiness["owned_paths"]:
            if isinstance(item, dict) and item.get("path"):
                owned.append(_normalize_rel_path(str(item["path"])))
            elif isinstance(item, str):
                owned.append(_normalize_rel_path(item))

    denied: list[str] = []
    if denied_paths is not None:
        denied = [_normalize_rel_path(p) for p in denied_paths]
    elif readiness and isinstance(readiness.get("denied_paths"), list):
        denied = [_normalize_rel_path(str(p)) for p in readiness["denied_paths"]]
    return owned, denied


def _check_write_scope(
    changed_files: list[str],
    owned_paths: list[str],
    denied_paths: list[str],
) -> list[str]:
    violations: list[str] = []
    if not changed_files:
        violations.append("Candidate diff is empty (no files changed vs base_sha).")
        return violations
    owned_lower = {_normalize_rel_path(o).lower() for o in owned_paths}
    for rel in changed_files:
        norm = _normalize_rel_path(rel)
        path_err = _validate_rel_path(norm, is_owned=True)
        if path_err:
            violations.append(path_err)
            continue
        if any(_paths_overlap(norm, d) for d in denied_paths):
            violations.append(f"Changed file {norm!r} matches Denied Paths.")
            continue
        if norm.lower() not in owned_lower:
            violations.append(f"Changed file {norm!r} is outside Owned Paths.")
    return violations


def record_peer_outcome(
    ledger_path: Path,
    *,
    signal: str | None = None,
    raw_output: str | None = None,
    status: str | None = None,
    exit_code: int = 0,
    total_tokens: int | None = None,
    error_message: str = "",
    candidate_sha: str | None = None,
    readiness_path: Path | None = None,
    packet_path: Path | None = None,
    base_sha: str | None = None,
    owned_paths: list[str] | None = None,
    denied_paths: list[str] | None = None,
    repo_root: Path | None = None,
    signal_payload: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Classify and record Peer outcome, escalation, infra startup error, or candidate SHA."""
    root = (repo_root or REPO_ROOT).resolve()
    with _ledger_lock(ledger_path):
        return _record_peer_outcome_locked(
            ledger_path,
            signal=signal,
            raw_output=raw_output,
            status=status,
            exit_code=exit_code,
            total_tokens=total_tokens,
            error_message=error_message,
            candidate_sha=candidate_sha,
            readiness_path=readiness_path,
            packet_path=packet_path,
            base_sha=base_sha,
            owned_paths=owned_paths,
            denied_paths=denied_paths,
            root=root,
            signal_payload=signal_payload,
        )


def _record_peer_outcome_locked(
    ledger_path: Path,
    *,
    signal: str | None,
    raw_output: str | None,
    status: str | None,
    exit_code: int,
    total_tokens: int | None,
    error_message: str,
    candidate_sha: str | None,
    readiness_path: Path | None,
    packet_path: Path | None,
    base_sha: str | None,
    owned_paths: list[str] | None,
    denied_paths: list[str] | None,
    root: Path,
    signal_payload: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], int]:
    ledger = load_ledger(ledger_path)
    if ledger["state"] != "ACTIVE_PEER_G1":
        raise ValueError(
            f"Cannot record Peer outcome while ledger is in state {ledger['state']!r} "
            f"(expected 'ACTIVE_PEER_G1')."
        )

    norm_status = str(status or "").strip().upper()
    explicit_sig = signal.strip().upper() if signal is not None else ""
    is_error_exit = norm_status == "ERROR" or (exit_code is not None and exit_code != 0)

    def _record_peer_failure(
        extra_detail: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any], int]:
        ledger["transient_retries_in_stage"] = 0
        ledger["candidate_sha"] = None
        ledger["total_candidates"] = int(ledger["total_candidates"]) + 1
        ledger["candidates_in_packet"] = int(ledger["candidates_in_packet"]) + 1
        ledger["consecutive_failed_candidates"] = (
            int(ledger["consecutive_failed_candidates"]) + 1
        )
        new_state = _apply_candidate_failure_breakers(ledger)
        payload = {
            "candidate_sha": candidate_sha,
            "exit_code": exit_code,
            "total_tokens": total_tokens,
            "error_message": error_message or (raw_output or ""),
        }
        if extra_detail:
            payload.update(extra_detail)
        _append_event(ledger, "PEER_ROUND_FAILED", payload)
        save_ledger(ledger_path, ledger)
        return "PEER_ROUND_FAILED", ledger, (2 if new_state == "PAUSED_BUDGET" else 1)

    # Evaluate 0-token startup failure BEFORE scanning raw_output prose/stderr
    is_infra_zero_tok = (
        explicit_sig in {"", "INFRA_STARTUP_ERROR"}
        and is_error_exit
        and total_tokens == 0
    )
    if explicit_sig == "INFRA_STARTUP_ERROR" or is_infra_zero_tok:
        max_retries = int(
            ledger["circuit_breakers"]["max_transient_retries_per_provider_pass"]
        )
        retries = int(ledger.get("transient_retries_in_stage", 0))
        detail = {
            "classification": "INFRA_STARTUP_ERROR",
            "exit_code": exit_code,
            "total_tokens": total_tokens,
            "error_message": error_message or (raw_output or ""),
        }
        if retries < max_retries:
            ledger["transient_retries_in_stage"] = retries + 1
            ledger["state"] = "ACTIVE_PEER_G1"
            detail["retry_allowed"] = True
            _append_event(ledger, "INFRA_STARTUP_ERROR", detail)
            save_ledger(ledger_path, ledger)
            return "INFRA_STARTUP_ERROR", ledger, 0

        ledger["state"] = "BLOCKED_INFRA"
        detail["retry_allowed"] = False
        _append_event(ledger, "BLOCKED_INFRA", detail)
        save_ledger(ledger_path, ledger)
        return "INFRA_STARTUP_ERROR", ledger, 2

    # Non-zero-token or unknown-token CLI crashes without explicit signal fail immediately
    if signal is None and is_error_exit:
        return _record_peer_failure()

    has_cand = candidate_sha is not None
    detected_signal = explicit_sig or (
        _extract_peer_signal(raw_output, has_candidate_sha=has_cand) or ""
    )
    if not detected_signal and has_cand:
        detected_signal = "CANDIDATE_READY"

    if detected_signal in PEER_ESCALATION_SIGNALS:
        ledger["transient_retries_in_stage"] = 0
        ledger["state"] = "NEEDS_REPLAN"
        _append_event(
            ledger,
            detected_signal,
            {"payload": signal_payload or {"raw": raw_output or error_message}},
        )
        save_ledger(ledger_path, ledger)
        return detected_signal, ledger, 0

    if detected_signal == "CANDIDATE_READY" or candidate_sha is not None:
        if candidate_sha is None or not is_hex_sha40(candidate_sha) or is_error_exit:
            return _record_peer_failure({"reason": "Missing or invalid candidate_sha."})

        if readiness_path is None or not readiness_path.is_file():
            raise ValueError("CANDIDATE_READY requires a valid readiness.json path.")
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        if not isinstance(readiness, dict):
            raise ValueError("readiness.json must be a JSON object.")
        if readiness.get("status") != "VALID":
            raise ValueError(f"readiness status is {readiness.get('status')!r}, expected 'VALID'.")
        if readiness.get("root_task_id") != ledger["root_task_id"]:
            raise ValueError(
                f"readiness root_task_id ({readiness.get('root_task_id')!r}) != "
                f"ledger root_task_id ({ledger['root_task_id']!r})."
            )
        if readiness.get("packet_version") != ledger["packet_version"]:
            raise ValueError(
                f"readiness packet_version ({readiness.get('packet_version')!r}) != "
                f"ledger packet_version ({ledger['packet_version']!r})."
            )

        r_base = str(readiness.get("base_sha", ""))
        if not is_hex_sha40(r_base):
            raise ValueError(f"Invalid base_sha in readiness.json: {r_base!r}.")
        if base_sha is not None and base_sha != r_base:
            raise ValueError(
                f"base_sha ({base_sha!r}) diverges from readiness base_sha ({r_base!r})."
            )
        if ledger.get("base_sha") and ledger["base_sha"] != r_base:
            raise ValueError(
                f"ledger base_sha ({ledger['base_sha']!r}) != readiness base_sha ({r_base!r})."
            )

        effective_pkt = packet_path or (
            Path(str(readiness["packet_path"])) if readiness.get("packet_path") else None
        )
        if effective_pkt is None or not effective_pkt.is_file():
            raise ValueError(f"Packet file missing for readiness verification: {effective_pkt}.")
        if _sha256_bytes(effective_pkt.read_bytes()) != readiness.get("packet_sha256"):
            ledger["state"] = "BLOCKED_BRIEF"
            _append_event(
                ledger,
                "CANDIDATE_BLOCKED_STALE_PACKET",
                {"packet_path": str(effective_pkt)},
            )
            save_ledger(ledger_path, ledger)
            return "BLOCKED_BRIEF", ledger, 2

        git_top = resolve_git_toplevel(root, timeout_sec=30.0)
        cand_valid = candidate_sha != r_base
        if cand_valid:
            cat = run_managed_stage_cmd(
                "peer_round",
                ["git", "-C", str(git_top), "cat-file", "-t", candidate_sha],
                ledger=ledger,
                timeout_override=30.0,
            )
            cand_valid = cat.returncode == 0 and cat.stdout.strip() == "commit"

        if not cand_valid:
            return _record_peer_failure({"reason": "Unreachable or non-distinct candidate_sha."})

        ledger["transient_retries_in_stage"] = 0
        ledger["total_candidates"] = int(ledger["total_candidates"]) + 1
        ledger["candidates_in_packet"] = int(ledger["candidates_in_packet"]) + 1

        diff_cmd = [
            "git",
            "-C",
            str(git_top),
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-renames",
            "--name-only",
            f"{r_base}..{candidate_sha}",
        ]
        diff_proc = run_managed_stage_cmd(
            "peer_round",
            diff_cmd,
            ledger=ledger,
            timeout_override=30.0,
        )
        if diff_proc.returncode != 0:
            raise RuntimeError(f"git diff failed: {diff_proc.stderr.strip()}")
        changed_files = [ln.strip() for ln in diff_proc.stdout.splitlines() if ln.strip()]

        owned_list, denied_list = _normalize_owned_and_denied(readiness, owned_paths, denied_paths)
        scope_errors = _check_write_scope(changed_files, owned_list, denied_list)
        if scope_errors:
            ledger["candidate_sha"] = None
            ledger["consecutive_failed_candidates"] = (
                int(ledger["consecutive_failed_candidates"]) + 1
            )
            new_state = _apply_candidate_failure_breakers(ledger)
            _append_event(
                ledger,
                "SCOPE_VIOLATION",
                {
                    "rejected_candidate_sha": candidate_sha,
                    "changed_files": changed_files,
                    "violations": scope_errors,
                },
            )
            save_ledger(ledger_path, ledger)
            return "SCOPE_VIOLATION", ledger, (2 if new_state == "PAUSED_BUDGET" else 1)

        ledger["candidate_sha"] = candidate_sha
        ledger["state"] = "VERIFY_FROZEN_G3"
        _append_event(
            ledger,
            "CANDIDATE_READY",
            {"candidate_sha": candidate_sha, "changed_files": changed_files},
        )
        save_ledger(ledger_path, ledger)
        return "CANDIDATE_READY", ledger, 0

    if is_error_exit:
        return _record_peer_failure()

    raise ValueError(
        f"Unable to classify Peer outcome (signal={signal!r}, status={status!r}, "
        f"exit_code={exit_code}, total_tokens={total_tokens})."
    )


def record_gate_outcome(
    ledger_path: Path,
    *,
    gate: str,
    outcome: str,
    candidate_sha: str | None = None,
    details: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Record G3/G4/G5 gate verdict strictly from its matching active state."""
    norm_gate = gate.strip().upper()
    norm_outcome = outcome.strip().upper()
    expected_state = {
        "G3": "VERIFY_FROZEN_G3",
        "G4": "REVIEW_FROZEN_G4",
        "G5": "READY_FOR_ACCEPTANCE",
    }.get(norm_gate)
    if expected_state is None:
        raise ValueError(f"Unsupported gate {gate!r}; expected G3, G4, or G5.")

    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        if (
            norm_gate == "G4"
            and norm_outcome == "PASSED"
            and ledger["state"] == "READY_FOR_ACCEPTANCE"
            and is_hex_sha40(candidate_sha)
            and ledger.get("candidate_sha") == candidate_sha
        ):
            return ledger, 0
        if (
            norm_gate == "G5"
            and norm_outcome == "VERIFIED"
            and ledger["state"] == "VERIFIED"
            and is_hex_sha40(candidate_sha)
            and ledger.get("candidate_sha") == candidate_sha
        ):
            return ledger, 0
        if ledger["state"] != expected_state:
            return ledger, 2

        expected_sha = ledger.get("candidate_sha")
        if (
            not is_hex_sha40(candidate_sha)
            or not is_hex_sha40(expected_sha)
            or candidate_sha != expected_sha
        ):
            return ledger, 2

        ledger["transient_retries_in_stage"] = 0

        if norm_gate in {"G3", "G4"} and norm_outcome in {"FAILED", "CHANGES_REQUESTED"}:
            if norm_gate == "G4":
                ledger["g4_rounds_total"] = int(ledger.get("g4_rounds_total", 0)) + 1
                ledger["g4_rounds_in_packet"] = int(ledger.get("g4_rounds_in_packet", 0)) + 1
                ledger["last_g4_candidate_sha"] = candidate_sha
                ledger["last_g4_status"] = norm_outcome
            ledger["candidate_sha"] = None
            ledger["consecutive_failed_candidates"] = (
                int(ledger["consecutive_failed_candidates"]) + 1
            )
            new_state = _apply_candidate_failure_breakers(ledger)
            _append_event(
                ledger,
                f"{norm_gate}_{norm_outcome}",
                {"candidate_sha": candidate_sha, "details": details or {}},
            )
            save_ledger(ledger_path, ledger)
            return ledger, (2 if new_state == "PAUSED_BUDGET" else 1)

        if norm_gate == "G3":
            if norm_outcome in {"PASSED", "VERIFIED"}:
                ledger["state"] = "REVIEW_FROZEN_G4"
                _append_event(ledger, "G3_PASSED", {"candidate_sha": candidate_sha})
                save_ledger(ledger_path, ledger)
                return ledger, 0
            if norm_outcome == "BLOCKED":
                ledger["state"] = "BLOCKED"
                _append_event(ledger, "G3_BLOCKED", {"details": details or {}})
                save_ledger(ledger_path, ledger)
                return ledger, 2

        if norm_gate == "G4":
            if norm_outcome in {"BLOCKED_AUTH", "BLOCKED_INFRA", "BLOCKED"}:
                ledger["last_g4_candidate_sha"] = candidate_sha
                ledger["last_g4_status"] = norm_outcome
                ledger["state"] = norm_outcome
                _append_event(ledger, f"G4_{norm_outcome}", {"details": details or {}})
                save_ledger(ledger_path, ledger)
                return ledger, 2
            if norm_outcome == "PASSED":
                ledger["g4_rounds_total"] = int(ledger.get("g4_rounds_total", 0)) + 1
                ledger["g4_rounds_in_packet"] = int(ledger.get("g4_rounds_in_packet", 0)) + 1
                ledger["last_g4_candidate_sha"] = candidate_sha
                ledger["last_g4_status"] = "PASSED"
                ledger["state"] = "READY_FOR_ACCEPTANCE"
                _append_event(ledger, "G4_PASSED", {"candidate_sha": candidate_sha})
                save_ledger(ledger_path, ledger)
                return ledger, 0

        if norm_gate == "G5":
            if norm_outcome == "VERIFIED":
                ledger["consecutive_failed_candidates"] = 0
                ledger["state"] = "VERIFIED"
                _append_event(ledger, "G5_VERIFIED", {"candidate_sha": candidate_sha})
                save_ledger(ledger_path, ledger)
                return ledger, 0
            if norm_outcome == "FAILED":
                ledger["candidate_sha"] = None
                ledger["consecutive_failed_candidates"] = (
                    int(ledger["consecutive_failed_candidates"]) + 1
                )
                new_state = _apply_candidate_failure_breakers(ledger)
                _append_event(ledger, "G5_FAILED", {"details": details or {}})
                save_ledger(ledger_path, ledger)
                return ledger, (2 if new_state == "PAUSED_BUDGET" else 1)
            if norm_outcome == "BLOCKED":
                ledger["state"] = "BLOCKED"
                _append_event(ledger, "G5_BLOCKED", {"details": details or {}})
                save_ledger(ledger_path, ledger)
                return ledger, 2

    raise ValueError(f"Unsupported gate/outcome combination: gate={gate!r}, outcome={outcome!r}")


def record_replan(
    ledger_path: Path,
    readiness_path: Path,
    *,
    packet_path: Path | None = None,
    human_approved: bool = False,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> tuple[dict[str, Any], int, str]:
    """Advance ledger to a bumped ``packet_version`` after validated ``readiness.json``."""
    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        current_state = ledger["state"]
        if current_state == "VERIFIED":
            return ledger, 2, "Refusing replan: ledger is already VERIFIED."
        if not human_approved and current_state != "NEEDS_REPLAN":
            return (
                ledger,
                2,
                f"Auto-replan requires state 'NEEDS_REPLAN', got {current_state!r}.",
            )

        if not readiness_path.is_file():
            return ledger, 2, f"Missing readiness.json at {readiness_path}."

        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        if not isinstance(readiness, dict):
            return ledger, 2, "readiness.json must be a JSON object."
        if readiness.get("root_task_id") != ledger["root_task_id"]:
            raise ValueError(
                f"Root task mismatch in readiness.json: {readiness.get('root_task_id')!r} != "
                f"{ledger['root_task_id']!r}."
            )
        if readiness.get("status") != "VALID":
            return (
                ledger,
                2,
                f"Cannot replan from non-VALID readiness ({readiness.get('status')!r}).",
            )

        new_version = readiness.get("packet_version")
        if not is_strict_int(new_version) or new_version <= int(ledger["packet_version"]):
            return (
                ledger,
                2,
                f"Replan requires bumped packet_version > {ledger['packet_version']} "
                f"(got {new_version!r}).",
            )

        effective_pkt = packet_path or (
            Path(str(readiness["packet_path"])) if readiness.get("packet_path") else None
        )
        if effective_pkt is None or not effective_pkt.is_file():
            return ledger, 2, f"Packet missing at {effective_pkt}."
        if _sha256_bytes(effective_pkt.read_bytes()) != readiness.get("packet_sha256"):
            return ledger, 2, "Packet SHA-256 does not match readiness.json."

        new_base_sha = str(readiness.get("base_sha", ""))
        if not is_hex_sha40(new_base_sha):
            return ledger, 2, f"Invalid base_sha in readiness.json: {new_base_sha!r}."

        active_root = (repo_root or REPO_ROOT).resolve()
        if repo_root is None:
            try:
                active_root = resolve_git_toplevel(effective_pkt.parent, timeout_sec=10.0)
            except (ValueError, TimeoutError):
                active_root = REPO_ROOT.resolve()

        effective_policy = policy_path or (
            Path(str(readiness["policy_path"])) if readiness.get("policy_path") else None
        )
        _, current_policy_sha, policy_err = _load_policy_state(active_root, effective_policy)
        if policy_err:
            return ledger, 2, policy_err
        if readiness.get("policy_sha256") != current_policy_sha:
            return (
                ledger,
                2,
                f"Stale readiness.json: policy_sha256 mismatch "
                f"({readiness.get('policy_sha256')!r} != {current_policy_sha!r}).",
            )

        try:
            git_top = resolve_git_toplevel(active_root, timeout_sec=30.0)
            rev = run_managed_stage_cmd(
                "brief_generation",
                ["git", "-C", str(git_top), "rev-parse", "--verify", f"{new_base_sha}^{{commit}}"],
                ledger=ledger,
                timeout_override=30.0,
            )
            if rev.returncode != 0:
                return ledger, 2, f"Base SHA {new_base_sha} does not resolve in git."
        except (ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return ledger, 2, f"Unable to verify base_sha {new_base_sha} in git: {exc}"

        new_budget = _require_finite_number(
            readiness.get("budget_usd"),
            "budget_usd",
            positive=True,
        )

        max_auto = int(ledger["circuit_breakers"]["max_auto_replans_per_root_task"])
        if int(ledger["auto_replans_used"]) >= max_auto and not human_approved:
            ledger["state"] = "PAUSED_BUDGET"
            msg = "Auto-replan limit reached; human approval required."
            _append_event(ledger, "AUTO_REPLAN_LIMIT_PAUSED", {"reason": msg})
            save_ledger(ledger_path, ledger)
            return ledger, 2, msg

        if not human_approved:
            ledger["auto_replans_used"] = int(ledger["auto_replans_used"]) + 1
        else:
            ledger["budget_limit_usd"] = new_budget
        ledger["packet_version"] = new_version
        ledger["task_id"] = str(readiness.get("task_id", ledger["task_id"]))
        ledger["base_sha"] = new_base_sha
        ledger["candidate_sha"] = None
        ledger["candidates_in_packet"] = 0
        ledger["g4_rounds_in_packet"] = 0
        ledger["consecutive_failed_candidates"] = 0
        ledger["transient_retries_in_stage"] = 0
        ledger["state"] = "ACTIVE_PEER_G1"
        _append_event(
            ledger,
            "REPLAN_APPLIED",
            {"packet_version": new_version, "human_approved": human_approved},
        )
        save_ledger(ledger_path, ledger)
        return ledger, 0, f"Replan applied (packet_version={new_version})."


def _resolve_pricing(
    model: str,
    pricing: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    table = pricing or DEFAULT_PRICING_PER_1M
    norm = model.strip().lower()
    if norm in table:
        return table[norm]
    for prefix, rates in table.items():
        if norm == prefix.lower() or norm.startswith(prefix.lower()):
            return rates
    raise ValueError(f"Unknown model pricing for {model!r}.")


def compute_usage_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Compute USD API-equivalent cost from token counts and pricing table."""
    rates = _resolve_pricing(model, pricing=pricing)
    in_cost = (input_tokens * float(rates["input"])) / 1_000_000.0
    out_cost = ((output_tokens + thinking_tokens) * float(rates["output"])) / 1_000_000.0
    cache_cost = (cache_read_tokens * float(rates["cache_read"])) / 1_000_000.0
    return in_cost + out_cost + cache_cost


def record_usage(
    ledger_path: Path,
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    conversation_id: str | None = None,
    counter_mode: str = "cumulative",
    pricing: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, Any], float, int]:
    """Record token usage with high-water deduplication for cumulative sessions."""
    for name, val in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("thinking_tokens", thinking_tokens),
        ("cache_read_tokens", cache_read_tokens),
    ):
        if not is_strict_int(val) or val < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {val!r}.")
    if counter_mode not in {"cumulative", "delta"}:
        raise ValueError(f"counter_mode must be 'cumulative' or 'delta', got {counter_mode!r}.")

    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        usage_map = ledger.setdefault("usage_by_conversation", {})
        conv_key = (conversation_id or "").strip()

        if counter_mode == "cumulative" and conv_key:
            prev = usage_map.get(
                conv_key,
                {
                    "model": model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "cache_read_tokens": 0,
                    "billed_usd": 0.0,
                },
            )
            d_in = max(0, input_tokens - int(prev["input_tokens"]))
            d_out = max(0, output_tokens - int(prev["output_tokens"]))
            d_think = max(0, thinking_tokens - int(prev["thinking_tokens"]))
            d_cache = max(0, cache_read_tokens - int(prev["cache_read_tokens"]))

            delta_usd = compute_usage_cost_usd(
                model,
                input_tokens=d_in,
                output_tokens=d_out,
                thinking_tokens=d_think,
                cache_read_tokens=d_cache,
                pricing=pricing,
            )
            usage_map[conv_key] = {
                "model": model,
                "input_tokens": max(int(prev["input_tokens"]), input_tokens),
                "output_tokens": max(int(prev["output_tokens"]), output_tokens),
                "thinking_tokens": max(int(prev["thinking_tokens"]), thinking_tokens),
                "cache_read_tokens": max(int(prev["cache_read_tokens"]), cache_read_tokens),
                "billed_usd": round(float(prev["billed_usd"]) + delta_usd, 8),
            }
        else:
            delta_usd = compute_usage_cost_usd(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                cache_read_tokens=cache_read_tokens,
                pricing=pricing,
            )
            if conv_key:
                prev = usage_map.get(
                    conv_key,
                    {
                        "model": model,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "thinking_tokens": 0,
                        "cache_read_tokens": 0,
                        "billed_usd": 0.0,
                    },
                )
                usage_map[conv_key] = {
                    "model": model,
                    "input_tokens": int(prev["input_tokens"]) + input_tokens,
                    "output_tokens": int(prev["output_tokens"]) + output_tokens,
                    "thinking_tokens": int(prev["thinking_tokens"]) + thinking_tokens,
                    "cache_read_tokens": int(prev["cache_read_tokens"]) + cache_read_tokens,
                    "billed_usd": round(float(prev["billed_usd"]) + delta_usd, 8),
                }

        ledger["spent_api_equiv_usd"] = round(float(ledger["spent_api_equiv_usd"]) + delta_usd, 8)
        committed = _refresh_budget_warning(ledger)
        limit = float(ledger["budget_limit_usd"])
        rc = 0
        if round(committed, 8) > round(limit, 8):
            if ledger["state"] != "VERIFIED":
                ledger["state"] = "PAUSED_BUDGET"
                rc = 2
            _append_event(
                ledger,
                "BUDGET_EXCEEDED_USAGE",
                {"committed_usd": committed, "limit_usd": limit},
            )
        save_ledger(ledger_path, ledger)
        return ledger, delta_usd, rc


def reserve_budget(
    ledger_path: Path,
    reservation_id: str,
    amount_usd: float,
) -> tuple[bool, dict[str, Any], int]:
    """Atomically reserve USD against remaining budget; pause on >100% exhaustion."""
    if not reservation_id.strip():
        raise ValueError("reservation_id must be non-empty.")
    valid_amt = _require_finite_number(amount_usd, "amount_usd")

    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        if ledger["state"] == "VERIFIED":
            return False, ledger, 2
        reservations = ledger.setdefault("active_reservations", {})
        other_reserved = sum(
            float(v) for k, v in reservations.items() if k != reservation_id
        )
        spent = float(ledger.get("spent_api_equiv_usd", 0.0))
        limit = float(ledger.get("budget_limit_usd", 0.0))
        projected = round(spent + other_reserved + valid_amt, 8)

        if projected > round(limit, 8):
            ledger["state"] = "PAUSED_BUDGET"
            _append_event(
                ledger,
                "BUDGET_RESERVATION_REJECTED",
                {
                    "reservation_id": reservation_id,
                    "projected_usd": projected,
                    "limit_usd": limit,
                },
            )
            save_ledger(ledger_path, ledger)
            return False, ledger, 2

        reservations[reservation_id] = valid_amt
        _refresh_budget_warning(ledger)
        _append_event(
            ledger,
            "BUDGET_RESERVED",
            {"reservation_id": reservation_id, "amount_usd": valid_amt},
        )
        save_ledger(ledger_path, ledger)
        return True, ledger, 0


def release_reservation(ledger_path: Path, reservation_id: str) -> dict[str, Any]:
    """Release an active budget reservation if present."""
    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        reservations = ledger.setdefault("active_reservations", {})
        if reservation_id in reservations:
            reservations.pop(reservation_id)
            _append_event(ledger, "BUDGET_RELEASED", {"reservation_id": reservation_id})
            save_ledger(ledger_path, ledger)
        return ledger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable S-L-P task state & budget runner.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize or load ledger by root_task_id.")
    p_init.add_argument("--ledger", type=Path, required=True)
    p_init.add_argument("--root-task-id", required=True)
    p_init.add_argument("--task-id", default=None)
    p_init.add_argument("--packet-version", type=int, default=1)
    p_init.add_argument("--base-sha", default="")
    p_init.add_argument("--budget-usd", type=float, default=6.0)
    p_init.add_argument("--json", action="store_true")

    p_disp = sub.add_parser("check-dispatch", help="Validate G0.5 + model + breakers + budget.")
    p_disp.add_argument("--ledger", type=Path, required=True)
    p_disp.add_argument("--readiness", type=Path, required=True)
    p_disp.add_argument("--packet", type=Path, required=True)
    p_disp.add_argument("--peer-model", required=True)
    p_disp.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_disp.add_argument("--policy", type=Path, default=None)
    p_disp.add_argument("--reserve-usd", type=float, default=0.0)
    p_disp.add_argument("--reservation-id", default=None)
    p_disp.add_argument("--json", action="store_true")

    p_peer = sub.add_parser("record-peer", help="Record Peer signal, infra error, or SHA.")
    p_peer.add_argument("--ledger", type=Path, required=True)
    p_peer.add_argument("--signal", default=None)
    p_peer.add_argument("--raw-output", default=None)
    p_peer.add_argument("--status", default=None)
    p_peer.add_argument("--exit-code", type=int, default=0)
    p_peer.add_argument("--total-tokens", type=int, default=None)
    p_peer.add_argument("--error-message", default="")
    p_peer.add_argument("--candidate-sha", default=None)
    p_peer.add_argument("--readiness", type=Path, default=None)
    p_peer.add_argument("--packet", type=Path, default=None)
    p_peer.add_argument("--base-sha", default=None)
    p_peer.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_peer.add_argument("--json", action="store_true")

    p_gate = sub.add_parser("record-gate", help="Record G3/G4/G5 gate outcome.")
    p_gate.add_argument("--ledger", type=Path, required=True)
    p_gate.add_argument("--gate", required=True)
    p_gate.add_argument("--outcome", required=True)
    p_gate.add_argument("--candidate-sha", default=None)
    p_gate.add_argument("--json", action="store_true")

    p_rep = sub.add_parser("record-replan", help="Apply bumped packet_version replan.")
    p_rep.add_argument("--ledger", type=Path, required=True)
    p_rep.add_argument("--readiness", type=Path, required=True)
    p_rep.add_argument("--packet", type=Path, default=None)
    p_rep.add_argument("--human-approved", action="store_true")
    p_rep.add_argument("--repo-root", type=Path, default=None)
    p_rep.add_argument("--policy", type=Path, default=None)
    p_rep.add_argument("--json", action="store_true")

    p_use = sub.add_parser("record-usage", help="Record model token usage with deduplication.")
    p_use.add_argument("--ledger", type=Path, required=True)
    p_use.add_argument("--model", required=True)
    p_use.add_argument("--input-tokens", type=int, default=0)
    p_use.add_argument("--output-tokens", type=int, default=0)
    p_use.add_argument("--thinking-tokens", type=int, default=0)
    p_use.add_argument("--cache-read-tokens", type=int, default=0)
    p_use.add_argument("--conversation-id", default=None)
    p_use.add_argument("--counter-mode", choices=["cumulative", "delta"], default="cumulative")
    p_use.add_argument("--json", action="store_true")

    p_res = sub.add_parser("reserve-budget", help="Reserve USD against task budget.")
    p_res.add_argument("--ledger", type=Path, required=True)
    p_res.add_argument("--reservation-id", required=True)
    p_res.add_argument("--amount-usd", type=float, required=True)
    p_res.add_argument("--json", action="store_true")

    p_rel = sub.add_parser("release-reservation", help="Release an active budget reservation.")
    p_rel.add_argument("--ledger", type=Path, required=True)
    p_rel.add_argument("--reservation-id", required=True)
    p_rel.add_argument("--json", action="store_true")

    p_stat = sub.add_parser("status", help="Inspect current ledger state.")
    p_stat.add_argument("--ledger", type=Path, required=True)
    p_stat.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch_cli(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[task_runner] ERROR: {exc}", file=sys.stderr)
        return 2


def _dispatch_cli(args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd == "init":
        ledger = init_or_load_ledger(
            args.ledger,
            root_task_id=args.root_task_id,
            task_id=args.task_id,
            packet_version=args.packet_version,
            base_sha=args.base_sha,
            budget_usd=args.budget_usd,
        )
        if args.json:
            print(json.dumps(ledger, indent=2, allow_nan=False))
        else:
            print(f"[task_runner] state={ledger['state']} root_task_id={ledger['root_task_id']}")
        return 0

    if cmd == "check-dispatch":
        ok, ledger, rc, msg = check_dispatch(
            args.ledger,
            args.readiness,
            args.packet,
            args.peer_model,
            repo_root=args.repo_root,
            policy_path=args.policy,
            reserve_usd=args.reserve_usd,
            reservation_id=args.reservation_id,
        )
        if args.json:
            print(
                json.dumps(
                    {"allowed": ok, "message": msg, "ledger": ledger},
                    indent=2,
                    allow_nan=False,
                )
            )
        else:
            print(f"[task_runner] allowed={ok} state={ledger['state']} msg={msg}")
        return rc

    if cmd == "record-peer":
        outcome, ledger, rc = record_peer_outcome(
            args.ledger,
            signal=args.signal,
            raw_output=args.raw_output,
            status=args.status,
            exit_code=args.exit_code,
            total_tokens=args.total_tokens,
            error_message=args.error_message,
            candidate_sha=args.candidate_sha,
            readiness_path=args.readiness,
            packet_path=args.packet,
            base_sha=args.base_sha,
            repo_root=args.repo_root,
        )
        if args.json:
            print(json.dumps({"outcome": outcome, "ledger": ledger}, indent=2, allow_nan=False))
        else:
            print(f"[task_runner] outcome={outcome} state={ledger['state']}")
        return rc

    if cmd == "record-gate":
        ledger, rc = record_gate_outcome(
            args.ledger,
            gate=args.gate,
            outcome=args.outcome,
            candidate_sha=args.candidate_sha,
        )
        if args.json:
            print(json.dumps(ledger, indent=2))
        else:
            print(f"[task_runner] state={ledger['state']}")
        return rc

    if cmd == "record-replan":
        ledger, rc, msg = record_replan(
            args.ledger,
            args.readiness,
            packet_path=args.packet,
            human_approved=args.human_approved,
            repo_root=args.repo_root,
            policy_path=args.policy,
        )
        if args.json:
            print(json.dumps({"message": msg, "ledger": ledger}, indent=2))
        else:
            print(f"[task_runner] state={ledger['state']} msg={msg}")
        return rc

    if cmd == "record-usage":
        ledger, delta_usd, rc = record_usage(
            args.ledger,
            model=args.model,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            thinking_tokens=args.thinking_tokens,
            cache_read_tokens=args.cache_read_tokens,
            conversation_id=args.conversation_id,
            counter_mode=args.counter_mode,
        )
        if args.json:
            print(json.dumps({"delta_usd": delta_usd, "ledger": ledger}, indent=2))
        else:
            print(
                f"[task_runner] delta_usd={delta_usd:.6f} "
                f"spent={ledger['spent_api_equiv_usd']:.6f} state={ledger['state']}"
            )
        return rc

    if cmd == "reserve-budget":
        ok, ledger, rc = reserve_budget(args.ledger, args.reservation_id, args.amount_usd)
        if args.json:
            print(json.dumps({"reserved": ok, "ledger": ledger}, indent=2))
        else:
            print(f"[task_runner] reserved={ok} state={ledger['state']}")
        return rc

    if cmd == "release-reservation":
        ledger = release_reservation(args.ledger, args.reservation_id)
        if args.json:
            print(json.dumps(ledger, indent=2))
        else:
            print(f"[task_runner] state={ledger['state']}")
        return 0

    if cmd == "status":
        ledger = load_ledger(args.ledger)
        if args.json:
            print(json.dumps(ledger, indent=2))
        else:
            print(f"[task_runner] state={ledger['state']} root_task_id={ledger['root_task_id']}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
