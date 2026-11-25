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
    "reviewer_pass_total": 900,
    "reviewer_pass_idle": 270,
    "reviewer_agy_pass_idle": 630,
    "root_task_wall_clock": 5400,
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

ZERO_OK_CIRCUIT_BREAKERS: frozenset[str] = frozenset(
    {
        "max_transient_retries_per_provider_pass",
        "max_auto_replans_per_root_task",
    }
)


def _load_policy_int_map(
    defaults: dict[str, int],
    *,
    section_getter: Any,
    zero_ok_keys: frozenset[str] = frozenset(),
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, int]:
    merged = dict(defaults)
    policy_file = (
        policy_path.resolve()
        if policy_path is not None
        else ((repo_root or REPO_ROOT).resolve() / "harness" / "model-policy.json")
    )
    if not policy_file.is_file():
        return merged
    try:
        data = json.loads(policy_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return merged
    raw_sec = section_getter(data) if isinstance(data, dict) else None
    if not isinstance(raw_sec, dict):
        return merged
    for key in defaults:
        val = raw_sec.get(key)
        min_val = 0 if key in zero_ok_keys else 1
        if is_strict_int(val) and val >= min_val:
            merged[key] = int(val)
    return merged


def load_circuit_breakers(
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, int]:
    """Load integer circuit-breaker caps from model-policy.json with safe defaults."""
    return _load_policy_int_map(
        DEFAULT_CIRCUIT_BREAKERS,
        section_getter=lambda d: d.get("circuit_breakers"),
        zero_ok_keys=ZERO_OK_CIRCUIT_BREAKERS,
        repo_root=repo_root,
        policy_path=policy_path,
    )


def load_deadlines_sec(
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, int]:
    """Load integer deadline settings from model-policy.json with safe defaults."""
    return _load_policy_int_map(
        DEFAULT_DEADLINES_SEC,
        section_getter=lambda d: (
            d["circuit_breakers"].get("deadlines_sec")
            if isinstance(d.get("circuit_breakers"), dict)
            else None
        ),
        repo_root=repo_root,
        policy_path=policy_path,
    )


def _read_optional_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _norm_attempt_dir(path_val: Path | str) -> str:
    return Path(path_val).resolve().as_posix()


def collect_g4_attempt_union(
    attempts_dirs: list[Path],
    tracker: dict[str, Any] | None = None,
    *,
    candidate_sha: str | None = None,
) -> dict[str, Any]:
    """Compute exact union of canonical attempt dirs across filesystem and ``g4-tracker.json``."""
    seen_roots: set[Path] = set()
    round_dirs: set[str] = set()
    sha_dirs: set[str] = set()
    sha_statuses: dict[str, str] = {}
    discovered_entries: dict[str, dict[str, Any]] = {}
    completed_statuses = {"PASSED", "CHANGES_REQUESTED", "BLOCKED"}

    for raw_dir in attempts_dirs:
        resolved_dir = raw_dir.resolve()
        if resolved_dir in seen_roots or not resolved_dir.is_dir():
            continue
        seen_roots.add(resolved_dir)

        for child in sorted(resolved_dir.iterdir()):
            if not child.is_dir():
                continue
            lock_data = _read_optional_json_dict(child / ".review-attempt.lock")
            rev_data = _read_optional_json_dict(child / "consolidated-review.json")
            if lock_data is None and rev_data is None:
                continue
            if rev_data is None and lock_data is not None and lock_data.get("dry_run") is True:
                continue

            norm_dir = _norm_attempt_dir(child)
            status_str = ""
            lock_sha = lock_data.get("candidate_sha") if lock_data is not None else None
            rev_sha = rev_data.get("revision_sha") if rev_data is not None else None
            if rev_data is not None:
                raw_status = rev_data.get("review_gate_status")
                status_str = str(raw_status) if raw_status is not None else ""
                if status_str in completed_statuses or not rev_data:
                    round_dirs.add(norm_dir)
                discovered_entries[norm_dir] = {
                    "candidate_sha": rev_sha or lock_sha,
                    "status": status_str or "BLOCKED",
                }
            elif lock_data is not None:
                status_str = "IN_PROGRESS"
                round_dirs.add(norm_dir)
                discovered_entries[norm_dir] = {
                    "candidate_sha": lock_sha,
                    "status": status_str,
                }

            if candidate_sha:
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
                    sha_dirs.add(norm_dir)
                    if status_str:
                        sha_statuses[norm_dir] = status_str

    native_tracker_round_dirs: set[str] = set()
    if isinstance(tracker, dict):
        comp_map = tracker.get("completed_attempts", {})
        if isinstance(comp_map, dict):
            for raw_out, entry in comp_map.items():
                if not isinstance(entry, dict):
                    continue
                norm_dir = _norm_attempt_dir(raw_out)
                st = str(entry.get("status") or "")
                if st in completed_statuses or st == "IN_PROGRESS":
                    round_dirs.add(norm_dir)
                    if not entry.get("seeded_from_fs"):
                        native_tracker_round_dirs.add(norm_dir)
                if candidate_sha and entry.get("candidate_sha") == candidate_sha:
                    sha_dirs.add(norm_dir)
                    if st:
                        sha_statuses[norm_dir] = st

        att_map = tracker.get("attempts", {})
        if isinstance(att_map, dict):
            for res_id, entry in att_map.items():
                if not isinstance(entry, dict):
                    continue
                raw_out = entry.get("out_dir")
                norm_dir = _norm_attempt_dir(raw_out) if raw_out else f"attempt:{res_id}"
                st = str(entry.get("status") or "")
                if st in completed_statuses:
                    round_dirs.add(norm_dir)
                    native_tracker_round_dirs.add(norm_dir)
                if candidate_sha and entry.get("candidate_sha") == candidate_sha:
                    sha_dirs.add(norm_dir)
                    if norm_dir not in sha_statuses and st:
                        sha_statuses[norm_dir] = st

        in_flight_map = tracker.get("in_flight", {})
        if isinstance(in_flight_map, dict):
            for res_id, entry in in_flight_map.items():
                if not isinstance(entry, dict):
                    continue
                raw_out = entry.get("out_dir")
                norm_dir = _norm_attempt_dir(raw_out) if raw_out else f"in-flight:{res_id}"
                round_dirs.add(norm_dir)
                if candidate_sha and entry.get("candidate_sha") == candidate_sha:
                    sha_dirs.add(norm_dir)
                    sha_statuses[norm_dir] = "IN_PROGRESS"

    seeded_legacy = 0
    seeded_sha = 0
    if isinstance(tracker, dict):
        imp_rounds = _extract_legacy_rounds_by_source(tracker)
        seeded_legacy = sum(imp_rounds.values())
        if candidate_sha:
            imp_sha = _extract_legacy_nested_by_source(
                tracker,
                imported_key="imported_legacy_sha",
                counts_key="sha_invocations",
                keyed_fn=_keyed_tracker_sha_dirs,
            )
            seeded_sha = sum(sub.get(candidate_sha, 0) for sub in imp_sha.values())

    return {
        "round_dirs": round_dirs,
        "total_rounds": len(round_dirs) + seeded_legacy,
        "sha_dirs": sha_dirs,
        "sha_count": len(sha_dirs) + seeded_sha,
        "sha_statuses": sha_statuses,
        "discovered_entries": discovered_entries,
    }


def count_review_attempts(
    attempts_dirs: list[Path],
    *,
    candidate_sha: str | None = None,
) -> tuple[int, int]:
    """Count completed/locked G4 attempts and prior invocations for ``candidate_sha``."""
    summary = collect_g4_attempt_union(attempts_dirs, None, candidate_sha=candidate_sha)
    return int(summary["total_rounds"]), int(summary["sha_count"])


def _ensure_ledger_defaults(
    data: dict[str, Any],
    *,
    policy_breakers: dict[str, int] | None = None,
) -> dict[str, Any]:
    defaults = policy_breakers or DEFAULT_CIRCUIT_BREAKERS
    if "circuit_breakers" not in data:
        data["circuit_breakers"] = {}
    elif not isinstance(data["circuit_breakers"], dict):
        raise ValueError("Invalid 'circuit_breakers' in ledger.json: expected dict.")
    breakers = data["circuit_breakers"]
    for key, val in defaults.items():
        breakers.setdefault(key, val)
    for br_k, br_v in breakers.items():
        min_val = 0 if br_k in ZERO_OK_CIRCUIT_BREAKERS else 1
        if not isinstance(br_k, str) or not br_k or not is_strict_int(br_v) or br_v < min_val:
            raise ValueError(
                f"Invalid circuit_breakers[{br_k!r}]={br_v!r} in ledger.json "
                f"(expected int >= {min_val})."
            )

    if "packet_version" in data and (
        not is_strict_int(data["packet_version"]) or data["packet_version"] < 1
    ):
        raise ValueError("Invalid 'packet_version' in ledger.json: expected int >= 1.")
    for opt_cnt_key in ("auto_replans_used", "transient_retries_in_stage"):
        if opt_cnt_key in data and (
            not is_strict_int(data[opt_cnt_key]) or data[opt_cnt_key] < 0
        ):
            raise ValueError(
                f"Invalid {opt_cnt_key!r} in ledger.json: expected non-negative int."
            )

    for cnt_key in (
        "g4_rounds_total",
        "g4_rounds_in_packet",
        "total_candidates",
        "candidates_in_packet",
        "consecutive_failed_candidates",
    ):
        if cnt_key not in data:
            data[cnt_key] = 0
        elif not is_strict_int(data[cnt_key]) or data[cnt_key] < 0:
            raise ValueError(
                f"Invalid {cnt_key!r} in ledger.json: expected non-negative int."
            )

    for map_key in (
        "g4_invocations_by_sha",
        "active_g4_reservations",
        "active_reservations",
        "usage_by_conversation",
    ):
        if map_key not in data:
            data[map_key] = {}
        elif not isinstance(data[map_key], dict):
            raise ValueError(f"Invalid {map_key!r} in ledger.json: expected dict.")

    for sha_k, cnt in data["g4_invocations_by_sha"].items():
        if not isinstance(sha_k, str) or not sha_k or not is_strict_int(cnt) or cnt < 0:
            raise ValueError("Invalid entry in ledger.json 'g4_invocations_by_sha'.")
    for res_id, ent in data["active_g4_reservations"].items():
        if not isinstance(res_id, str) or not res_id or not isinstance(ent, dict):
            raise ValueError("Invalid entry in ledger.json 'active_g4_reservations'.")
    for res_id, res_amt in data["active_reservations"].items():
        if not isinstance(res_id, str) or not res_id:
            raise ValueError("Invalid key in ledger.json 'active_reservations'.")
        _require_finite_number(res_amt, f"active_reservations[{res_id!r}]")

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


def extract_root_task_id(
    *,
    task_packet: Path | None = None,
    task_manifest: Path | None = None,
) -> str | None:
    """Extract declared ``root_task_id`` from ``readiness.json`` or ``packet.md``."""
    if task_manifest is not None and task_manifest.is_file():
        m_data = _read_optional_json_dict(task_manifest)
        if isinstance(m_data, dict):
            rt = m_data.get("root_task_id")
            if isinstance(rt, str) and rt.strip():
                return rt.strip()
    if task_packet is not None and task_packet.is_file():
        try:
            text = task_packet.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(
            r"^\s*[-*]\s+\*\*Root Task ID\*\*:\s*`([^`\r\n]+)`",
            text,
            flags=re.MULTILINE,
        )
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def extract_packet_epoch(task_packet: Path | None = None) -> str | None:
    """Return stable packet epoch token (``v<N>:<sha16>`` or ``sha:<sha16>``)."""
    if task_packet is None or not task_packet.is_file():
        return None
    try:
        raw = task_packet.read_bytes()
    except OSError:
        return None
    sha16 = hashlib.sha256(raw).hexdigest()[:16]
    text = raw.decode("utf-8", errors="replace")
    m = re.search(
        r"^\s*[-*]\s+\*\*Packet Version\*\*:\s*`?(\d+)`?\b",
        text,
        flags=re.MULTILINE,
    )
    if m:
        return f"v{m.group(1)}:{sha16}"
    return f"sha:{sha16}"


def discover_child_runs_for_root(
    root_task_id: str,
    *,
    repo_root: Path,
    exclude_ids: set[str] | None = None,
) -> list[Path]:
    """Discover child run directories under ``harness/runs/`` bound to ``root_task_id``."""
    runs_dir = repo_root.resolve() / "harness" / "runs"
    if not runs_dir.is_dir() or not root_task_id:
        return []
    excluded = set(exclude_ids or set()) | {root_task_id}
    matched_dirs: list[Path] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name in excluded:
            continue
        readiness = run_dir / "readiness.json"
        if extract_root_task_id(task_manifest=readiness) == root_task_id:
            matched_dirs.append(run_dir)
            continue
        for pkt in sorted(run_dir.glob("packet*.md")):
            if extract_root_task_id(task_packet=pkt) == root_task_id:
                matched_dirs.append(run_dir)
                break
    return matched_dirs


def find_ledger_path_for_task(
    task_id: str,
    *,
    repo_root: Path,
    task_packet: Path | None = None,
    task_manifest: Path | None = None,
) -> Path | None:
    """Locate ``ledger.json`` by task_id, packet/manifest root_task_id, or runs/ scan."""
    root = repo_root.resolve()
    runs_dir = root / "harness" / "runs"
    try:
        direct = default_ledger_path(task_id, repo_root=root)
        if direct.is_file():
            return direct.resolve()
    except ValueError:
        pass

    hinted_root = extract_root_task_id(task_packet=task_packet, task_manifest=task_manifest)
    if hinted_root:
        try:
            hinted = default_ledger_path(hinted_root, repo_root=root)
            if hinted.is_file():
                return hinted.resolve()
        except ValueError:
            pass

    if runs_dir.is_dir():
        for cand in sorted(runs_dir.glob("*/ledger.json")):
            data = _read_optional_json_dict(cand)
            if not isinstance(data, dict) or not data.get("root_task_id"):
                continue
            if data.get("task_id") == task_id or data.get("root_task_id") == task_id:
                return cand.resolve()
    return None


def validate_ledger_task_binding(
    ledger: dict[str, Any],
    *,
    task_id: str,
    expected_root_task_id: str | None = None,
) -> str | None:
    """Verify ``ledger.json`` belongs to ``task_id`` / ``expected_root_task_id``."""
    actual_root = ledger.get("root_task_id")
    if expected_root_task_id and actual_root != expected_root_task_id:
        return (
            f"Ledger root_task_id mismatch: expected {expected_root_task_id!r}, "
            f"got {actual_root!r}."
        )
    if task_id == "adhoc" and expected_root_task_id is None:
        return None
    allowed_ids = {ledger.get("task_id"), actual_root}
    if task_id not in allowed_ids:
        return (
            f"Ledger task mismatch: ledger belongs to root={actual_root!r} "
            f"task={ledger.get('task_id')!r}, cannot bind task_id={task_id!r}."
        )
    return None


def default_ledger_path(root_task_id: str, repo_root: Path | None = None) -> Path:
    safe_id = _validate_safe_task_id(root_task_id, "root_task_id")
    root = (repo_root or REPO_ROOT).resolve()
    return root / "harness" / "runs" / safe_id / "ledger.json"


def default_g4_tracker_path(task_id: str, repo_root: Path | None = None) -> Path:
    """Return canonical ``harness/runs/<task_id>/g4-tracker.json`` path."""
    safe_id = _validate_safe_task_id(task_id, "task_id")
    root = (repo_root or REPO_ROOT).resolve()
    return root / "harness" / "runs" / safe_id / "g4-tracker.json"


def _atomic_write_bytes(target_path: Path, raw_bytes: bytes) -> None:
    resolved = target_path.resolve()
    with _ledger_lock(resolved):
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = f".{resolved.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp_path = resolved.parent / tmp_name
        try:
            with tmp_path.open("wb") as fh:
                fh.write(raw_bytes)
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


def _atomic_write_json(target_path: Path, data: dict[str, Any]) -> None:
    payload = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write_bytes(target_path, payload)


def load_g4_tracker(tracker_path: Path, *, task_id: str = "") -> dict[str, Any]:
    """Load canonical ``g4-tracker.json`` or initialize an empty tracker payload."""
    resolved = tracker_path.resolve()
    data: dict[str, Any] = {}
    if resolved.is_file():
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid g4-tracker payload at {resolved}")
        data = loaded

    if "schema_version" not in data:
        data["schema_version"] = 1
    elif not is_strict_int(data["schema_version"]) or data["schema_version"] < 1:
        raise ValueError(f"Invalid 'schema_version' in g4-tracker at {resolved}.")

    if "task_id" not in data:
        if task_id:
            data["task_id"] = _validate_safe_task_id(task_id, "task_id")
    else:
        raw_tid = data["task_id"]
        if not isinstance(raw_tid, str) or not raw_tid.strip():
            raise ValueError(f"Invalid 'task_id' in g4-tracker at {resolved}.")
        _validate_safe_task_id(raw_tid, "task_id")

    if "completed_rounds" not in data:
        data["completed_rounds"] = 0
    elif not is_strict_int(data["completed_rounds"]) or data["completed_rounds"] < 0:
        raise ValueError(f"Invalid 'completed_rounds' in g4-tracker at {resolved}.")

    entry_map_keys = ("in_flight", "completed_attempts", "attempts")
    count_map_keys = ("sha_invocations", "packet_rounds", "imported_legacy_rounds")
    nested_map_keys = ("imported_legacy_packets", "imported_legacy_sha")
    for key in (*entry_map_keys, *count_map_keys, *nested_map_keys):
        if key not in data:
            data[key] = {}
        elif not isinstance(data[key], dict):
            raise ValueError(f"Invalid {key!r} in g4-tracker at {resolved}: expected dict.")

    for key in entry_map_keys:
        for k, v in data[key].items():
            if not isinstance(k, str) or not isinstance(v, dict):
                raise ValueError(f"Invalid entry under {key!r} in g4-tracker at {resolved}.")

    for key in count_map_keys:
        for k, v in data[key].items():
            if not isinstance(k, str) or not k or not is_strict_int(v) or v < 0:
                raise ValueError(f"Invalid counter under {key!r} in g4-tracker at {resolved}.")

    for key in nested_map_keys:
        for src_k, sub in data[key].items():
            if not isinstance(src_k, str) or not src_k or not isinstance(sub, dict):
                raise ValueError(f"Invalid source map under {key!r} at {resolved}.")
            for sub_k, sub_v in sub.items():
                if (
                    not isinstance(sub_k, str)
                    or not sub_k
                    or not is_strict_int(sub_v)
                    or sub_v < 0
                ):
                    raise ValueError(f"Invalid counter under {key!r}[{src_k!r}] at {resolved}.")
    return data


def save_g4_tracker(tracker_path: Path, tracker: dict[str, Any]) -> None:
    """Atomically persist ``g4-tracker.json`` under its OS lock."""
    tracker["updated_at_utc"] = _now_utc().isoformat()
    _atomic_write_json(tracker_path, tracker)


def _native_tracker_round_dirs(tracker: dict[str, Any]) -> set[str]:
    round_statuses = {"PASSED", "CHANGES_REQUESTED", "BLOCKED", "IN_PROGRESS"}
    native: set[str] = set()
    for raw_out, ent in (tracker.get("completed_attempts") or {}).items():
        if isinstance(ent, dict) and str(ent.get("status") or "") in round_statuses:
            if not ent.get("seeded_from_fs"):
                native.add(_norm_attempt_dir(raw_out))
    for res_id, ent in (tracker.get("attempts") or {}).items():
        if isinstance(ent, dict) and str(ent.get("status") or "") in round_statuses:
            raw_out = ent.get("out_dir")
            native.add(_norm_attempt_dir(raw_out) if raw_out else f"attempt:{res_id}")
    return native


def _native_tracker_packet_round_dirs(
    tracker: dict[str, Any], packet_epoch: str
) -> set[str]:
    round_statuses = {"PASSED", "CHANGES_REQUESTED", "BLOCKED", "IN_PROGRESS"}
    native: set[str] = set()
    for raw_out, ent in (tracker.get("completed_attempts") or {}).items():
        if (
            isinstance(ent, dict)
            and str(ent.get("status") or "") in round_statuses
            and ent.get("packet_epoch") == packet_epoch
            and not ent.get("seeded_from_fs")
        ):
            native.add(_norm_attempt_dir(raw_out))
    for res_id, ent in (tracker.get("attempts") or {}).items():
        if (
            isinstance(ent, dict)
            and str(ent.get("status") or "") in round_statuses
            and ent.get("packet_epoch") == packet_epoch
        ):
            raw_out = ent.get("out_dir")
            native.add(_norm_attempt_dir(raw_out) if raw_out else f"attempt:{res_id}")
    return native


def _packet_version_bucket(epoch: str | None) -> str | None:
    """Normalize a packet epoch (``v<N>:<sha>`` or ``sha:<sha>``) to its version bucket."""
    if not isinstance(epoch, str) or not epoch:
        return None
    if epoch.startswith("v"):
        head = epoch.split(":", 1)[0]
        if len(head) > 1 and head[1:].isdigit():
            return head
    if epoch.startswith("sha:"):
        return "unversioned"
    return epoch


def count_tracker_packet_rounds(
    tracker: dict[str, Any] | None, packet_epoch: str | None
) -> int:
    """Count distinct G4 rounds recorded in ``tracker`` for ``packet_epoch``'s version bucket."""
    target_bucket = _packet_version_bucket(packet_epoch)
    if not isinstance(tracker, dict) or not target_bucket:
        return 0
    round_statuses = {"PASSED", "CHANGES_REQUESTED", "BLOCKED", "IN_PROGRESS"}
    round_dirs: set[str] = set()
    for raw_out, ent in (tracker.get("completed_attempts") or {}).items():
        if (
            isinstance(ent, dict)
            and str(ent.get("status") or "") in round_statuses
            and _packet_version_bucket(ent.get("packet_epoch")) == target_bucket
        ):
            round_dirs.add(_norm_attempt_dir(raw_out))
    for res_id, ent in (tracker.get("attempts") or {}).items():
        if (
            isinstance(ent, dict)
            and str(ent.get("status") or "") in round_statuses
            and _packet_version_bucket(ent.get("packet_epoch")) == target_bucket
        ):
            raw_out = ent.get("out_dir")
            round_dirs.add(_norm_attempt_dir(raw_out) if raw_out else f"attempt:{res_id}")
    for res_id, ent in (tracker.get("in_flight") or {}).items():
        if (
            isinstance(ent, dict)
            and _packet_version_bucket(ent.get("packet_epoch")) == target_bucket
        ):
            raw_out = ent.get("out_dir")
            round_dirs.add(_norm_attempt_dir(raw_out) if raw_out else f"inflight:{res_id}")
    pkt_map = tracker.get("packet_rounds") or {}
    legacy_residual = 0
    if isinstance(pkt_map, dict):
        for ep_key, cnt in pkt_map.items():
            if _packet_version_bucket(ep_key) == target_bucket:
                native_for_key = len(_native_tracker_packet_round_dirs(tracker, ep_key))
                legacy_residual += max(0, int(cnt) - native_for_key)
    return len(round_dirs) + legacy_residual


def _tracker_source_id(tracker: dict[str, Any]) -> str:
    tid = tracker.get("task_id")
    return tid.strip() if isinstance(tid, str) and tid.strip() else "_anon"


def _keyed_tracker_sha_dirs(tracker: dict[str, Any], candidate_sha: str) -> set[str]:
    keyed: set[str] = set()
    for raw_out, ent in (tracker.get("completed_attempts") or {}).items():
        if isinstance(ent, dict) and ent.get("candidate_sha") == candidate_sha:
            keyed.add(_norm_attempt_dir(raw_out))
    for res_id, ent in (tracker.get("attempts") or {}).items():
        if isinstance(ent, dict) and ent.get("candidate_sha") == candidate_sha:
            raw_out = ent.get("out_dir")
            keyed.add(_norm_attempt_dir(raw_out) if raw_out else f"attempt:{res_id}")
    for res_id, ent in (tracker.get("in_flight") or {}).items():
        if isinstance(ent, dict) and ent.get("candidate_sha") == candidate_sha:
            raw_out = ent.get("out_dir")
            keyed.add(_norm_attempt_dir(raw_out) if raw_out else f"in-flight:{res_id}")
    return keyed


def _extract_legacy_rounds_by_source(tracker: dict[str, Any]) -> dict[str, int]:
    src = _tracker_source_id(tracker)
    imp: dict[str, int] = {}
    raw_imp = tracker.get("imported_legacy_rounds") or {}
    if isinstance(raw_imp, dict):
        for k, v in raw_imp.items():
            if is_strict_int(v) and v >= 0:
                imp[str(k)] = int(v)
    other_sum = sum(v for k, v in imp.items() if k != src)
    raw_comp = tracker.get("completed_rounds", 0)
    comp_val = int(raw_comp) if is_strict_int(raw_comp) and raw_comp >= 0 else 0
    own_legacy = max(0, comp_val - len(_native_tracker_round_dirs(tracker)) - other_sum)
    if own_legacy > 0 or src in imp:
        imp[src] = max(imp.get(src, 0), own_legacy)
    return imp


def _extract_legacy_nested_by_source(
    tracker: dict[str, Any],
    *,
    imported_key: str,
    counts_key: str,
    keyed_fn: Any,
) -> dict[str, dict[str, int]]:
    src = _tracker_source_id(tracker)
    imp: dict[str, dict[str, int]] = {}
    raw_imp = tracker.get(imported_key) or {}
    if isinstance(raw_imp, dict):
        for s_id, sub in raw_imp.items():
            if isinstance(sub, dict):
                imp[str(s_id)] = {
                    str(k): int(v)
                    for k, v in sub.items()
                    if is_strict_int(v) and v >= 0
                }
    counts_map = tracker.get(counts_key) or {}
    if isinstance(counts_map, dict):
        for item_key, total_val in counts_map.items():
            if not is_strict_int(total_val) or total_val < 0:
                continue
            k_str = str(item_key)
            other_sum = sum(
                sub.get(k_str, 0) for s_id, sub in imp.items() if s_id != src
            )
            own_legacy = max(0, int(total_val) - len(keyed_fn(tracker, k_str)) - other_sum)
            if own_legacy > 0 or (src in imp and k_str in imp[src]):
                src_map = imp.setdefault(src, {})
                src_map[k_str] = max(src_map.get(k_str, 0), own_legacy)
    return imp


def merge_g4_trackers(
    base_tracker: dict[str, Any] | None,
    extra_tracker: dict[str, Any] | None,
    *,
    include_in_flight: bool = True,
) -> dict[str, Any] | None:
    """Merge ``extra_tracker`` into ``base_tracker`` idempotently (CRDT GCounter + set union)."""
    if extra_tracker is None:
        return base_tracker
    if base_tracker is None:
        if include_in_flight:
            return extra_tracker
        cloned = json.loads(json.dumps(extra_tracker))
        cloned["in_flight"] = {}
        return cloned
    merged = json.loads(json.dumps(base_tracker))
    copy_keys = (
        ("completed_attempts", "attempts", "in_flight")
        if include_in_flight
        else ("completed_attempts", "attempts")
    )
    for key in copy_keys:
        dst = merged.setdefault(key, {})
        src = extra_tracker.get(key, {})
        if isinstance(dst, dict) and isinstance(src, dict):
            for k, v in src.items():
                dst.setdefault(k, v)

    b_imp_rounds = _extract_legacy_rounds_by_source(base_tracker)
    e_imp_rounds = _extract_legacy_rounds_by_source(extra_tracker)
    merged_imp_rounds: dict[str, int] = {}
    for s_id in sorted(set(b_imp_rounds) | set(e_imp_rounds)):
        val = max(b_imp_rounds.get(s_id, 0), e_imp_rounds.get(s_id, 0))
        if val > 0:
            merged_imp_rounds[s_id] = val
    merged["imported_legacy_rounds"] = merged_imp_rounds
    merged["completed_rounds"] = (
        len(_native_tracker_round_dirs(merged)) + sum(merged_imp_rounds.values())
    )

    def _merge_nested_legacy(
        imported_key: str, counts_key: str, keyed_fn: Any
    ) -> dict[str, dict[str, int]]:
        b_nested = _extract_legacy_nested_by_source(
            base_tracker,
            imported_key=imported_key,
            counts_key=counts_key,
            keyed_fn=keyed_fn,
        )
        e_nested = _extract_legacy_nested_by_source(
            extra_tracker,
            imported_key=imported_key,
            counts_key=counts_key,
            keyed_fn=keyed_fn,
        )
        merged_nested: dict[str, dict[str, int]] = {}
        for s_id in sorted(set(b_nested) | set(e_nested)):
            b_sub = b_nested.get(s_id, {})
            e_sub = e_nested.get(s_id, {})
            sub_out: dict[str, int] = {}
            for k_str in sorted(set(b_sub) | set(e_sub)):
                val = max(b_sub.get(k_str, 0), e_sub.get(k_str, 0))
                if val > 0:
                    sub_out[k_str] = val
            if sub_out:
                merged_nested[s_id] = sub_out
        return merged_nested

    merged_imp_packets = _merge_nested_legacy(
        "imported_legacy_packets", "packet_rounds", _native_tracker_packet_round_dirs
    )
    merged["imported_legacy_packets"] = merged_imp_packets
    all_epochs: set[str] = set()
    for trk in (base_tracker, extra_tracker, merged):
        pkt_map = trk.get("packet_rounds")
        if isinstance(pkt_map, dict):
            all_epochs.update(str(k) for k in pkt_map)
        for sec in ("completed_attempts", "attempts"):
            sec_map = trk.get(sec)
            if isinstance(sec_map, dict):
                for ent in sec_map.values():
                    if isinstance(ent, dict) and isinstance(ent.get("packet_epoch"), str):
                        all_epochs.add(ent["packet_epoch"])
    for sub in merged_imp_packets.values():
        all_epochs.update(sub.keys())
    merged["packet_rounds"] = {
        ep: len(_native_tracker_packet_round_dirs(merged, ep))
        + sum(sub.get(ep, 0) for sub in merged_imp_packets.values())
        for ep in sorted(all_epochs)
    }

    merged_imp_sha = _merge_nested_legacy(
        "imported_legacy_sha", "sha_invocations", _keyed_tracker_sha_dirs
    )
    merged["imported_legacy_sha"] = merged_imp_sha
    all_shas: set[str] = set()
    for trk in (base_tracker, extra_tracker, merged):
        sha_map = trk.get("sha_invocations")
        if isinstance(sha_map, dict):
            all_shas.update(str(k) for k in sha_map)
        for sec in ("completed_attempts", "attempts", "in_flight"):
            sec_map = trk.get(sec)
            if isinstance(sec_map, dict):
                for ent in sec_map.values():
                    if isinstance(ent, dict) and isinstance(ent.get("candidate_sha"), str):
                        all_shas.add(ent["candidate_sha"])
    for sub in merged_imp_sha.values():
        all_shas.update(sub.keys())
    merged["sha_invocations"] = {
        sha: len(_keyed_tracker_sha_dirs(merged, sha))
        + sum(sub.get(sha, 0) for sub in merged_imp_sha.values())
        for sha in sorted(all_shas)
    }
    return merged


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
    if "spent_api_equiv_usd" not in data:
        raise ValueError(f"Missing required 'spent_api_equiv_usd' in ledger at {resolved}.")
    _require_finite_number(data["spent_api_equiv_usd"], "spent_api_equiv_usd")
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
        merged_deadlines = load_deadlines_sec(repo_root=repo_root, policy_path=policy_path)
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


def _count_qualified_reviewer_routes(repo_root: Path) -> int:
    """Count configured AGY reviewer fallback routes in model-policy.json (min 1)."""
    data = _read_optional_json_dict(repo_root / "harness" / "model-policy.json")
    if not isinstance(data, dict):
        return 1
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return 1
    fallbacks = roles.get("independent_reviewer_fallbacks")
    if not isinstance(fallbacks, dict):
        return 1
    routes = fallbacks.get("qualified_routes")
    if not isinstance(routes, list):
        return 1
    return max(1, len(routes))


def run_review_gate_step(
    *,
    commit_sha: str,
    task_id: str,
    packet_path: Path,
    out_dir: Path,
    base_sha: str | None = None,
    repo_root: Path | None = None,
    ledger: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``codex_room_review.py`` via managed subprocess with reviewer deadlines."""
    root = (repo_root or REPO_ROOT).resolve()
    script = Path(__file__).resolve().parent / "codex_room_review.py"
    effective_ledger = ledger_path
    if effective_ledger is None and isinstance(ledger, dict) and ledger.get("root_task_id"):
        cand = default_ledger_path(str(ledger["root_task_id"]), repo_root=root)
        if cand.is_file():
            effective_ledger = cand
    if effective_ledger is None:
        effective_ledger = find_ledger_path_for_task(
            task_id, repo_root=root, task_packet=packet_path
        )
    disk_ledger: dict[str, Any] | None = None
    if ledger is None and effective_ledger is not None and effective_ledger.is_file():
        try:
            disk_ledger = load_ledger(effective_ledger, repo_root=root)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            disk_ledger = None
    cfg_ledger = ledger if isinstance(ledger, dict) else disk_ledger
    deadlines = (
        cfg_ledger.get("deadlines_sec", DEFAULT_DEADLINES_SEC)
        if isinstance(cfg_ledger, dict)
        else load_deadlines_sec(repo_root=root)
    )
    pass_total = int(deadlines.get("reviewer_pass_total", 900))
    pass_idle = int(deadlines.get("reviewer_pass_idle", 270))
    pass_agy_idle = int(deadlines.get("reviewer_agy_pass_idle", 630))
    effective_base = base_sha or (str(cfg_ledger.get("base_sha", "")) if cfg_ledger else "")
    cb_cfg = cfg_ledger.get("circuit_breakers") if isinstance(cfg_ledger, dict) else None
    raw_retries = (
        cb_cfg.get("max_transient_retries_per_provider_pass")
        if isinstance(cb_cfg, dict)
        else None
    )
    if is_strict_int(raw_retries):
        max_retries = max(0, int(raw_retries))
    else:
        max_retries = max(
            0,
            int(
                load_circuit_breakers(repo_root=root).get(
                    "max_transient_retries_per_provider_pass", 1
                )
            ),
        )
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
        "--agy-idle-timeout",
        str(pass_agy_idle),
        "--max-transient-retries",
        str(max_retries),
        "--fail-on-blocking",
    ]
    if effective_base:
        cmd.extend(["--base", effective_base])
    if effective_ledger is not None:
        cmd.extend(["--ledger", str(effective_ledger)])
    attempts_per_pass = 1 + max_retries
    n_routes = _count_qualified_reviewer_routes(root)
    margin = min(120.0, max(0.5, float(pass_total) * 0.2))
    outer_timeout = float(pass_total * attempts_per_pass * (1 + 2 * n_routes) + margin)
    return run_managed_stage_cmd(
        "reviewer_pass_total",
        cmd,
        ledger=cfg_ledger,
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
    task_packet: Path | None = None,
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
    if task_packet is not None:
        cmd.extend(["--task-packet", str(task_packet)])
    effective_ledger = ledger_path
    if effective_ledger is None and isinstance(ledger, dict) and ledger.get("root_task_id"):
        cand = default_ledger_path(str(ledger["root_task_id"]), repo_root=root)
        if cand.is_file():
            effective_ledger = cand
    if effective_ledger is None:
        effective_ledger = find_ledger_path_for_task(
            task_id, repo_root=root, task_manifest=readiness_json
        )
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
    wall_limit = float(ledger["deadlines_sec"].get("root_task_wall_clock", 5400))
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

        already_recorded = any(
            isinstance(ev, dict)
            and ev.get("kind") == "CANDIDATE_READY"
            and isinstance(ev.get("detail"), dict)
            and ev["detail"].get("candidate_sha") == candidate_sha
            for ev in ledger.get("events", [])
        )
        ledger["transient_retries_in_stage"] = 0
        if not already_recorded:
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
    max_total_candidates: int | None = None,
    max_g4_rounds: int | None = None,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> tuple[dict[str, Any], int, str]:
    """Advance ledger to a bumped ``packet_version`` after validated ``readiness.json``."""
    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        current_state = ledger["state"]
        if current_state == "VERIFIED":
            return ledger, 2, "Refusing replan: ledger is already VERIFIED."
        active_root = (repo_root or REPO_ROOT).resolve()
        in_flight_g4 = bool(ledger.get("active_g4_reservations"))
        if not in_flight_g4:
            tracker_candidates = [ledger_path.resolve().parent / "g4-tracker.json"]
            if repo_root is not None:
                tracker_candidates.append(
                    default_g4_tracker_path(str(ledger["root_task_id"]), repo_root=active_root)
                )
            for cand_t in tracker_candidates:
                if cand_t.is_file():
                    try:
                        t_data = load_g4_tracker(cand_t)
                    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                        return (
                            ledger,
                            2,
                            f"Refusing replan: corrupt g4-tracker.json at {cand_t}: {exc}",
                        )
                    if t_data.get("in_flight"):
                        in_flight_g4 = True
                        break
        if in_flight_g4:
            return (
                ledger,
                2,
                "Refusing replan while G4 review reservation is active in flight.",
            )
        if (max_total_candidates is not None or max_g4_rounds is not None) and (
            not human_approved
        ):
            return ledger, 2, "Ceiling overrides require --human-approved."
        if max_total_candidates is not None and (
            not is_strict_int(max_total_candidates) or max_total_candidates < 1
        ):
            return (
                ledger,
                2,
                f"Invalid max_total_candidates={max_total_candidates!r}; must be >= 1.",
            )
        if max_g4_rounds is not None and (
            not is_strict_int(max_g4_rounds) or max_g4_rounds < 1
        ):
            return ledger, 2, f"Invalid max_g4_rounds={max_g4_rounds!r}; must be >= 1."
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
            cb = ledger.setdefault("circuit_breakers", {})
            if max_total_candidates is not None:
                cb["max_total_candidates_per_root_task"] = int(max_total_candidates)
            elif int(ledger.get("total_candidates", 0)) >= int(
                cb.get("max_total_candidates_per_root_task", 6)
            ):
                cb["max_total_candidates_per_root_task"] = (
                    int(ledger.get("total_candidates", 0)) + 1
                )
            if max_g4_rounds is not None:
                cb["max_g4_rounds_per_root_task"] = int(max_g4_rounds)
            elif int(ledger.get("g4_rounds_total", 0)) >= int(
                cb.get("max_g4_rounds_per_root_task", 6)
            ):
                cb["max_g4_rounds_per_root_task"] = int(ledger.get("g4_rounds_total", 0)) + 1
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


def _validate_usage_inputs(
    *,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    cache_read_tokens: int,
    counter_mode: str,
) -> None:
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


def apply_usage_to_ledger_dict(
    ledger: dict[str, Any],
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    conversation_id: str | None = None,
    counter_mode: str = "cumulative",
    pricing: dict[str, dict[str, float]] | None = None,
) -> tuple[float, int]:
    """Apply token usage in memory with high-water deduplication for cumulative sessions."""
    _validate_usage_inputs(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        cache_read_tokens=cache_read_tokens,
        counter_mode=counter_mode,
    )
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
    return delta_usd, rc


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
    _validate_usage_inputs(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        cache_read_tokens=cache_read_tokens,
        counter_mode=counter_mode,
    )

    with _ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        delta_usd, rc = apply_usage_to_ledger_dict(
            ledger,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cache_read_tokens=cache_read_tokens,
            conversation_id=conversation_id,
            counter_mode=counter_mode,
            pricing=pricing,
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
    p_rep.add_argument("--max-total-candidates", type=int, default=None)
    p_rep.add_argument("--max-g4-rounds", type=int, default=None)
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
            max_total_candidates=args.max_total_candidates,
            max_g4_rounds=args.max_g4_rounds,
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
