"""Independent 2-pass Codex Room Lead Review and finding consolidator.

Runs two isolated read-only review passes on an immutable commit SHA:
  Pass 1: Correctness, spec adherence, concurrency, and test strength.
  Pass 2: Security, Prop Firm Read-Only, F01-F08 & R01-R06 reliability.

Prioritizes Codex CLI (`codex exec -s read-only`), with optional fallback to
Antigravity CLI (`agy --mode plan --sandbox --model claude-opus-5-5-medium`)
when Codex CLI quota is exhausted or unavailable (`--backend auto`).

Enforces test-required.md §7: no stale-SHA approval, no majority-vote
dismissal (any blocking finding from either pass keeps status OPEN).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import inspect
import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts import task_runner
    from harness.scripts.codex_room_sync import (
        EXPECTED_REVIEW_MODEL,
        is_excluded_model,
        load_model_policy,
        verify_role,
    )
    from harness.scripts.schema_utils import (
        cleanup_readonly_tree,
        close_job_handle,
        compute_tree_sha256,
        is_hex_sha40,
        is_strict_int,
        materialize_frozen_tree,
        resolve_git_toplevel,
        run_managed_cmd,
        spawn_managed_process,
        terminate_process_tree,
        validate_instance_against_schema,
    )
except ImportError:
    import task_runner
    from codex_room_sync import (
        EXPECTED_REVIEW_MODEL,
        is_excluded_model,
        load_model_policy,
        verify_role,
    )
    from schema_utils import (
        cleanup_readonly_tree,
        close_job_handle,
        compute_tree_sha256,
        is_hex_sha40,
        is_strict_int,
        materialize_frozen_tree,
        resolve_git_toplevel,
        run_managed_cmd,
        spawn_managed_process,
        terminate_process_tree,
        validate_instance_against_schema,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_ROOM_DIR = REPO_ROOT / "harness" / "codex-room"
SCHEMA_PATH = CODEX_ROOM_DIR / "schemas" / "review-output.schema.json"
PROMPTS_DIR = CODEX_ROOM_DIR / "prompts"

DEFAULT_AGY_FALLBACK_MODEL = "claude-opus-5-5-medium"
AGY_INLINE_PROMPT_CHAR_LIMIT = 16000

OUTCOME_AUTH_ERROR = "AUTH_ERROR"
OUTCOME_CONFIRMED_QUOTA = "CONFIRMED_QUOTA_EXHAUSTED"
OUTCOME_TRANSIENT = "TRANSIENT_RATE_OR_TRANSPORT"
OUTCOME_SCHEMA_OR_POLICY = "SCHEMA_OR_SHA_OR_POLICY_ERROR"

PASS_LENS_MAP: dict[str, str] = {
    "pass1": "correctness_and_tests",
    "pass2": "security_and_invariants",
}
PROVENANCE_TUPLE_KEYS: tuple[str, ...] = (
    "candidate_sha",
    "base_sha",
    "packet_sha256",
    "policy_sha256",
    "schema_sha256",
    "lens",
    "frozen_tree_sha256",
)

CODEX_AUTH_RE = re.compile(
    r"(?:\b401\b|\bunauthorized\b|token_revoked|invalidated oauth token"
    r"|refresh token was revoked|invalid_api_key|authentication failed)",
    re.IGNORECASE,
)
CODEX_QUOTA_MARKERS: tuple[str, ...] = (
    "usage_limit_reached",
    "insufficient_quota",
    "hit your usage limit",
    "exceeded your current quota",
    "credit balance",
    "billing_hard_limit_reached",
)
CODEX_TRANSIENT_RE = re.compile(
    r"(?:rate_limit_exceeded|request timed out|connection reset"
    r"|\b502\b|\b503\b|\b504\b|\b429\b)",
    re.IGNORECASE,
)
AGY_AUTH_TEXT_MARKERS: tuple[str, ...] = (
    "unauthenticated",
    "not logged in",
    "sign in",
)
AGY_QUOTA_TEXT_MARKERS: tuple[str, ...] = (
    "individual quota reached",
    "quota exhausted",
    "quota reached",
    "resource_exhausted",
)
AGY_TRANSIENT_RE = re.compile(
    r"(?:\b502\b|\b503\b|\b504\b|deadline_exceeded|\bunavailable\b"
    r"|connection reset|request timed out)",
    re.IGNORECASE,
)
RETRY_AFTER_RE = re.compile(
    r"retry[-_\s]?after(?:\s*[:=]\s*|\s+)(\d+(?:\.\d+)?)\s*s?",
    re.IGNORECASE,
)

_PASS_EXEC_META_LOCK = threading.Lock()
_PASS_EXEC_META: dict[tuple[str, str], tuple[int, str | None]] = {}


@dataclass(frozen=True)
class PassFailure:
    """Typed classification for a failed review pass."""

    category: str
    reason: str
    retry_after_sec: float | None = None


@dataclass
class PassState:
    """Mutable per-pass state tracked across retries, fallbacks, and resume."""

    pass_name: str
    lens: str
    payload: dict[str, Any] | None = None
    backend: str | None = None
    model: str | None = None
    reused: bool = False
    is_complete: bool = False


SEVERITY_RANK = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}
BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})


def validate_strict_json_schema(schema: Any, path: str = "$") -> list[str]:
    """Verify JSON Schema satisfies OpenAI Structured Outputs strict constraints."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    schema_type = schema.get("type")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        props = schema.get("properties", {})
        req = schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(req, list):
            errors.append(f"{path}: properties must be object and required must be array")
        else:
            missing_req = [k for k in props if k not in req]
            if missing_req:
                errors.append(f"{path}: missing required keys {missing_req}")
            for k, sub in props.items():
                errors.extend(validate_strict_json_schema(sub, f"{path}.properties.{k}"))
    elif schema_type == "array":
        if "items" not in schema:
            errors.append(f"{path}: array schema missing items")
        else:
            errors.extend(validate_strict_json_schema(schema["items"], f"{path}.items"))
    return errors


def resolve_codex_command() -> list[str]:
    """Resolve Codex CLI executable safely across Windows (.cmd/.exe/.ps1) and POSIX."""
    for candidate in ("codex.cmd", "codex.exe", "codex"):
        found = shutil.which(candidate)
        if found:
            if found.lower().endswith(".ps1"):
                return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", found]
            return [found]
    ps1 = shutil.which("codex.ps1")
    if ps1:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
    raise FileNotFoundError("Codex CLI (`codex`) not found on PATH.")


def resolve_agy_command() -> list[str]:
    """Resolve Antigravity CLI (`agy`) executable safely across Windows and POSIX."""
    for candidate in ("agy.exe", "agy.cmd", "agy"):
        found = shutil.which(candidate)
        if found:
            return [found]
    raise FileNotFoundError("Antigravity CLI (`agy`) not found on PATH.")


def _norm_provider_text(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").lower()


def _parse_retry_after_value(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val >= 0 else None
    if isinstance(val, str):
        try:
            parsed = float(val.strip())
            return parsed if parsed >= 0 else None
        except ValueError:
            return None
    return None


def _extract_retry_after_from_text(text: str) -> float | None:
    match = RETRY_AFTER_RE.search(text)
    if not match:
        return None
    return _parse_retry_after_value(match.group(1))


def _non_watchdog_stderr(stderr_text: str) -> str:
    lines = [
        ln
        for ln in stderr_text.splitlines()
        if not ln.lstrip().startswith("[codex_room_review]")
    ]
    return "\n".join(lines)


def _extract_codex_error_events(raw_jsonl_text: str) -> list[tuple[str, float | None]]:
    """Extract only `error` and `turn.failed` Codex JSONL events (never tool outputs)."""
    events: list[tuple[str, float | None]] = []
    for line in raw_jsonl_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in {"error", "turn.failed"}:
            continue
        parts: list[str] = []
        retry_after: float | None = None
        for key in ("message", "code", "type"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        err_obj = ev.get("error")
        if isinstance(err_obj, dict):
            for key in ("message", "code", "type"):
                val = err_obj.get(key)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
            retry_after = _parse_retry_after_value(
                err_obj.get("retry_after_sec")
                or err_obj.get("retry_after")
                or err_obj.get("retryAfterSec")
            )
        elif isinstance(err_obj, str) and err_obj.strip():
            parts.append(err_obj.strip())
        if retry_after is None:
            retry_after = _parse_retry_after_value(
                ev.get("retry_after_sec") or ev.get("retry_after")
            )
        joined = " ".join(parts).strip()
        if joined:
            events.append((joined, retry_after))
    return events


def classify_codex_failure(
    out_dir: Path,
    pass_name: str,
    *,
    returncode: int = 1,
    termination_reason: str | None = None,
    pass_payload: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> PassFailure:
    """Classify a failed Codex pass into AUTH, QUOTA, TRANSIENT, or SCHEMA/POLICY."""
    if pass_payload is not None and validation_errors:
        return PassFailure(
            OUTCOME_SCHEMA_OR_POLICY,
            f"{pass_name}: validation error ({'; '.join(validation_errors)})",
        )

    raw_path = out_dir / f"{pass_name}.raw.jsonl"
    stderr_path = out_dir / f"{pass_name}.stderr.log"
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.is_file() else ""
    stderr_text = (
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    )

    sources: list[tuple[str, str, float | None]] = []
    for ev_text, ev_retry in _extract_codex_error_events(raw_text):
        sources.append((f"{pass_name}.raw.jsonl", ev_text, ev_retry))
    stderr_provider = _non_watchdog_stderr(stderr_text)
    if stderr_provider.strip():
        sources.append(
            (
                f"{pass_name}.stderr.log",
                stderr_provider,
                _extract_retry_after_from_text(stderr_provider),
            )
        )

    for src_name, text, _ in sources:
        norm = _norm_provider_text(text)
        m_auth = CODEX_AUTH_RE.search(norm)
        if m_auth:
            return PassFailure(
                OUTCOME_AUTH_ERROR,
                f"codex_auth_error ({src_name}: matched '{m_auth.group(0)}')",
            )

    for src_name, text, _ in sources:
        norm = _norm_provider_text(text)
        for marker in CODEX_QUOTA_MARKERS:
            if marker in norm:
                return PassFailure(
                    OUTCOME_CONFIRMED_QUOTA,
                    f"codex_quota_exhausted ({src_name}: matched '{marker}')",
                )

    has_watchdog = termination_reason is not None or (
        "[codex_room_review]" in stderr_text and "terminated:" in stderr_text
    )
    if has_watchdog:
        return PassFailure(
            OUTCOME_SCHEMA_OR_POLICY,
            f"{pass_name}: watchdog timeout",
        )

    for src_name, text, ev_retry in sources:
        norm = _norm_provider_text(text)
        m_tr = CODEX_TRANSIENT_RE.search(norm)
        if m_tr:
            retry_sec = ev_retry if ev_retry is not None else _extract_retry_after_from_text(norm)
            return PassFailure(
                OUTCOME_TRANSIENT,
                f"codex_transient_error ({src_name}: matched '{m_tr.group(0)}')",
                retry_after_sec=retry_sec,
            )

    return PassFailure(
        OUTCOME_SCHEMA_OR_POLICY,
        f"{pass_name}: non-quota codex failure (rc={returncode})",
    )


def _extract_agy_error_objects(stderr_text: str) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    marker = "AGY_ERROR:"
    while True:
        pos = stderr_text.find(marker, idx)
        if pos == -1:
            break
        brace = stderr_text.find("{", pos + len(marker))
        if brace == -1:
            idx = pos + len(marker)
            continue
        try:
            obj, end_idx = decoder.raw_decode(stderr_text, brace)
            if isinstance(obj, dict):
                objs.append(obj)
            idx = end_idx
        except json.JSONDecodeError:
            idx = brace + 1
    return objs


def _extract_agy_result_errors(raw_jsonl_text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for line in raw_jsonl_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            res = ev["result"]
            if str(res.get("status", "")).upper() == "ERROR":
                results.append(res)
    return results


def _scan_raw_usage_anomalies(raw_text: str, *, reject_positive: bool) -> bool:
    """Detect malformed token values (and optionally >0 counts) across JSONL usage dicts."""
    usage_keys = (
        "input_tokens",
        "cache_read_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
    )
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        res = ev.get("result") if isinstance(ev.get("result"), dict) else None
        usage = (
            res.get("usage")
            if (res and isinstance(res.get("usage"), dict))
            else ev.get("usage")
        )
        if not isinstance(usage, dict):
            continue
        for key in usage_keys:
            if key not in usage:
                continue
            val = usage[key]
            if reject_positive:
                if not is_strict_int(val) or val != 0:
                    return True
            elif val is not None and (not is_strict_int(val) or val < 0):
                return True
    return False


def _agy_raw_has_nonzero_or_bad_usage(raw_text: str) -> bool:
    """Return True if raw AGY JSONL records any >0 token count or malformed token value."""
    return _scan_raw_usage_anomalies(raw_text, reject_positive=True)


def _raw_log_has_malformed_usage(raw_path: Path) -> bool:
    """Return True if raw JSONL contains non-None non-int or negative token values."""
    if not raw_path.is_file():
        return False
    return _scan_raw_usage_anomalies(
        raw_path.read_text(encoding="utf-8", errors="replace"),
        reject_positive=False,
    )


def classify_agy_failure(
    out_dir: Path,
    pass_name: str,
    *,
    returncode: int = 1,
    termination_reason: str | None = None,
    pass_payload: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> PassFailure:
    """Classify a failed AGY pass into AUTH, QUOTA, TRANSIENT, or SCHEMA/POLICY."""
    if pass_payload is not None and validation_errors:
        return PassFailure(
            OUTCOME_SCHEMA_OR_POLICY,
            f"{pass_name}: validation error ({'; '.join(validation_errors)})",
        )

    raw_path = out_dir / f"{pass_name}.raw.jsonl"
    stderr_path = out_dir / f"{pass_name}.stderr.log"
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.is_file() else ""
    stderr_text = (
        stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    )
    stderr_provider = _non_watchdog_stderr(stderr_text)
    agy_errors = _extract_agy_error_objects(stderr_provider)
    result_errors = _extract_agy_result_errors(raw_text)

    for obj in agy_errors:
        code = obj.get("error_code")
        status = str(obj.get("status", "")).upper()
        if code in {401, 403} or status == "UNAUTHENTICATED":
            return PassFailure(
                OUTCOME_AUTH_ERROR,
                f"agy_auth_error ({pass_name}.stderr.log: code={code} status={status})",
            )

    for res in result_errors:
        code = res.get("error_code")
        err_blob = _norm_provider_text(
            " ".join(str(res.get(k, "")) for k in ("error", "message", "short_error", "detail"))
        )
        if code in {401, 403} or any(m in err_blob for m in AGY_AUTH_TEXT_MARKERS):
            return PassFailure(
                OUTCOME_AUTH_ERROR,
                f"agy_auth_error ({pass_name}.raw.jsonl: {err_blob[:120]})",
            )

    for obj in agy_errors:
        code = obj.get("error_code")
        status = str(obj.get("status", "")).upper()
        short_err = _norm_provider_text(
            str(obj.get("short_error") or obj.get("message") or obj.get("error") or "")
        )
        if status == "RESOURCE_EXHAUSTED" or (
            code == 429 and ("quota" in short_err or "limit" in short_err)
        ):
            return PassFailure(
                OUTCOME_CONFIRMED_QUOTA,
                f"agy_quota_exhausted ({pass_name}.stderr.log: status={status} code={code})",
            )

    for res in result_errors:
        err_blob = _norm_provider_text(
            " ".join(str(res.get(k, "")) for k in ("error", "message", "short_error"))
        )
        for marker in AGY_QUOTA_TEXT_MARKERS:
            if marker in err_blob:
                return PassFailure(
                    OUTCOME_CONFIRMED_QUOTA,
                    f"agy_quota_exhausted ({pass_name}.raw.jsonl: matched '{marker}')",
                )

    has_watchdog = termination_reason is not None or (
        "[codex_room_review]" in stderr_text and "terminated:" in stderr_text
    )
    if has_watchdog:
        return PassFailure(OUTCOME_SCHEMA_OR_POLICY, f"{pass_name}: watchdog timeout")

    bad_or_nonzero = _agy_raw_has_nonzero_or_bad_usage(raw_text)
    _, _, r_in, r_cache, r_out = _extract_agy_cumulative_usage(raw_path)
    zero_raw_tokens = (r_in + r_cache + r_out) == 0 and not bad_or_nonzero
    stderr_norm = _norm_provider_text(stderr_provider)

    for res in result_errors:
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        total_tokens = usage.get("total_tokens")
        err_blob = _norm_provider_text(
            " ".join(str(res.get(k, "")) for k in ("error", "message", "short_error"))
        )
        retry_sec = _parse_retry_after_value(
            res.get("retry_after_sec") or res.get("retry_after")
        ) or _extract_retry_after_from_text(err_blob)
        if (
            is_strict_int(total_tokens)
            and total_tokens == 0
            and zero_raw_tokens
            and (
                "failed to start conversation" in err_blob
                or "empty conversation id" in err_blob
            )
        ):
            return PassFailure(
                OUTCOME_TRANSIENT,
                f"agy_transient_startup ({pass_name}.raw.jsonl: 0-token startup race)",
                retry_after_sec=retry_sec,
            )
        if AGY_TRANSIENT_RE.search(err_blob):
            return PassFailure(
                OUTCOME_TRANSIENT,
                f"agy_transient_error ({pass_name}.raw.jsonl: {err_blob[:120]})",
                retry_after_sec=retry_sec,
            )

    for obj in agy_errors:
        code = obj.get("error_code")
        status = str(obj.get("status", "")).upper()
        short_err = _norm_provider_text(
            str(obj.get("short_error") or obj.get("message") or obj.get("error") or "")
        )
        retry_sec = _parse_retry_after_value(
            obj.get("retry_after_sec") or obj.get("retry_after") or obj.get("retryAfterSec")
        ) or _extract_retry_after_from_text(short_err)
        if (
            not result_errors
            and zero_raw_tokens
            and (
                "failed to start conversation" in short_err
                or "empty conversation id" in short_err
            )
        ):
            return PassFailure(
                OUTCOME_TRANSIENT,
                f"agy_transient_startup ({pass_name}.stderr.log: 0-token startup race)",
                retry_after_sec=retry_sec,
            )
        if (
            code in {429, 502, 503, 504}
            or status in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}
            or AGY_TRANSIENT_RE.search(short_err)
        ):
            return PassFailure(
                OUTCOME_TRANSIENT,
                f"agy_transient_error ({pass_name}.stderr.log: status={status} code={code})",
                retry_after_sec=retry_sec,
            )

    if (
        not result_errors
        and zero_raw_tokens
        and (
            "failed to start conversation" in stderr_norm
            or "empty conversation id" in stderr_norm
        )
    ):
        return PassFailure(
            OUTCOME_TRANSIENT,
            f"agy_transient_startup ({pass_name}.stderr.log: 0-token startup race)",
            retry_after_sec=_extract_retry_after_from_text(stderr_provider),
        )

    if AGY_TRANSIENT_RE.search(stderr_provider):
        return PassFailure(
            OUTCOME_TRANSIENT,
            f"agy_transient_error ({pass_name}.stderr.log)",
            retry_after_sec=_extract_retry_after_from_text(stderr_provider),
        )

    return PassFailure(
        OUTCOME_SCHEMA_OR_POLICY,
        f"{pass_name}: non-quota agy failure (rc={returncode})",
    )


def _detect_codex_quota_error(
    out_dir: Path, pass_names: tuple[str, ...] = ("pass1", "pass2")
) -> str | None:
    """Return confirmed quota exhaustion reason from Codex pass logs, if any."""
    for pass_name in pass_names:
        classification = classify_codex_failure(out_dir, pass_name)
        if classification.category == OUTCOME_CONFIRMED_QUOTA:
            return classification.reason
    return None


def resolve_quota_pool(model: str | None, policy: dict[str, Any] | None = None) -> str:
    """Map a reviewer model name to its quota pool identifier."""
    if isinstance(policy, dict):
        roles = policy.get("roles", {})
        if isinstance(roles, dict):
            fb = roles.get("independent_reviewer_fallbacks", {})
            if isinstance(fb, dict):
                pools = fb.get("quota_pools", {})
                if isinstance(pools, dict) and isinstance(model, str) and model in pools:
                    pool_val = pools[model]
                    if isinstance(pool_val, str) and pool_val.strip():
                        return pool_val.strip()
    if model is None or model == EXPECTED_REVIEW_MODEL or str(model).lower().startswith("gpt-"):
        return "openai_codex"
    low = str(model).strip().lower()
    if low.startswith("claude-"):
        return "anthropic_agy"
    if low.startswith("gemini-"):
        return "gemini_agy"
    return f"pool:{low}"


def extract_qualified_routes(
    policy: dict[str, Any] | None,
    excluded_prefixes: tuple[str, ...],
) -> tuple[list[str], str | None]:
    """Read and validate `roles.independent_reviewer_fallbacks.qualified_routes`."""
    if not isinstance(policy, dict):
        return [], None
    roles = policy.get("roles")
    if not isinstance(roles, dict):
        return [], None
    fb = roles.get("independent_reviewer_fallbacks")
    if not isinstance(fb, dict):
        return [], None
    raw_routes = fb.get("qualified_routes", [])
    if not isinstance(raw_routes, list):
        return [], "model-policy.json qualified_routes must be a list."
    routes: list[str] = []
    for item in raw_routes:
        model_val = item.get("model") if isinstance(item, dict) else item
        if not isinstance(model_val, str) or not model_val.strip():
            return [], f"Invalid entry in qualified_routes: {item!r}"
        clean = model_val.strip()
        if is_excluded_model(clean, excluded_prefixes):
            return [], f"Excluded model {clean!r} in qualified_routes (MOD-001)."
        routes.append(clean)
    return routes, None


def extract_max_transient_retries(policy: dict[str, Any] | None) -> int:
    """Read `circuit_breakers.max_transient_retries_per_provider_pass` (default 1)."""
    if not isinstance(policy, dict):
        return 1
    breakers = policy.get("circuit_breakers")
    if not isinstance(breakers, dict):
        return 1
    val = breakers.get("max_transient_retries_per_provider_pass", 1)
    if is_strict_int(val) and val >= 0:
        return int(val)
    return 1


def extract_codex_window_guard_config(
    policy: dict[str, Any] | None,
) -> tuple[bool, float, str | None]:
    """Validate 5h window preflight fallback settings under independent_reviewer_primary."""
    if not isinstance(policy, dict):
        return True, 90.0, None
    roles = policy.get("roles")
    if roles is None:
        return True, 90.0, None
    if not isinstance(roles, dict):
        return False, 90.0, "model-policy.json roles must be an object."
    primary = roles.get("independent_reviewer_primary")
    if primary is None:
        return True, 90.0, None
    if not isinstance(primary, dict):
        return (
            False,
            90.0,
            "model-policy.json roles.independent_reviewer_primary must be an object.",
        )

    enabled = True
    if "fallback_on_5h_window_exhausted" in primary:
        raw_enabled = primary["fallback_on_5h_window_exhausted"]
        if not isinstance(raw_enabled, bool):
            return (
                False,
                90.0,
                "fallback_on_5h_window_exhausted must be a strict boolean.",
            )
        enabled = raw_enabled

    threshold = 90.0
    if "primary_window_exhausted_percent" in primary:
        raw_pct = primary["primary_window_exhausted_percent"]
        if (
            isinstance(raw_pct, bool)
            or not isinstance(raw_pct, (int, float))
            or not math.isfinite(float(raw_pct))
            or float(raw_pct) <= 0.0
            or float(raw_pct) > 100.0
        ):
            return (
                False,
                90.0,
                "primary_window_exhausted_percent must be finite in (0, 100].",
            )
        threshold = float(raw_pct)

    return enabled, threshold, None


def probe_codex_rate_limits(
    codex_prefix: list[str],
    runtime_dir: Path,
    *,
    init_timeout_sec: float = 3.0,
    read_timeout_sec: float = 6.0,
) -> dict[str, Any] | None:
    """Query ``codex app-server`` ``account/rateLimits/read`` via JSON-RPC over stdio."""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(runtime_dir)
    proc: subprocess.Popen[str] | None = None
    job_handle: int | None = None
    out_queue: queue.Queue[str | None] = queue.Queue()
    reader: threading.Thread | None = None
    try:
        proc, job_handle = spawn_managed_process(
            [*codex_prefix, "app-server"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (OSError, RuntimeError):
        return None

    def _pump_stdout() -> None:
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                out_queue.put(line)
        except OSError:
            pass
        finally:
            out_queue.put(None)

    def _read_response(target_id: int, timeout_sec: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.05, timeout_sec)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = out_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            if not isinstance(msg, dict):
                return None
            if msg.get("id") != target_id:
                continue
            if "error" in msg:
                return None
            return msg

    try:
        if proc.stdin is None or proc.stdout is None:
            return None
        reader = threading.Thread(target=_pump_stdout, daemon=True)
        reader.start()
        init_req = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex_room_review", "version": "1.0"}
                },
            }
        )
        proc.stdin.write(init_req + "\n")
        proc.stdin.flush()
        init_msg = _read_response(1, init_timeout_sec)
        if not init_msg or not isinstance(init_msg.get("result"), dict):
            return None

        notif = json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        read_req = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            }
        )
        proc.stdin.write(notif + "\n" + read_req + "\n")
        proc.stdin.flush()
        read_msg = _read_response(2, read_timeout_sec)
        if not read_msg or not isinstance(read_msg.get("result"), dict):
            return None
        return read_msg["result"]
    except (OSError, RuntimeError):
        return None
    finally:
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                terminate_process_tree(proc, job_handle=job_handle)
                job_handle = None
            except OSError:
                pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if job_handle is not None:
            try:
                close_job_handle(job_handle)
            except OSError:
                pass
        if reader is not None:
            reader.join(timeout=0.2)


def _rl_field(obj: Any, camel: str, snake: str) -> Any:
    if not isinstance(obj, dict):
        return None
    if camel in obj:
        return obj[camel]
    return obj.get(snake)


def _parse_rl_pct(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f_val = float(val)
    return f_val if math.isfinite(f_val) else None


def _parse_rl_int(val: Any) -> int | None:
    return int(val) if is_strict_int(val) else None


def _build_rl_window_snapshot(win: Any) -> dict[str, Any] | None:
    if not isinstance(win, dict):
        return None
    raw_mins = (
        win["windowDurationMins"]
        if "windowDurationMins" in win
        else win.get("window_minutes", win.get("window_duration_mins"))
    )
    return {
        "usedPercent": _parse_rl_pct(_rl_field(win, "usedPercent", "used_percent")),
        "windowDurationMins": _parse_rl_int(raw_mins),
        "resetsAt": _parse_rl_int(_rl_field(win, "resetsAt", "resets_at")),
    }


def evaluate_codex_window_exhaustion(
    rate_limits_result: dict[str, Any] | None,
    *,
    threshold_pct: float = 90.0,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Evaluate Codex 5h/secondary window limits and build allowlisted preflight snapshot."""
    if not isinstance(rate_limits_result, dict):
        return False, None, None
    by_id = _rl_field(rate_limits_result, "rateLimitsByLimitId", "rate_limits_by_limit_id")
    bucket: Any = None
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        bucket = by_id["codex"]
    if not isinstance(bucket, dict):
        bucket = _rl_field(rate_limits_result, "rateLimits", "rate_limits")
    if not isinstance(bucket, dict) and "primary" in rate_limits_result:
        bucket = rate_limits_result
    if not isinstance(bucket, dict):
        return False, None, None

    primary = bucket.get("primary")
    secondary = bucket.get("secondary")
    primary_used = _parse_rl_pct(_rl_field(primary, "usedPercent", "used_percent"))
    primary_resets = _parse_rl_int(_rl_field(primary, "resetsAt", "resets_at"))
    secondary_used = _parse_rl_pct(_rl_field(secondary, "usedPercent", "used_percent"))
    secondary_resets = _parse_rl_int(_rl_field(secondary, "resetsAt", "resets_at"))
    rate_limit_type = _rl_field(bucket, "rateLimitReachedType", "rate_limit_reached_type")
    if rate_limit_type is None:
        rate_limit_type = _rl_field(
            rate_limits_result, "rateLimitReachedType", "rate_limit_reached_type"
        )
    spend_reached = _rl_field(bucket, "spendControlReached", "spend_control_reached")
    if spend_reached is None:
        spend_reached = _rl_field(
            rate_limits_result, "spendControlReached", "spend_control_reached"
        )
    ordinary_allowed = _rl_field(bucket, "ordinaryUsageAllowed", "ordinary_usage_allowed")
    if ordinary_allowed is None:
        ordinary_allowed = _rl_field(
            rate_limits_result, "ordinaryUsageAllowed", "ordinary_usage_allowed"
        )

    raw_limit_id = _rl_field(bucket, "limitId", "limit_id")
    raw_plan_type = _rl_field(bucket, "planType", "plan_type")
    raw_credits = bucket.get("credits")
    credits_snap: dict[str, Any] | None = None
    if isinstance(raw_credits, dict):
        raw_has = _rl_field(raw_credits, "hasCredits", "has_credits")
        raw_unlim = _rl_field(raw_credits, "unlimited", "unlimited")
        credits_snap = {
            "hasCredits": raw_has if isinstance(raw_has, bool) else None,
            "unlimited": raw_unlim if isinstance(raw_unlim, bool) else None,
        }
    clean_rlt = (
        rate_limit_type.strip()
        if isinstance(rate_limit_type, str) and rate_limit_type.strip()
        else None
    )
    sanitized: dict[str, Any] = {
        "limitId": raw_limit_id if isinstance(raw_limit_id, str) and raw_limit_id else "codex",
        "planType": raw_plan_type if isinstance(raw_plan_type, str) else None,
        "ordinaryUsageAllowed": (
            ordinary_allowed if isinstance(ordinary_allowed, bool) else None
        ),
        "spendControlReached": spend_reached if isinstance(spend_reached, bool) else None,
        "rateLimitReachedType": clean_rlt,
        "primary": _build_rl_window_snapshot(primary),
        "secondary": _build_rl_window_snapshot(secondary),
        "credits": credits_snap,
    }

    if primary_used is not None and primary_used >= threshold_pct:
        reason = (
            f"codex_5h_window_exhausted "
            f"(usedPercent={primary_used}, resetsAt={primary_resets})"
        )
        return True, reason, sanitized
    if secondary_used is not None and secondary_used >= 100.0:
        reason = (
            f"codex_secondary_window_exhausted "
            f"(usedPercent={secondary_used}, resetsAt={secondary_resets})"
        )
        return True, reason, sanitized
    if isinstance(rate_limit_type, str) and rate_limit_type.strip():
        return True, f"codex_rate_limit_reached ({rate_limit_type.strip()})", sanitized
    if spend_reached is True or ordinary_allowed is False:
        reason = (
            f"codex_usage_disallowed "
            f"(spendControlReached={spend_reached}, ordinaryUsageAllowed={ordinary_allowed})"
        )
        return True, reason, sanitized
    return False, None, sanitized


def _validate_pass_payload(
    payload: Any,
    schema: dict[str, Any],
    *,
    expected_sha: str,
    expected_lens: str,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["pass output is missing or not a JSON object"]
    errs = validate_instance_against_schema(payload, schema)
    rev_sha = payload.get("revision_sha")
    if rev_sha != expected_sha:
        errs.append(f"revision_sha mismatch ({rev_sha!r} != {expected_sha!r})")
    lens = payload.get("lens")
    if lens != expected_lens:
        errs.append(f"lens mismatch ({lens!r} != {expected_lens!r})")
    return errs


def _build_expected_provenance(
    *,
    candidate_sha: str,
    base_sha: str,
    packet_sha256: str,
    policy_sha256: str,
    schema_sha256: str,
    lens: str,
    frozen_tree_sha256: str,
) -> dict[str, str]:
    return {
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "packet_sha256": packet_sha256,
        "policy_sha256": policy_sha256,
        "schema_sha256": schema_sha256,
        "lens": lens,
        "frozen_tree_sha256": frozen_tree_sha256,
    }


def _write_pass_provenance(
    out_dir: Path,
    pass_name: str,
    expected_prov: dict[str, str],
    *,
    backend: str,
    model: str,
) -> Path:
    prov_doc = {
        **expected_prov,
        "backend": backend,
        "model": model,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    prov_path = out_dir / f"{pass_name}.provenance.json"
    prov_path.write_text(json.dumps(prov_doc, indent=2) + "\n", encoding="utf-8")
    return prov_path


def _try_load_reusable_pass(
    source_dir: Path,
    pass_name: str,
    expected_prov: dict[str, str],
    schema: dict[str, Any],
    excluded_prefixes: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pass_path = source_dir / f"{pass_name}.json"
    prov_path = source_dir / f"{pass_name}.provenance.json"
    if not pass_path.is_file() or not prov_path.is_file():
        return None
    try:
        payload = json.loads(pass_path.read_text(encoding="utf-8"))
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(prov, dict):
        return None
    val_errs = _validate_pass_payload(
        payload,
        schema,
        expected_sha=expected_prov["candidate_sha"],
        expected_lens=expected_prov["lens"],
    )
    if val_errs:
        return None
    if payload.get("verdict") == "BLOCKED":
        return None
    for key in PROVENANCE_TUPLE_KEYS:
        if prov.get(key) != expected_prov[key]:
            return None
    backend = prov.get("backend")
    model = prov.get("model")
    if backend not in {"codex", "agy"} or not isinstance(model, str) or not model.strip():
        return None
    if is_excluded_model(model, excluded_prefixes):
        return None
    return payload, prov


def _strip_json_fences(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_agy_structured_output(stdout_lines: list[str]) -> dict[str, Any] | None:
    """Parse structured review output from `agy --output-format stream-json` or `json`."""
    result_events: list[dict[str, Any]] = []
    for line in stdout_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result_events.append(event)

    if result_events:
        last_result = result_events[-1].get("result")
        if not isinstance(last_result, dict):
            return None
        status = str(last_result.get("status", "")).upper()
        if status and status != "SUCCESS":
            return None
        structured = last_result.get("structured_output")
        if isinstance(structured, dict):
            return structured
        response_text = last_result.get("response")
        if isinstance(response_text, str) and response_text.strip():
            try:
                parsed = json.loads(_strip_json_fences(response_text))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return None
        return None

    full_text = "".join(stdout_lines).strip()
    if not full_text:
        return None
    try:
        parsed_full = json.loads(_strip_json_fences(full_text))
    except json.JSONDecodeError:
        return None
    if isinstance(parsed_full, dict):
        if isinstance(parsed_full.get("structured_output"), dict):
            return parsed_full["structured_output"]
        if "revision_sha" in parsed_full and "lens" in parsed_full:
            return parsed_full
    return None


def _sanitize_archive_token(value: str) -> str:
    """Sanitize backend or model identifiers for safe pass-archive filenames."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "default"


def _archive_pass_logs(out_dir: Path, pass_name: str, tag: str) -> str:
    """Move pass artifacts aside under `<pass_name>.<tag>.<suffix>` without overwriting."""
    suffixes = (".json", ".raw.jsonl", ".stderr.log")
    existing_srcs = [s for s in suffixes if (out_dir / f"{pass_name}{s}").is_file()]
    if not existing_srcs:
        return tag
    actual_tag = tag
    seq = 1
    while any((out_dir / f"{pass_name}.{actual_tag}{s}").exists() for s in existing_srcs):
        actual_tag = f"{tag}-seq{seq}"
        seq += 1
    for suffix in existing_srcs:
        src = out_dir / f"{pass_name}{suffix}"
        dst = out_dir / f"{pass_name}.{actual_tag}{suffix}"
        src.replace(dst)
    return actual_tag


def _archive_failed_codex_logs(
    out_dir: Path, pass_names: tuple[str, ...] = ("pass1", "pass2")
) -> list[str]:
    """Preserve failed Codex pass logs before running fallback backend."""
    tags: list[str] = []
    for pass_name in pass_names:
        tags.append(_archive_pass_logs(out_dir, pass_name, "codex-failed"))
    return tags


def _git_output(repo_root: Path, args: list[str], *, timeout_sec: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return run_managed_cmd(["git", "-C", str(repo_root), *args], timeout_sec=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Git command timed out after {timeout_sec:.1f}s: git {' '.join(args)}") from exc


def resolve_commit_info(repo_root: Path, commit_ref: str, base_ref: str | None) -> dict[str, Any]:
    """Resolve immutable candidate SHA, base SHA, diff stat, and dirty status."""
    git_top = resolve_git_toplevel(repo_root)
    rev = _git_output(git_top, ["rev-parse", "--verify", f"{commit_ref}^{{commit}}"])
    if rev.returncode != 0:
        raise ValueError(f"Cannot resolve commit '{commit_ref}': {rev.stderr.strip()}")
    candidate_sha = rev.stdout.strip()

    if base_ref:
        base_rev = _git_output(git_top, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        if base_rev.returncode != 0:
            raise ValueError(f"Cannot resolve base commit '{base_ref}': {base_rev.stderr.strip()}")
        base_sha = base_rev.stdout.strip()
        diff_range = f"{base_sha}..{candidate_sha}"
    else:
        parent_rev = _git_output(git_top, ["rev-parse", "--verify", f"{candidate_sha}^"])
        if parent_rev.returncode == 0:
            base_sha = parent_rev.stdout.strip()
            diff_range = f"{base_sha}..{candidate_sha}"
        else:
            base_sha = "ROOT_COMMIT"
            diff_range = candidate_sha

    stat_proc = _git_output(git_top, ["show", "--stat", "--format=", candidate_sha])
    if base_sha != "ROOT_COMMIT":
        stat_proc = _git_output(git_top, ["diff", "--stat", diff_range])
        files_proc = _git_output(git_top, ["diff", "--name-status", diff_range])
    else:
        files_proc = _git_output(git_top, ["show", "--name-status", "--format=", candidate_sha])

    status_proc = _git_output(git_top, ["status", "--porcelain"])
    dirty_lines = [ln for ln in status_proc.stdout.splitlines() if ln.strip()]

    return {
        "git_top": str(git_top),
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "diff_stat": stat_proc.stdout.strip() or "(no diff stat)",
        "changed_files": files_proc.stdout.strip() or "(no changed files)",
        "worktree_dirty": bool(dirty_lines),
        "dirty_files": dirty_lines,
    }


def render_prompt(template_path: Path, info: dict[str, Any], task_packet_text: str) -> str:
    """Populate a Codex Room pass prompt template with immutable commit context."""
    template = template_path.read_text(encoding="utf-8")
    replacements = {
        "{{CANDIDATE_SHA}}": info["candidate_sha"],
        "{{BASE_SHA}}": info["base_sha"],
        "{{DIFF_STAT}}": info["diff_stat"],
        "{{CHANGED_FILES}}": info["changed_files"],
        "{{FROZEN_SRC_DIR}}": str(info.get("frozen_src_dir", "")),
        "{{PATCH_PATH}}": str(info.get("patch_path") or "(none — ROOT_COMMIT)"),
        "{{TASK_PACKET}}": task_packet_text,
    }
    rendered = template
    for key, val in replacements.items():
        rendered = rendered.replace(key, val)
    return rendered


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _finding_dedup_key(f: dict[str, Any]) -> tuple[str, int, str, str]:
    line_raw = f.get("line_start", 0)
    line_num = line_raw if is_strict_int(line_raw) else 0
    return (
        str(f.get("file", "")).replace("\\", "/").strip(),
        line_num,
        str(f.get("category", "")).strip().lower(),
        _normalize_title(str(f.get("title", ""))),
    )


def consolidate_reviews(
    pass1_data: dict[str, Any] | None,
    pass2_data: dict[str, Any] | None,
    expected_sha: str,
) -> dict[str, Any]:
    """Consolidate two independent review passes on ``expected_sha``."""
    errors: list[str] = []
    if not is_hex_sha40(expected_sha):
        errors.append(f"Invalid expected_sha: must be 40-char lowercase hex, got {expected_sha!r}")
    if not isinstance(pass1_data, dict):
        errors.append("Pass 1 (correctness_and_tests) output is missing or invalid JSON.")
    if not isinstance(pass2_data, dict):
        errors.append("Pass 2 (security_and_invariants) output is missing or invalid JSON.")

    if not isinstance(pass1_data, dict) or not isinstance(pass2_data, dict):
        return {
            "revision_sha": expected_sha,
            "passes_completed": int(isinstance(pass1_data, dict)) + int(isinstance(pass2_data, dict)),
            "independent_contexts": False,
            "review_gate_status": "BLOCKED",
            "blocking_open_count": 0,
            "advisory_count": 0,
            "consolidated_findings": [],
            "test_quality_issues": [],
            "unverified_areas": [],
            "errors": errors,
        }

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    p1_schema_errors = validate_instance_against_schema(pass1_data, schema, path="$.pass1")
    p2_schema_errors = validate_instance_against_schema(pass2_data, schema, path="$.pass2")
    errors.extend(p1_schema_errors)
    errors.extend(p2_schema_errors)

    p1_sha = pass1_data.get("revision_sha")
    p2_sha = pass2_data.get("revision_sha")
    if not is_hex_sha40(p1_sha):
        errors.append(f"Pass 1 revision_sha is not a valid 40-char lowercase hex SHA: {p1_sha!r}")
    elif p1_sha != expected_sha:
        errors.append(f"Pass 1 revision_sha mismatch: expected {expected_sha}, got {p1_sha!r}")

    if not is_hex_sha40(p2_sha):
        errors.append(f"Pass 2 revision_sha is not a valid 40-char lowercase hex SHA: {p2_sha!r}")
    elif p2_sha != expected_sha:
        errors.append(f"Pass 2 revision_sha mismatch: expected {expected_sha}, got {p2_sha!r}")

    if pass1_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 1 reported BLOCKED: {pass1_data.get('summary', '')}")
    if pass2_data.get("verdict") == "BLOCKED":
        errors.append(f"Pass 2 reported BLOCKED: {pass2_data.get('summary', '')}")

    lens1 = pass1_data.get("lens")
    lens2 = pass2_data.get("lens")
    if lens1 != "correctness_and_tests":
        errors.append(f"Pass 1 lens must be 'correctness_and_tests', got {lens1!r}")
    if lens2 != "security_and_invariants":
        errors.append(f"Pass 2 lens must be 'security_and_invariants', got {lens2!r}")

    independent_contexts = bool(
        lens1 == "correctness_and_tests"
        and lens2 == "security_and_invariants"
        and not p1_schema_errors
        and not p2_schema_errors
    )

    if p1_schema_errors or p2_schema_errors:
        return {
            "revision_sha": expected_sha,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "passes_completed": 2,
            "independent_contexts": False,
            "pass1_verdict": pass1_data.get("verdict"),
            "pass2_verdict": pass2_data.get("verdict"),
            "review_gate_status": "BLOCKED",
            "blocking_open_count": 0,
            "advisory_count": 0,
            "consolidated_findings": [],
            "test_quality_issues": [],
            "unverified_areas": [],
            "errors": errors,
        }

    merged_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for pass_label, payload in (("pass1", pass1_data), ("pass2", pass2_data)):
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list):
            continue
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                continue
            sev = str(raw_finding.get("severity", "")).lower()
            if sev not in SEVERITY_RANK:
                errors.append(f"{pass_label} finding {raw_finding.get('id', '?')!r} has invalid severity {sev!r}")
                continue
            key = _finding_dedup_key(raw_finding)
            is_blocking = sev in BLOCKING_SEVERITIES
            line_start = raw_finding.get("line_start") if is_strict_int(raw_finding.get("line_start")) else 0
            line_end = raw_finding.get("line_end") if is_strict_int(raw_finding.get("line_end")) else 0
            if key not in merged_by_key:
                merged_by_key[key] = {
                    "id": raw_finding.get("id", ""),
                    "severity": sev,
                    "blocking": is_blocking,
                    "status": "OPEN",
                    "detected_by": [pass_label],
                    "source_ids": [raw_finding.get("id", "")],
                    "category": raw_finding.get("category", ""),
                    "file": str(raw_finding.get("file", "")).replace("\\", "/"),
                    "line_start": line_start,
                    "line_end": line_end,
                    "title": raw_finding.get("title", ""),
                    "behavior_affected": raw_finding.get("behavior_affected", ""),
                    "trigger_condition": raw_finding.get("trigger_condition", ""),
                    "evidence": raw_finding.get("evidence", ""),
                    "verification_method": raw_finding.get("verification_method", ""),
                    "plan_ref": raw_finding.get("plan_ref", ""),
                }
            else:
                existing = merged_by_key[key]
                if pass_label not in existing["detected_by"]:
                    existing["detected_by"].append(pass_label)
                fid = raw_finding.get("id", "")
                if fid and fid not in existing["source_ids"]:
                    existing["source_ids"].append(fid)
                if SEVERITY_RANK.get(sev, 0) > SEVERITY_RANK.get(existing["severity"], 0):
                    existing["severity"] = sev
                    existing["blocking"] = sev in BLOCKING_SEVERITIES
                existing["line_end"] = max(existing["line_end"], line_end)
                ev = str(raw_finding.get("evidence", "")).strip()
                if ev and ev not in existing["evidence"]:
                    existing["evidence"] = f"{existing['evidence']}\n[{pass_label}] {ev}".strip()

    findings_list = sorted(
        merged_by_key.values(),
        key=lambda f: (-SEVERITY_RANK.get(f["severity"], 0), f["file"], f["line_start"]),
    )

    blocking_open_count = sum(1 for f in findings_list if f["blocking"] and f["status"] == "OPEN")
    advisory_count = sum(1 for f in findings_list if not f["blocking"])

    for label, payload in (("Pass 1", pass1_data), ("Pass 2", pass2_data)):
        if payload.get("verdict") == "CHANGES_REQUESTED":
            p_findings = payload.get("findings", [])
            has_blocking = any(
                isinstance(f, dict) and str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
                for f in p_findings
            )
            if not has_blocking:
                errors.append(
                    f"{label} reported CHANGES_REQUESTED without any blocking finding (critical/high/medium)."
                )

    p1_tqi = pass1_data.get("test_quality_issues") if isinstance(pass1_data.get("test_quality_issues"), list) else []
    p2_tqi = pass2_data.get("test_quality_issues") if isinstance(pass2_data.get("test_quality_issues"), list) else []
    p1_unv = pass1_data.get("unverified_areas") if isinstance(pass1_data.get("unverified_areas"), list) else []
    p2_unv = pass2_data.get("unverified_areas") if isinstance(pass2_data.get("unverified_areas"), list) else []

    test_issues = _dedup_ordered([*p1_tqi, *p2_tqi])
    unverified = _dedup_ordered([*p1_unv, *p2_unv])

    if errors:
        gate_status = "BLOCKED"
    elif blocking_open_count == 0:
        gate_status = "PASSED"
    else:
        gate_status = "CHANGES_REQUESTED"

    return {
        "revision_sha": expected_sha,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "passes_completed": 2,
        "independent_contexts": independent_contexts,
        "pass1_verdict": pass1_data.get("verdict"),
        "pass2_verdict": pass2_data.get("verdict"),
        "review_gate_status": gate_status,
        "blocking_open_count": blocking_open_count,
        "advisory_count": advisory_count,
        "consolidated_findings": findings_list,
        "test_quality_issues": test_issues,
        "unverified_areas": unverified,
        "errors": errors,
    }


def _dedup_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def render_consolidated_markdown(report: dict[str, Any]) -> str:
    """Format consolidated Codex Room review as readable markdown."""
    backend = report.get("review_backend", "codex")
    model = report.get("review_model")
    backend_label = f"`{backend}`" + (f" (`{model}`)" if model else "")
    if report.get("fallback_used"):
        reason = report.get("fallback_reason") or "codex_unavailable"
        backend_label += f" — fallback ({reason})"

    lines = [
        f"# Consolidated Codex Room Review — `{report['revision_sha'][:12]}`",
        "",
        f"- **Gate Status**: `{report['review_gate_status']}`",
        f"- **Revision SHA**: `{report['revision_sha']}`",
        f"- **Review Backend**: {backend_label}",
        (
            f"- **Passes Completed**: `{report['passes_completed']}` "
            f"(independent contexts: `{report['independent_contexts']}`)"
        ),
        f"- **Blocking Open Findings**: `{report['blocking_open_count']}`",
        f"- **Advisory Findings**: `{report['advisory_count']}`",
    ]
    if report.get("pass1_backend") or report.get("pass2_backend"):
        p1_b = report.get("pass1_backend") or "none"
        p1_m = report.get("pass1_model") or "none"
        p2_b = report.get("pass2_backend") or "none"
        p2_m = report.get("pass2_model") or "none"
        lines.append(f"- **Pass Attribution**: pass1=`{p1_b}:{p1_m}`, pass2=`{p2_b}:{p2_m}`")
    if report.get("reused_passes"):
        lines.append(f"- **Reused Passes**: `{', '.join(report['reused_passes'])}`")
    if report.get("exhausted_quota_pools"):
        pools = ", ".join(report["exhausted_quota_pools"])
        lines.append(f"- **Exhausted Quota Pools**: `{pools}`")
    lines.append("")
    if report["errors"]:
        lines.append("## Errors / Blockers")
        for err in report["errors"]:
            lines.append(f"- {err}")
        lines.append("")

    if report["consolidated_findings"]:
        lines.extend(
            [
                "## Consolidated Findings",
                "",
                "| ID | Severity | Blocking | Location | Detected By | Title | Status |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for f in report["consolidated_findings"]:
            loc = f"`{f['file']}:{f['line_start']}-{f['line_end']}`"
            det = ", ".join(f["detected_by"])
            lines.append(
                f"| {f['id']} | `{f['severity']}` | {f['blocking']} | {loc} | {det} | {f['title']} | `{f['status']}` |"
            )
        lines.append("")

    if report["test_quality_issues"]:
        lines.append("## Test Quality Issues")
        for item in report["test_quality_issues"]:
            lines.append(f"- {item}")
        lines.append("")

    if report["unverified_areas"]:
        lines.append("## Unverified Areas")
        for item in report["unverified_areas"]:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _run_subprocess_with_watchdog(
    cmd: list[str],
    *,
    pass_name: str,
    timeout_sec: float,
    idle_timeout_sec: float,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, list[str], list[str], str | None]:
    """Run a review subprocess with activity-based idle timeout and hard ceiling."""
    try:
        proc, job_handle = spawn_managed_process(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
    except RuntimeError as exc:
        return 1, [], [f"{exc}\n"], str(exc)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    activity_lock = threading.Lock()
    started_at = time.monotonic()
    last_activity = started_at

    def _touch_activity() -> None:
        nonlocal last_activity
        with activity_lock:
            last_activity = time.monotonic()

    def _write_stdin() -> None:
        if proc.stdin is None or stdin_text is None:
            return
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _pump_stream(stream: Any, bucket: list[str]) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                bucket.append(line)
                _touch_activity()
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    writer_thread = threading.Thread(target=_write_stdin, daemon=True) if stdin_text is not None else None
    stdout_thread = threading.Thread(target=_pump_stream, args=(proc.stdout, stdout_lines), daemon=True)
    stderr_thread = threading.Thread(target=_pump_stream, args=(proc.stderr, stderr_lines), daemon=True)
    if writer_thread is not None:
        writer_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    termination_reason: str | None = None
    try:
        while proc.poll() is None or stdout_thread.is_alive() or stderr_thread.is_alive():
            now = time.monotonic()
            with activity_lock:
                idle_elapsed = now - last_activity
            total_elapsed = now - started_at

            if idle_timeout_sec > 0 and idle_elapsed >= idle_timeout_sec:
                termination_reason = (
                    f"[codex_room_review] pass={pass_name} terminated: "
                    f"idle_timeout={idle_timeout_sec:.1f}s (total={total_elapsed:.1f}s)\n"
                )
                terminate_process_tree(proc, job_handle=job_handle)
                job_handle = None
                break
            if timeout_sec > 0 and total_elapsed >= timeout_sec:
                termination_reason = (
                    f"[codex_room_review] pass={pass_name} terminated: "
                    f"total_timeout={timeout_sec:.1f}s (idle={idle_elapsed:.1f}s)\n"
                )
                terminate_process_tree(proc, job_handle=job_handle)
                job_handle = None
                break
            time.sleep(0.05)

        if writer_thread is not None:
            writer_thread.join(timeout=2.0)
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
    finally:
        close_job_handle(job_handle)

    if termination_reason:
        stderr_lines.append(termination_reason)

    return proc.returncode if proc.returncode is not None else 1, stdout_lines, stderr_lines, termination_reason


def _record_pass_exec_meta(
    out_dir: Path, pass_name: str, rc: int, termination_reason: str | None
) -> None:
    with _PASS_EXEC_META_LOCK:
        _PASS_EXEC_META[(str(out_dir.resolve()), pass_name)] = (rc, termination_reason)


def _pop_pass_exec_meta(out_dir: Path, pass_name: str) -> tuple[int, str | None]:
    with _PASS_EXEC_META_LOCK:
        return _PASS_EXEC_META.pop((str(out_dir.resolve()), pass_name), (1, None))


def _run_codex_pass(
    codex_prefix: list[str],
    role: str,
    runtime_dir: Path,
    repo_root: Path,
    schema_path: Path,
    prompt_text: str,
    out_dir: Path,
    pass_name: str,
    model: str | None,
    timeout_sec: float,
    idle_timeout_sec: float = 180.0,
) -> dict[str, Any] | None:
    pass_json = out_dir / f"{pass_name}.json"
    raw_jsonl = out_dir / f"{pass_name}.raw.jsonl"
    stderr_log = out_dir / f"{pass_name}.stderr.log"

    cmd = [
        *codex_prefix,
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(repo_root),
        "--output-schema",
        str(schema_path),
        "-o",
        str(pass_json),
        "--json",
    ]
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")

    env = dict(os.environ)
    env["CODEX_HOME"] = str(runtime_dir)

    rc, stdout_lines, stderr_lines, termination_reason = _run_subprocess_with_watchdog(
        cmd,
        pass_name=pass_name,
        timeout_sec=timeout_sec,
        idle_timeout_sec=idle_timeout_sec,
        stdin_text=prompt_text,
        env=env,
    )

    raw_jsonl.write_text("".join(stdout_lines), encoding="utf-8")
    stderr_log.write_text("".join(stderr_lines), encoding="utf-8")
    _record_pass_exec_meta(out_dir, pass_name, rc, termination_reason)

    if termination_reason is not None or rc != 0 or not pass_json.is_file():
        return None
    try:
        return json.loads(pass_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _build_agy_cli_prompt(prompt_text: str, prompt_file: Path) -> str:
    """Build a strictly read-only CLI prompt for `agy -p`, avoiding Windows argv limits."""
    readonly_prefix = (
        "STRICT READ-ONLY INDEPENDENT REVIEW PASS (HEADLESS MODE). "
        "Do NOT create, edit, delete, or modify any files, do NOT spawn subagents, "
        "and NEVER invoke any shell, terminal, or command execution tool (headless mode auto-denies all shell commands). "
        "Use ONLY built-in read-only file/search tools (read_file, grep/search, find/list); "
        "all commit SHA, diff stat, and changed-file metadata are already provided in the prompt. "
        "Return ONLY valid JSON matching the provided schema.\n\n"
    )
    if len(prompt_text) <= AGY_INLINE_PROMPT_CHAR_LIMIT:
        return readonly_prefix + prompt_text
    return (
        readonly_prefix
        + f"Read and execute the complete review instructions from `{prompt_file}` using ONLY read-only file/search tools "
        + "(never call shell/command tools), then return ONLY the final JSON object matching the provided schema."
    )


def _run_agy_pass(
    agy_prefix: list[str],
    role: str,
    repo_root: Path,
    schema_path: Path,
    prompt_text: str,
    prompt_file: Path,
    out_dir: Path,
    pass_name: str,
    model: str,
    timeout_sec: float,
    idle_timeout_sec: float = 180.0,
) -> dict[str, Any] | None:
    """Run one isolated read-only review pass via Antigravity CLI (`agy`)."""
    pass_json = out_dir / f"{pass_name}.json"
    raw_jsonl = out_dir / f"{pass_name}.raw.jsonl"
    stderr_log = out_dir / f"{pass_name}.stderr.log"

    cli_prompt = _build_agy_cli_prompt(prompt_text, prompt_file)
    cmd = [
        *agy_prefix,
        "--sandbox",
        "--mode",
        "plan",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--json-schema",
        str(schema_path),
        "-p",
        cli_prompt,
    ]

    rc, stdout_lines, stderr_lines, termination_reason = _run_subprocess_with_watchdog(
        cmd,
        pass_name=f"{pass_name}:agy",
        timeout_sec=timeout_sec,
        idle_timeout_sec=idle_timeout_sec,
        stdin_text=None,
        cwd=repo_root,
    )

    raw_jsonl.write_text("".join(stdout_lines), encoding="utf-8")
    stderr_log.write_text("".join(stderr_lines), encoding="utf-8")
    _record_pass_exec_meta(out_dir, pass_name, rc, termination_reason)

    if termination_reason is not None or rc != 0:
        return None
    parsed = _extract_agy_structured_output(stdout_lines)
    if not isinstance(parsed, dict):
        return None
    pass_json.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    return parsed


def _execute_two_passes(
    runner: Any,
    pass1_kwargs: dict[str, Any],
    pass2_kwargs: dict[str, Any],
    *,
    sequential: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if sequential:
        return runner(**pass1_kwargs), runner(**pass2_kwargs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(runner, **pass1_kwargs)
        fut2 = pool.submit(runner, **pass2_kwargs)
        return fut1.result(), fut2.result()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run 2-pass Codex Room independent code review.")
    parser.add_argument("--commit", default="HEAD", help="Immutable commit SHA or ref to review.")
    parser.add_argument("--base", default=None, help="Optional base commit SHA for diff context.")
    parser.add_argument("--task-id", default="adhoc", help="Task identifier for run folder naming.")
    parser.add_argument(
        "--attempt-id",
        default=None,
        help="Optional unique attempt ID for write-once run dir.",
    )
    parser.add_argument("--task-packet", type=Path, default=None, help="Path to Task Packet md.")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Optional task ledger.json path for G4 cap & budget enforcement.",
    )
    parser.add_argument(
        "--require-ledger",
        action="store_true",
        help="Fail closed (BLOCKED, exit 2) if ledger.json cannot be resolved.",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help="Prior review output directory to reuse provenance-verified passes from.",
    )
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".codex-runtime")
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Optional explicit model override for active backend.",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "codex", "agy"),
        default="auto",
        help=(
            "Review backend: 'auto' (prioritize Codex CLI, fallback to qualified_routes "
            "on confirmed quota exhaustion), 'codex' (Codex CLI only), or 'agy' (AGY CLI)."
        ),
    )
    parser.add_argument(
        "--fallback-model",
        type=str,
        default=None,
        help=(
            "Explicit Antigravity CLI fallback model override (bypasses "
            "qualified_routes when set on --backend auto)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1200.0,
        help="Hard wall-clock safety ceiling per pass in seconds (0 disables). Default: 1200.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=180.0,
        help=(
            "Inactivity watchdog timeout per pass in seconds; resets on stdout/stderr "
            "activity (0 disables). Default: 180."
        ),
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run pass1 and pass2 sequentially instead of in parallel.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--consolidate-only",
        nargs=2,
        metavar=("PASS1_JSON", "PASS2_JSON"),
        type=Path,
        default=None,
    )
    parser.add_argument("--fail-on-blocking", action="store_true")
    return parser


def _merge_prior_pass_findings(
    fallback_payload: dict[str, Any] | None,
    prior_payload: dict[str, Any] | None,
    expected_sha: str,
) -> dict[str, Any] | None:
    """Preserve findings/issues from a completed Codex pass when falling back to agy."""
    if not isinstance(prior_payload, dict):
        return fallback_payload
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if validate_instance_against_schema(prior_payload, schema):
        return prior_payload
    if str(prior_payload.get("revision_sha", "")).strip() != expected_sha:
        return prior_payload
    if prior_payload.get("verdict") == "BLOCKED":
        return prior_payload

    prior_findings = list(prior_payload.get("findings", []))
    prior_has_blocking = any(
        str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
        for f in prior_findings
        if isinstance(f, dict)
    )
    if prior_payload.get("verdict") == "CHANGES_REQUESTED" and not prior_has_blocking:
        return prior_payload
    if not isinstance(fallback_payload, dict):
        return prior_payload

    merged = dict(fallback_payload)
    fb_findings = list(fallback_payload.get("findings", []))
    merged["findings"] = [*prior_findings, *fb_findings]
    merged["test_quality_issues"] = _dedup_ordered(
        [*prior_payload.get("test_quality_issues", []), *fallback_payload.get("test_quality_issues", [])]
    )
    merged["unverified_areas"] = _dedup_ordered(
        [*prior_payload.get("unverified_areas", []), *fallback_payload.get("unverified_areas", [])]
    )
    has_blocking = any(
        str(f.get("severity", "medium")).lower() in BLOCKING_SEVERITIES
        for f in merged["findings"]
    )
    if merged.get("verdict") == "APPROVE" and has_blocking:
        merged["verdict"] = "CHANGES_REQUESTED"
    return merged


def _write_once_outputs(out_dir: Path, report: dict[str, Any]) -> bool:
    """Write consolidated-review.json and .md exclusively, refusing overwrite."""
    json_path = out_dir / "consolidated-review.json"
    md_path = out_dir / "consolidated-review.md"
    try:
        with json_path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(report, indent=2) + "\n")
    except FileExistsError:
        print(
            f"[codex_room_review] Write-once refusal: {json_path} already exists.",
            file=sys.stderr,
        )
        return False
    with md_path.open("x", encoding="utf-8") as fh:
        fh.write(render_consolidated_markdown(report))
    return True


def _call_verify_role(role: str, runtime_root: Path, repo_root: Path) -> dict[str, Any]:
    sig = inspect.signature(verify_role)
    has_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "repo_root"
        for p in sig.parameters.values()
    )
    if has_kw:
        return verify_role(role, runtime_root, repo_root=repo_root)
    return verify_role(role, runtime_root)


def _should_enforce_g4_caps(
    args: argparse.Namespace,
    out_dir: Path,
    git_top: Path,
) -> bool:
    if args.dry_run or args.consolidate_only is not None:
        return False
    packet_candidate = args.task_packet or (
        git_top / "harness" / "runs" / args.task_id / "packet.md"
    )
    is_exempt_id = args.task_id == "adhoc" or str(args.task_id).startswith("canary-")
    if (
        args.ledger is None
        and not args.require_ledger
        and is_exempt_id
        and out_dir.parent.name != "attempts"
        and task_runner.extract_root_task_id(task_packet=packet_candidate) is None
        and task_runner.find_ledger_path_for_task(
            args.task_id, repo_root=git_top, task_packet=packet_candidate
        )
        is None
    ):
        return False
    return True


def _resolve_g4_ledger_path(args: argparse.Namespace, git_top: Path) -> Path | None:
    if args.ledger is not None:
        return args.ledger.resolve()
    packet_candidate = args.task_packet or (
        git_top / "harness" / "runs" / args.task_id / "packet.md"
    )
    return task_runner.find_ledger_path_for_task(
        args.task_id, repo_root=git_top, task_packet=packet_candidate
    )


def _seed_tracker_from_union(
    tracker: dict[str, Any], union: dict[str, Any], now_iso: str
) -> bool:
    """Snapshot legacy residuals, seed discovered attempt dirs, and recompute counts."""
    changed = False
    imp_rounds = task_runner._extract_legacy_rounds_by_source(tracker)
    imp_sha = task_runner._extract_legacy_nested_by_source(
        tracker,
        imported_key="imported_legacy_sha",
        counts_key="sha_invocations",
        keyed_fn=task_runner._keyed_tracker_sha_dirs,
    )
    if imp_rounds and tracker.get("imported_legacy_rounds") != imp_rounds:
        tracker["imported_legacy_rounds"] = imp_rounds
        changed = True
    if imp_sha and tracker.get("imported_legacy_sha") != imp_sha:
        tracker["imported_legacy_sha"] = imp_sha
        changed = True

    completed_map = tracker.setdefault("completed_attempts", {})
    for norm_dir, meta in union.get("discovered_entries", {}).items():
        existing = completed_map.get(norm_dir)
        existing_st = existing.get("status") if isinstance(existing, dict) else None
        if existing is None or (
            existing_st == "IN_PROGRESS" and meta.get("status") != "IN_PROGRESS"
        ):
            completed_map[norm_dir] = {
                "candidate_sha": meta.get("candidate_sha"),
                "status": meta.get("status"),
                "completed_at_utc": now_iso,
                "seeded_from_fs": True,
            }
            changed = True

    inv_map = tracker.setdefault("sha_invocations", {})
    all_shas: set[str] = {str(k) for k in inv_map}
    for sec in ("completed_attempts", "attempts", "in_flight"):
        sec_map = tracker.get(sec)
        if isinstance(sec_map, dict):
            for ent in sec_map.values():
                if isinstance(ent, dict) and isinstance(ent.get("candidate_sha"), str):
                    all_shas.add(ent["candidate_sha"])
    for sub in imp_sha.values():
        all_shas.update(sub.keys())
    for sha_key in sorted(all_shas):
        target_cnt = len(task_runner._keyed_tracker_sha_dirs(tracker, sha_key)) + sum(
            sub.get(sha_key, 0) for sub in imp_sha.values()
        )
        if int(inv_map.get(sha_key, 0)) < target_cnt:
            inv_map[sha_key] = target_cnt
            changed = True

    target_rounds = len(task_runner._native_tracker_round_dirs(tracker)) + sum(
        imp_rounds.values()
    )
    if int(tracker.get("completed_rounds", 0)) < target_rounds:
        tracker["completed_rounds"] = target_rounds
        changed = True
    return changed


def _preflight_g4_caps(
    args: argparse.Namespace,
    out_dir: Path,
    git_top: Path,
    candidate_sha: str,
    ledger_path: Path | None,
) -> tuple[int | None, Path | None, str | None]:
    """Enforce tracker, attempts/, and ledger.json G4 ceilings + atomic reservation."""
    if args.require_ledger and (ledger_path is None or not ledger_path.is_file()):
        print(
            f"[codex_room_review] BLOCKED: Missing required ledger.json for {args.task_id!r}.",
            file=sys.stderr,
        )
        return 2, None, None
    if args.ledger is not None and (ledger_path is None or not ledger_path.is_file()):
        print(
            f"[codex_room_review] BLOCKED: Ledger not found at {args.ledger}.",
            file=sys.stderr,
        )
        return 2, None, None

    packet_candidate = args.task_packet or (
        git_top / "harness" / "runs" / args.task_id / "packet.md"
    )
    expected_root = task_runner.extract_root_task_id(task_packet=packet_candidate)
    tracker_id = expected_root or args.task_id
    if ledger_path is not None and ledger_path.is_file():
        raw_ledger = task_runner._read_optional_json_dict(ledger_path)
        if raw_ledger is None:
            print(
                f"[codex_room_review] BLOCKED: Invalid JSON in ledger.json at {ledger_path}.",
                file=sys.stderr,
            )
            return 2, None, None
        if raw_ledger.get("root_task_id"):
            bind_err = task_runner.validate_ledger_task_binding(
                raw_ledger,
                task_id=args.task_id,
                expected_root_task_id=expected_root,
            )
            if bind_err:
                print(f"[codex_room_review] BLOCKED: {bind_err}", file=sys.stderr)
                return 2, None, None
            tracker_id = str(raw_ledger["root_task_id"])

    try:
        task_runner._validate_safe_task_id(tracker_id, "root_task_id")
        tracker_path = task_runner.default_g4_tracker_path(tracker_id, repo_root=git_top)
    except ValueError as exc:
        print(f"[codex_room_review] BLOCKED: {exc}", file=sys.stderr)
        return 2, None, None

    policy_cb = task_runner.load_circuit_breakers(repo_root=git_top)
    max_g4_root = int(policy_cb["max_g4_rounds_per_root_task"])
    max_g4_pkt = int(policy_cb["max_g4_rounds_per_packet_version"])
    max_g4_sha = int(policy_cb["max_g4_invocations_per_candidate_sha"])
    packet_epoch = task_runner.extract_packet_epoch(packet_candidate)

    attempt_roots = [git_top / "harness" / "runs" / args.task_id / "attempts"]
    if tracker_id != args.task_id:
        attempt_roots.append(git_top / "harness" / "runs" / tracker_id / "attempts")
    if out_dir.parent.name == "attempts":
        attempt_roots.append(out_dir.parent)

    halted_states = {
        "PAUSED_BUDGET",
        "NEEDS_REPLAN",
        "BLOCKED",
        "BLOCKED_BRIEF",
        "BLOCKED_AUTH",
        "BLOCKED_INFRA",
        "VERIFIED",
    }
    reservation_id = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    now_iso = datetime.now(timezone.utc).isoformat()

    def _check_and_reserve_locked() -> tuple[int | None, Path | None, str | None]:
        try:
            tracker = task_runner.load_g4_tracker(tracker_path, task_id=tracker_id)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            print(
                f"[codex_room_review] BLOCKED: Invalid root g4-tracker.json "
                f"at {tracker_path}: {exc}",
                file=sys.stderr,
            )
            return 2, None, None

        extra_trackers: list[dict[str, Any]] = []
        if tracker_id != args.task_id:
            child_t_path = task_runner.default_g4_tracker_path(
                args.task_id, repo_root=git_top
            )
            if child_t_path.is_file():
                try:
                    extra_trackers.append(
                        task_runner.load_g4_tracker(child_t_path, task_id=args.task_id)
                    )
                except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    print(
                        f"[codex_room_review] BLOCKED: Invalid child g4-tracker.json "
                        f"at {child_t_path}: {exc}",
                        file=sys.stderr,
                    )
                    return 2, None, None

        run_attempt_roots = list(attempt_roots)
        for sib_dir in task_runner.discover_child_runs_for_root(
            tracker_id, repo_root=git_top, exclude_ids={tracker_id, args.task_id}
        ):
            sib_attempts = sib_dir / "attempts"
            if sib_attempts not in run_attempt_roots:
                run_attempt_roots.append(sib_attempts)
            sib_t_path = sib_dir / "g4-tracker.json"
            if sib_t_path.is_file():
                try:
                    extra_trackers.append(
                        task_runner.load_g4_tracker(sib_t_path, task_id=sib_dir.name)
                    )
                except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    print(
                        f"[codex_room_review] BLOCKED: Invalid sibling g4-tracker.json "
                        f"at {sib_t_path}: {exc}",
                        file=sys.stderr,
                    )
                    return 2, None, None

        any_extra_in_flight = any(
            isinstance(et, dict) and bool(et.get("in_flight")) for et in extra_trackers
        )
        merged = tracker
        for extra_t in extra_trackers:
            merged_next = task_runner.merge_g4_trackers(
                merged, extra_t, include_in_flight=False
            )
            if merged_next is not None:
                merged = merged_next
        merged["task_id"] = tracker_id
        tracker = merged

        union = task_runner.collect_g4_attempt_union(
            run_attempt_roots, tracker, candidate_sha=candidate_sha
        )
        prior_sha_statuses = {st for st in union["sha_statuses"].values() if st}
        if (
            tracker.get("in_flight")
            or any_extra_in_flight
            or "IN_PROGRESS" in prior_sha_statuses
        ):
            print(
                "[codex_room_review] BLOCKED: Active G4 review already in flight "
                f"for task {args.task_id!r}.",
                file=sys.stderr,
            )
            return 2, None, None

        seeded = _seed_tracker_from_union(tracker, union, now_iso)
        if (
            tracker_path.is_file()
            or bool(extra_trackers)
            or seeded
            or bool(union.get("discovered_entries"))
        ):
            task_runner.save_g4_tracker(tracker_path, tracker)
        effective_rounds = int(union["total_rounds"])
        t_sha = int(tracker.get("sha_invocations", {}).get(candidate_sha, 0))
        effective_sha = max(int(union["sha_count"]), t_sha)
        non_infra_statuses = {st for st in prior_sha_statuses if st != "BLOCKED_INFRA"}
        norm_out = task_runner._norm_attempt_dir(out_dir)

        resume_err: str | None = None
        if args.resume_from is not None:
            resume_rev_path = Path(args.resume_from).resolve() / "consolidated-review.json"
            resume_rev = task_runner._read_optional_json_dict(resume_rev_path)
            if resume_rev is None:
                resume_err = "missing or invalid consolidated-review.json"
            else:
                rev_sha = resume_rev.get("revision_sha")
                rev_status = str(resume_rev.get("review_gate_status") or "")
                if rev_sha != candidate_sha:
                    resume_err = f"revision_sha {rev_sha!r} != {candidate_sha[:12]}"
                elif rev_status != "BLOCKED_INFRA":
                    resume_err = f"review_gate_status={rev_status!r} != 'BLOCKED_INFRA'"

        if ledger_path is None:
            if tracker.get("in_flight") or "IN_PROGRESS" in prior_sha_statuses:
                print(
                    "[codex_room_review] BLOCKED: Active G4 review already in flight "
                    f"for task {args.task_id!r}.",
                    file=sys.stderr,
                )
                return 2, None, None
            if effective_rounds >= max_g4_root:
                print(
                    f"[codex_room_review] PAUSED_BUDGET: G4 round cap reached "
                    f"({effective_rounds} >= {max_g4_root}).",
                    file=sys.stderr,
                )
                return 2, None, None
            pkt_rounds = task_runner.count_tracker_packet_rounds(tracker, packet_epoch)
            if packet_epoch is not None and pkt_rounds >= max_g4_pkt:
                print(
                    f"[codex_room_review] NEEDS_REPLAN: G4 packet cap reached "
                    f"(packet_rounds={pkt_rounds} >= {max_g4_pkt}).",
                    file=sys.stderr,
                )
                return 2, None, None
            if effective_sha >= max_g4_sha or (effective_sha >= 1 and args.resume_from is None):
                print(
                    f"[codex_room_review] BLOCKED: Duplicate G4 invocation for candidate_sha "
                    f"{candidate_sha[:12]} (invocations={effective_sha}).",
                    file=sys.stderr,
                )
                return 2, None, None
            if args.resume_from is not None and effective_sha >= 1 and (
                non_infra_statuses or resume_err is not None
            ):
                print(
                    f"[codex_room_review] BLOCKED: Cannot resume G4 for candidate_sha "
                    f"{candidate_sha[:12]}; only BLOCKED_INFRA attempts may resume.",
                    file=sys.stderr,
                )
                return 2, None, None
            slot_meta: dict[str, Any] = {
                "candidate_sha": candidate_sha,
                "out_dir": norm_out,
                "pid": os.getpid(),
                "reserved_at_utc": now_iso,
            }
            if packet_epoch is not None:
                slot_meta["packet_epoch"] = packet_epoch
            tracker["in_flight"][reservation_id] = slot_meta
            tracker["sha_invocations"][candidate_sha] = effective_sha + 1
            task_runner.save_g4_tracker(tracker_path, tracker)
            return None, tracker_path, reservation_id

        try:
            ledger = task_runner.load_ledger(ledger_path, repo_root=git_top)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            print(
                f"[codex_room_review] BLOCKED: Invalid ledger.json at {ledger_path}: {exc}",
                file=sys.stderr,
            )
            return 2, None, None
        locked_bind_err = task_runner.validate_ledger_task_binding(
            ledger, task_id=args.task_id, expected_root_task_id=expected_root
        )
        if locked_bind_err:
            print(f"[codex_room_review] BLOCKED: {locked_bind_err}", file=sys.stderr)
            return 2, None, None
        cb = ledger.get("circuit_breakers", {})
        l_max_root = int(cb.get("max_g4_rounds_per_root_task", max_g4_root))
        l_max_pkt = int(
            cb.get(
                "max_g4_rounds_per_packet_version",
                max_g4_pkt,
            )
        )
        l_max_sha = int(cb.get("max_g4_invocations_per_candidate_sha", max_g4_sha))

        ledger_active = ledger.get("active_g4_reservations", {})
        if (
            tracker.get("in_flight")
            or any_extra_in_flight
            or ledger_active
            or "IN_PROGRESS" in prior_sha_statuses
        ):
            print(
                "[codex_room_review] BLOCKED: Active G4 review already in flight "
                f"for task {args.task_id!r}.",
                file=sys.stderr,
            )
            return 2, None, None

        ledger_in_flight = len(ledger_active)
        g4_total = int(ledger.get("g4_rounds_total", 0))
        if g4_total >= l_max_root or effective_rounds >= l_max_root:
            if ledger_in_flight == 0 and ledger["state"] != "VERIFIED":
                ledger["state"] = "PAUSED_BUDGET"
                task_runner._append_event(
                    ledger,
                    "G4_ROUND_CAP_PAUSED",
                    {
                        "g4_rounds_total": g4_total,
                        "effective_rounds": effective_rounds,
                        "cap": l_max_root,
                    },
                )
                task_runner.save_ledger(ledger_path, ledger)
            print(
                f"[codex_room_review] PAUSED_BUDGET: G4 round cap reached "
                f"(ledger={g4_total}, effective={effective_rounds}, cap={l_max_root}).",
                file=sys.stderr,
            )
            return 2, None, None

        g4_pkt = int(ledger.get("g4_rounds_in_packet", 0))
        tracker_pkt = (
            task_runner.count_tracker_packet_rounds(tracker, packet_epoch)
            if packet_epoch is not None
            else 0
        )
        effective_pkt = max(g4_pkt, tracker_pkt)
        if effective_pkt >= l_max_pkt:
            if ledger_in_flight == 0 and ledger["state"] not in {"PAUSED_BUDGET", "VERIFIED"}:
                ledger["state"] = "NEEDS_REPLAN"
                task_runner._append_event(
                    ledger,
                    "G4_PACKET_CAP_REPLAN",
                    {
                        "g4_rounds_in_packet": g4_pkt,
                        "tracker_packet_rounds": tracker_pkt,
                        "cap": l_max_pkt,
                    },
                )
                task_runner.save_ledger(ledger_path, ledger)
            print(
                f"[codex_room_review] NEEDS_REPLAN: G4 packet cap reached "
                f"(ledger={g4_pkt}, tracker={tracker_pkt}, cap={l_max_pkt}).",
                file=sys.stderr,
            )
            return 2, None, None

        inv_map = ledger.setdefault("g4_invocations_by_sha", {})
        sha_count = max(int(inv_map.get(candidate_sha, 0)), effective_sha)
        last_status = ledger.get("last_g4_status")
        allow_resume = (
            ledger["state"] == "BLOCKED_INFRA"
            and last_status == "BLOCKED_INFRA"
            and args.resume_from is not None
            and ledger.get("last_g4_candidate_sha") == candidate_sha
            and sha_count < l_max_sha
            and not non_infra_statuses
            and resume_err is None
        )
        if ledger["state"] in (halted_states - {"BLOCKED_INFRA"}):
            print(
                f"[codex_room_review] BLOCKED: Ledger state is {ledger['state']!r}; G4 refused.",
                file=sys.stderr,
            )
            return 2, None, None
        if sha_count >= l_max_sha or (sha_count >= 1 and args.resume_from is None):
            print(
                f"[codex_room_review] BLOCKED: Duplicate G4 invocation for candidate_sha "
                f"{candidate_sha[:12]} (count={sha_count}).",
                file=sys.stderr,
            )
            return 2, None, None
        if ledger["state"] == "BLOCKED_INFRA" and not allow_resume:
            print(
                f"[codex_room_review] BLOCKED: Ledger state is {ledger['state']!r}; G4 refused.",
                file=sys.stderr,
            )
            return 2, None, None
        if not allow_resume and (
            ledger["state"] != "REVIEW_FROZEN_G4" or ledger.get("candidate_sha") != candidate_sha
        ):
            print(
                f"[codex_room_review] BLOCKED: Ledger state/SHA mismatch "
                f"(state={ledger['state']!r}, expected={ledger.get('candidate_sha')!r}).",
                file=sys.stderr,
            )
            return 2, None, None

        if args.resume_from is not None and sha_count >= 1 and not allow_resume:
            print(
                f"[codex_room_review] BLOCKED: Cannot resume G4 for candidate_sha "
                f"{candidate_sha[:12]}; only BLOCKED_INFRA attempts may resume.",
                file=sys.stderr,
            )
            return 2, None, None

        spent = float(ledger.get("spent_api_equiv_usd", 0.0))
        reserved = task_runner._active_reserved_usd(ledger)
        committed = round(spent + reserved, 8)
        limit = float(ledger.get("budget_limit_usd", 0.0))
        if limit > 0 and committed > round(limit, 8):
            ledger["state"] = "PAUSED_BUDGET"
            task_runner._append_event(
                ledger,
                "BUDGET_EXCEEDED_G4_PREFLIGHT",
                {
                    "spent_usd": spent,
                    "reserved_usd": reserved,
                    "committed_usd": committed,
                    "limit_usd": limit,
                },
            )
            task_runner.save_ledger(ledger_path, ledger)
            print(
                f"[codex_room_review] PAUSED_BUDGET: Budget exceeded "
                f"(${committed:.4f} committed = ${spent:.4f} spent + "
                f"${reserved:.4f} reserved > ${limit:.4f}).",
                file=sys.stderr,
            )
            return 2, None, None

        slot_meta = {
            "candidate_sha": candidate_sha,
            "out_dir": norm_out,
            "pid": os.getpid(),
            "reserved_at_utc": now_iso,
        }
        if packet_epoch is not None:
            slot_meta["packet_epoch"] = packet_epoch
        tracker["in_flight"][reservation_id] = slot_meta
        tracker["sha_invocations"][candidate_sha] = sha_count + 1
        task_runner.save_g4_tracker(tracker_path, tracker)

        active_g4 = ledger.setdefault("active_g4_reservations", {})
        active_g4[reservation_id] = {
            "candidate_sha": candidate_sha,
            "out_dir": norm_out,
            "packet_version": int(ledger.get("packet_version", 1)),
            "task_id": str(ledger.get("task_id", args.task_id)),
            "base_sha": str(ledger.get("base_sha", "")),
            "reserved_at_utc": now_iso,
        }
        inv_map[candidate_sha] = sha_count + 1
        ledger["last_g4_candidate_sha"] = candidate_sha
        task_runner._append_event(
            ledger,
            "G4_INVOCATION_RESERVED",
            {"candidate_sha": candidate_sha, "invocation": sha_count + 1},
        )
        task_runner.save_ledger(ledger_path, ledger)
        return None, tracker_path, reservation_id

    with task_runner._ledger_lock(tracker_path):
        if ledger_path is not None:
            with task_runner._ledger_lock(ledger_path):
                return _check_and_reserve_locked()
        return _check_and_reserve_locked()


def _safe_nonneg_int(val: Any) -> int:
    return int(val) if is_strict_int(val) and val > 0 else 0


def _extract_codex_usage_deltas(raw_path: Path) -> list[tuple[int, int, int]]:
    """Extract per-turn (uncached_in, cache_read, output_tokens) deltas from Codex JSONL."""
    if not raw_path.is_file():
        return []
    deltas: list[tuple[int, int, int]] = []
    for line in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "turn.completed":
            continue
        usage = ev.get("usage")
        if not isinstance(usage, dict):
            continue
        raw_in = _safe_nonneg_int(usage.get("input_tokens"))
        cache_in = max(
            _safe_nonneg_int(usage.get("cached_input_tokens")),
            _safe_nonneg_int(usage.get("cache_read_tokens")),
        )
        uncached_in = max(0, raw_in - cache_in)
        out_tok = _safe_nonneg_int(usage.get("output_tokens"))
        if uncached_in or cache_in or out_tok:
            deltas.append((uncached_in, cache_in, out_tok))
    return deltas


def _extract_agy_cumulative_usage(
    raw_path: Path,
) -> tuple[str | None, str | None, int, int, int]:
    """Extract (conversation_id, model, input_tokens, cache_read, output_tokens) from AGY."""
    if not raw_path.is_file():
        return None, None, 0, 0, 0
    conv_id: str | None = None
    model_name: str | None = None
    max_in = 0
    max_cache = 0
    max_out = 0
    for line in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if isinstance(ev.get("conversation_id"), str) and ev["conversation_id"].strip():
            conv_id = ev["conversation_id"].strip()
        if isinstance(ev.get("model"), str) and ev["model"].strip():
            model_name = ev["model"].strip()
        res = ev.get("result") if isinstance(ev.get("result"), dict) else None
        if res:
            if isinstance(res.get("conversation_id"), str) and res["conversation_id"].strip():
                conv_id = res["conversation_id"].strip()
            if isinstance(res.get("model"), str) and res["model"].strip():
                model_name = res["model"].strip()
        usage = (
            res.get("usage")
            if (res and isinstance(res.get("usage"), dict))
            else ev.get("usage")
        )
        if not isinstance(usage, dict):
            continue
        max_in = max(max_in, _safe_nonneg_int(usage.get("input_tokens")))
        max_cache = max(
            max_cache,
            _safe_nonneg_int(usage.get("cache_read_tokens")),
            _safe_nonneg_int(usage.get("cached_input_tokens")),
        )
        max_out = max(max_out, _safe_nonneg_int(usage.get("output_tokens")))
    return conv_id, model_name, max_in, max_cache, max_out


def _extract_raw_log_tag(raw_path: Path, pass_name: str) -> str:
    """Extract archive tag from `<pass_name>[.<tag>].raw.jsonl` without dot splitting."""
    prefix = f"{pass_name}."
    suffix = ".raw.jsonl"
    name = raw_path.name
    if name.startswith(prefix) and name.endswith(suffix) and len(name) > len(prefix) + len(suffix):
        return name[len(prefix) : -len(suffix)]
    return ""


def _resolve_raw_log_model(
    raw_path: Path,
    *,
    pass_name: str,
    is_codex: bool,
    pass_state: PassState,
    active_backend: str,
    active_model: str | None,
    primary_model: str | None,
    route_models_by_tag: dict[str, str],
) -> str | None:
    tag = _extract_raw_log_tag(raw_path, pass_name)
    if tag and tag in route_models_by_tag:
        return route_models_by_tag[tag]
    if is_codex:
        if pass_state.backend == "codex" and pass_state.model:
            return pass_state.model
        if active_backend == "codex" and active_model:
            return active_model
        return primary_model or EXPECTED_REVIEW_MODEL
    if pass_state.backend == "agy" and pass_state.model:
        return pass_state.model
    return active_model if active_backend == "agy" else None


def _first_priceable_model(candidates: list[str | None]) -> str | None:
    seen: set[str] = set()
    for cand in candidates:
        if not isinstance(cand, str) or not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            task_runner._resolve_pricing(cand)
            return cand
        except ValueError:
            continue
    return None


def _plan_raw_log_billing(
    *,
    out_dir: Path,
    states: dict[str, PassState],
    reused_passes: list[str],
    active_backend: str,
    active_model: str | None,
    primary_model: str | None,
    route_models_by_tag: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    bill_entries: list[dict[str, Any]] = []
    unbillable_errors: list[str] = []
    for pass_name in ("pass1", "pass2"):
        if pass_name in reused_passes:
            continue
        p_state = states[pass_name]
        for raw_path in sorted(out_dir.glob(f"{pass_name}*.raw.jsonl")):
            if p_state.is_complete and _raw_log_has_malformed_usage(raw_path):
                unbillable_errors.append(
                    f"Unbillable malformed token usage in {raw_path.name}."
                )
            codex_deltas = _extract_codex_usage_deltas(raw_path)
            if codex_deltas:
                c_model = _resolve_raw_log_model(
                    raw_path,
                    pass_name=pass_name,
                    is_codex=True,
                    pass_state=p_state,
                    active_backend=active_backend,
                    active_model=active_model,
                    primary_model=primary_model,
                    route_models_by_tag=route_models_by_tag,
                )
                priced = _first_priceable_model([c_model])
                if priced is None:
                    unbillable_errors.append(
                        f"Unbillable Codex token usage in {raw_path.name}: "
                        f"no pricing for model {c_model!r}."
                    )
                    continue
                bill_entries.append({"kind": "codex", "model": priced, "deltas": codex_deltas})
                continue

            conv_id, ev_model, a_in, a_cache, a_out = _extract_agy_cumulative_usage(raw_path)
            if not (a_in or a_cache or a_out):
                continue
            route_model = _resolve_raw_log_model(
                raw_path,
                pass_name=pass_name,
                is_codex=False,
                pass_state=p_state,
                active_backend=active_backend,
                active_model=active_model,
                primary_model=primary_model,
                route_models_by_tag=route_models_by_tag,
            )
            priced_agy = _first_priceable_model([ev_model, route_model])
            if priced_agy is None:
                unbillable_errors.append(
                    f"Unbillable AGY token usage in {raw_path.name}: "
                    f"no pricing for event={ev_model!r} or route={route_model!r}."
                )
                continue
            bill_entries.append(
                {
                    "kind": "agy",
                    "model": priced_agy,
                    "a_in": a_in,
                    "a_cache": a_cache,
                    "a_out": a_out,
                    "conv_id": conv_id,
                }
            )
    return bill_entries, unbillable_errors


def _bill_pass_raw_logs(
    ledger_path: Path,
    *,
    out_dir: Path,
    states: dict[str, PassState],
    reused_passes: list[str],
    active_backend: str,
    active_model: str | None,
    primary_model: str | None,
    route_models_by_tag: dict[str, str],
) -> list[str]:
    bill_entries, unbillable_errors = _plan_raw_log_billing(
        out_dir=out_dir,
        states=states,
        reused_passes=reused_passes,
        active_backend=active_backend,
        active_model=active_model,
        primary_model=primary_model,
        route_models_by_tag=route_models_by_tag,
    )
    for item in bill_entries:
        if item["kind"] == "codex":
            for u_in, u_cache, u_out in item["deltas"]:
                task_runner.record_usage(
                    ledger_path,
                    model=item["model"],
                    input_tokens=u_in,
                    output_tokens=u_out,
                    thinking_tokens=0,
                    cache_read_tokens=u_cache,
                    counter_mode="delta",
                )
        else:
            task_runner.record_usage(
                ledger_path,
                model=item["model"],
                input_tokens=item["a_in"],
                output_tokens=item["a_out"],
                thinking_tokens=0,
                cache_read_tokens=item["a_cache"],
                conversation_id=item["conv_id"],
                counter_mode="cumulative",
            )
    return unbillable_errors


def _has_prior_blocking_review(
    attempt_roots: list[Path],
    candidate_sha: str,
    *,
    resume_dir: Path | None = None,
) -> bool:
    """Inspect historical consolidated-review.json files for prior blocking findings."""
    candidate_dirs: list[Path] = []
    for root_dir in attempt_roots:
        if root_dir.is_dir():
            candidate_dirs.extend(sorted(p for p in root_dir.iterdir() if p.is_dir()))
    if resume_dir is not None and resume_dir.is_dir():
        candidate_dirs.append(resume_dir)
    for d_path in candidate_dirs:
        rev = task_runner._read_optional_json_dict(d_path / "consolidated-review.json")
        if not isinstance(rev, dict):
            continue
        if str(rev.get("revision_sha", "")).strip() != candidate_sha:
            continue
        if rev.get("pass1_verdict") == "CHANGES_REQUESTED":
            return True
        if rev.get("pass2_verdict") == "CHANGES_REQUESTED":
            return True
        if rev.get("review_gate_status") == "CHANGES_REQUESTED":
            return True
        if int(rev.get("blocking_open_count") or 0) > 0:
            return True
    return False


def _record_g4_outcome(
    tracker_path: Path | None,
    ledger_path: Path | None,
    reservation_id: str | None,
    *,
    task_id: str,
    git_top: Path,
    out_dir: Path,
    candidate_sha: str,
    report: dict[str, Any],
    states: dict[str, PassState],
    reused_passes: list[str],
    active_backend: str,
    active_model: str | None,
    primary_model: str | None,
    route_models_by_tag: dict[str, str],
    resume_dir: Path | None = None,
) -> str | None:
    """Release G4 in-flight reservation, record spend, and transition tracker + ledger."""
    gate_status = str(report.get("review_gate_status", "BLOCKED"))
    count_round = gate_status in {"PASSED", "CHANGES_REQUESTED", "BLOCKED"}
    attempt_roots = [git_top / "harness" / "runs" / task_id / "attempts"]
    if tracker_path is not None and tracker_path.parent.name != task_id:
        attempt_roots.append(tracker_path.parent / "attempts")
    if out_dir.parent.name == "attempts":
        attempt_roots.append(out_dir.parent)

    def _update_locked() -> str | None:
        prior_sha_statuses: set[str] = set()
        if tracker_path is not None:
            try:
                tracker = task_runner.load_g4_tracker(tracker_path)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                err_msg = f"Corrupt g4-tracker.json at {tracker_path}: {exc}"
                report["review_gate_status"] = "BLOCKED"
                if err_msg not in report.setdefault("errors", []):
                    report["errors"].append(err_msg)
                if ledger_path is not None and ledger_path.is_file():
                    _bill_pass_raw_logs(
                        ledger_path,
                        out_dir=out_dir,
                        states=states,
                        reused_passes=reused_passes,
                        active_backend=active_backend,
                        active_model=active_model,
                        primary_model=primary_model,
                        route_models_by_tag=route_models_by_tag,
                    )
                    ledger = task_runner.load_ledger(ledger_path, repo_root=git_top)
                    if reservation_id is not None:
                        ledger.setdefault("active_g4_reservations", {}).pop(
                            reservation_id, None
                        )
                    spent = float(ledger.get("spent_api_equiv_usd", 0.0))
                    limit = float(ledger.get("budget_limit_usd", 0.0))
                    reserved = task_runner._active_reserved_usd(ledger)
                    committed = round(spent + reserved, 8)
                    ledger["state"] = (
                        "PAUSED_BUDGET"
                        if (limit > 0 and committed > round(limit, 8))
                        else "BLOCKED"
                    )
                    ledger["last_g4_candidate_sha"] = candidate_sha
                    ledger["last_g4_status"] = "BLOCKED"
                    task_runner._append_event(
                        ledger, "G4_TRACKER_CORRUPT_BLOCKED", {"error": err_msg}
                    )
                    task_runner.save_ledger(ledger_path, ledger)
                    return str(ledger["state"])
                return None
            prior_union = task_runner.collect_g4_attempt_union(
                attempt_roots, tracker, candidate_sha=candidate_sha
            )
            prior_sha_statuses = {
                st for st in prior_union["sha_statuses"].values() if st and st != "IN_PROGRESS"
            }
            norm_out = task_runner._norm_attempt_dir(out_dir)
            completed_at = datetime.now(timezone.utc).isoformat()
            pkt_epoch: str | None = None
            if reservation_id:
                inflight_ent = tracker["in_flight"].pop(reservation_id, None)
                if isinstance(inflight_ent, dict) and isinstance(
                    inflight_ent.get("packet_epoch"), str
                ):
                    pkt_epoch = inflight_ent["packet_epoch"]
                att_entry: dict[str, Any] = {
                    "candidate_sha": candidate_sha,
                    "status": gate_status,
                    "out_dir": norm_out,
                    "completed_at_utc": completed_at,
                }
                if pkt_epoch is not None:
                    att_entry["packet_epoch"] = pkt_epoch
                tracker["attempts"][reservation_id] = att_entry
            comp_entry: dict[str, Any] = {
                "candidate_sha": candidate_sha,
                "status": gate_status,
                "completed_at_utc": completed_at,
            }
            if pkt_epoch is not None:
                comp_entry["packet_epoch"] = pkt_epoch
            tracker.setdefault("completed_attempts", {})[norm_out] = comp_entry
            if count_round:
                tracker["completed_rounds"] = int(tracker.get("completed_rounds", 0)) + 1
                if pkt_epoch is not None:
                    pkt_map = tracker.setdefault("packet_rounds", {})
                    pkt_map[pkt_epoch] = int(pkt_map.get(pkt_epoch, 0)) + 1
            task_runner.save_g4_tracker(tracker_path, tracker)

        if ledger_path is None or not ledger_path.is_file():
            return None

        bill_errors = _bill_pass_raw_logs(
            ledger_path,
            out_dir=out_dir,
            states=states,
            reused_passes=reused_passes,
            active_backend=active_backend,
            active_model=active_model,
            primary_model=primary_model,
            route_models_by_tag=route_models_by_tag,
        )
        effective_gate_status = gate_status
        if bill_errors:
            effective_gate_status = "BLOCKED"
            report["review_gate_status"] = "BLOCKED"
            for b_err in bill_errors:
                if b_err not in report.setdefault("errors", []):
                    report["errors"].append(b_err)
        ledger = task_runner.load_ledger(ledger_path, repo_root=git_top)
        res_meta: Any = None
        if reservation_id is not None:
            res_meta = ledger.setdefault("active_g4_reservations", {}).pop(
                reservation_id, None
            )
        if effective_gate_status in {"PASSED", "CHANGES_REQUESTED"}:
            ledger["g4_rounds_total"] = int(ledger.get("g4_rounds_total", 0)) + 1

        spent = float(ledger.get("spent_api_equiv_usd", 0.0))
        limit = float(ledger.get("budget_limit_usd", 0.0))
        reserved = task_runner._active_reserved_usd(ledger)
        committed = round(spent + reserved, 8)
        budget_exceeded = ledger.get("state") == "PAUSED_BUDGET" or (
            limit > 0 and committed > round(limit, 8)
        )

        stale_reservation = False
        if reservation_id is not None:
            stale_reservation = not (
                isinstance(res_meta, dict)
                and res_meta.get("candidate_sha") == candidate_sha
                and int(ledger.get("packet_version", 1))
                == int(res_meta.get("packet_version", -1))
                and str(ledger.get("task_id", "")) == str(res_meta.get("task_id", ""))
                and str(ledger.get("base_sha", "")) == str(res_meta.get("base_sha", ""))
                and ledger.get("state") in {"REVIEW_FROZEN_G4", "BLOCKED_INFRA", "PAUSED_BUDGET"}
            )
        if stale_reservation:
            if budget_exceeded:
                ledger["state"] = "PAUSED_BUDGET"
            task_runner._append_event(
                ledger,
                "G4_STALE_RESERVATION_IGNORED",
                {
                    "candidate_sha": candidate_sha,
                    "reservation_id": reservation_id,
                    "ledger_packet_version": ledger.get("packet_version"),
                    "ledger_state": ledger.get("state"),
                    "reservation": res_meta,
                },
            )
            task_runner.save_ledger(ledger_path, ledger)
            return "STALE_RESERVATION"

        prior_changes_requested = (
            "CHANGES_REQUESTED" in prior_sha_statuses
            or _has_prior_blocking_review(attempt_roots, candidate_sha, resume_dir=resume_dir)
            or (
                ledger.get("last_g4_candidate_sha") == candidate_sha
                and ledger.get("last_g4_status") == "CHANGES_REQUESTED"
            )
        )
        if effective_gate_status in {"PASSED", "CHANGES_REQUESTED"}:
            ledger["g4_rounds_in_packet"] = int(ledger.get("g4_rounds_in_packet", 0)) + 1
        ledger["last_g4_candidate_sha"] = candidate_sha
        ledger["last_g4_status"] = effective_gate_status
        ledger["transient_retries_in_stage"] = 0

        if budget_exceeded:
            ledger["state"] = "PAUSED_BUDGET"
            task_runner._append_event(
                ledger,
                "G4_BUDGET_PAUSED",
                {
                    "candidate_sha": candidate_sha,
                    "spent_usd": spent,
                    "reserved_usd": reserved,
                    "committed_usd": committed,
                    "limit_usd": limit,
                },
            )
        elif effective_gate_status == "PASSED":
            if prior_changes_requested:
                ledger["candidate_sha"] = None
                ledger["state"] = "BLOCKED"
                ledger["last_g4_status"] = "BLOCKED"
                task_runner._append_event(
                    ledger,
                    "G4_BLOCKED_PRIOR_CHANGES_REQUESTED",
                    {"candidate_sha": candidate_sha},
                )
            else:
                ledger["candidate_sha"] = candidate_sha
                ledger["state"] = "READY_FOR_ACCEPTANCE"
                task_runner._append_event(ledger, "G4_PASSED", {"candidate_sha": candidate_sha})
        elif effective_gate_status == "CHANGES_REQUESTED":
            ledger["candidate_sha"] = None
            ledger["consecutive_failed_candidates"] = (
                int(ledger.get("consecutive_failed_candidates", 0)) + 1
            )
            task_runner._apply_candidate_failure_breakers(ledger)
            task_runner._append_event(
                ledger,
                "G4_CHANGES_REQUESTED",
                {"candidate_sha": candidate_sha},
            )
        else:
            ledger["state"] = effective_gate_status
            task_runner._append_event(
                ledger,
                f"G4_{effective_gate_status}",
                {"candidate_sha": candidate_sha, "errors": report.get("errors", [])},
            )
        task_runner.save_ledger(ledger_path, ledger)
        return str(ledger["state"])

    if tracker_path is not None:
        with task_runner._ledger_lock(tracker_path):
            if ledger_path is not None and ledger_path.is_file():
                with task_runner._ledger_lock(ledger_path):
                    return _update_locked()
            return _update_locked()
    if ledger_path is not None and ledger_path.is_file():
        with task_runner._ledger_lock(ledger_path):
            return _update_locked()
    return None


def _release_g4_reservation_on_abort(
    tracker_path: Path | None,
    ledger_path: Path | None,
    reservation_id: str | None,
    *,
    git_top: Path,
    candidate_sha: str,
) -> None:
    """Release leaked in-flight reservation if G4 exits unexpectedly before finalize."""
    if not reservation_id:
        return

    def _release_locked() -> None:
        if tracker_path is not None and tracker_path.is_file():
            try:
                tracker = task_runner.load_g4_tracker(tracker_path)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                tracker = None
            if tracker is not None and reservation_id in tracker.get("in_flight", {}):
                entry = tracker["in_flight"].pop(reservation_id, None)
                raw_out = entry.get("out_dir") if isinstance(entry, dict) else None
                if raw_out:
                    norm_out = task_runner._norm_attempt_dir(raw_out)
                    completed_at = datetime.now(timezone.utc).isoformat()
                    tracker["attempts"][reservation_id] = {
                        "candidate_sha": candidate_sha,
                        "status": "ABORTED",
                        "out_dir": norm_out,
                        "completed_at_utc": completed_at,
                    }
                    tracker.setdefault("completed_attempts", {})[norm_out] = {
                        "candidate_sha": candidate_sha,
                        "status": "ABORTED",
                        "completed_at_utc": completed_at,
                    }
                task_runner.save_g4_tracker(tracker_path, tracker)
        if ledger_path is not None and ledger_path.is_file():
            ledger = task_runner.load_ledger(ledger_path, repo_root=git_top)
            active = ledger.get("active_g4_reservations", {})
            if reservation_id in active:
                res_meta = active.pop(reservation_id, None)
                epoch_ok = (
                    isinstance(res_meta, dict)
                    and int(ledger.get("packet_version", 1))
                    == int(res_meta.get("packet_version", -1))
                    and str(ledger.get("task_id", "")) == str(res_meta.get("task_id", ""))
                )
                if epoch_ok:
                    ledger["state"] = "BLOCKED"
                    ledger["last_g4_candidate_sha"] = candidate_sha
                    ledger["last_g4_status"] = "ABORTED"
                task_runner._append_event(
                    ledger,
                    "G4_ABORTED_RELEASE" if epoch_ok else "G4_STALE_RESERVATION_ABORTED",
                    {"candidate_sha": candidate_sha, "reservation_id": reservation_id},
                )
                task_runner.save_ledger(ledger_path, ledger)

    if tracker_path is not None:
        with task_runner._ledger_lock(tracker_path):
            if ledger_path is not None and ledger_path.is_file():
                with task_runner._ledger_lock(ledger_path):
                    _release_locked()
            else:
                _release_locked()
    elif ledger_path is not None and ledger_path.is_file():
        with task_runner._ledger_lock(ledger_path):
            _release_locked()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    policy_data, excluded_prefixes, policy_err = load_model_policy(
        repo_root, require_exists=False
    )
    if policy_err:
        print(f"[codex_room_review] BLOCKED: {policy_err}", file=sys.stderr)
        return 2
    if args.model and is_excluded_model(args.model, excluded_prefixes):
        print(
            f"[codex_room_review] BLOCKED: --model {args.model!r} "
            "is excluded by model-policy.json.",
            file=sys.stderr,
        )
        return 2
    if args.fallback_model and is_excluded_model(args.fallback_model, excluded_prefixes):
        print(
            f"[codex_room_review] BLOCKED: --fallback-model {args.fallback_model!r} "
            "is excluded by model-policy.json.",
            file=sys.stderr,
        )
        return 2

    schema_bytes = SCHEMA_PATH.read_bytes()
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    schema = json.loads(schema_bytes.decode("utf-8"))
    schema_errors = validate_strict_json_schema(schema)
    if schema_errors:
        print(
            f"[codex_room_review] Invalid review-output.schema.json: {schema_errors}",
            file=sys.stderr,
        )
        return 2

    try:
        info = resolve_commit_info(repo_root, args.commit, args.base)
    except (ValueError, TimeoutError) as exc:
        print(f"[codex_room_review] {exc}", file=sys.stderr)
        return 2
    git_top = Path(info["git_top"])
    effective_prefixes = excluded_prefixes
    if git_top != repo_root:
        git_policy, git_prefixes, git_policy_err = load_model_policy(
            git_top, require_exists=False
        )
        if git_policy_err:
            print(f"[codex_room_review] BLOCKED: {git_policy_err}", file=sys.stderr)
            return 2
        policy_data = git_policy
        effective_prefixes = git_prefixes
        if args.model and is_excluded_model(args.model, effective_prefixes):
            print(
                f"[codex_room_review] BLOCKED: --model {args.model!r} is excluded.",
                file=sys.stderr,
            )
            return 2
        if args.fallback_model and is_excluded_model(args.fallback_model, effective_prefixes):
            print(
                f"[codex_room_review] BLOCKED: --fallback-model {args.fallback_model!r} "
                "is excluded.",
                file=sys.stderr,
            )
            return 2

    qualified_routes, routes_err = extract_qualified_routes(policy_data, effective_prefixes)
    if routes_err:
        print(f"[codex_room_review] BLOCKED: {routes_err}", file=sys.stderr)
        return 2
    window_guard_enabled, window_exhausted_pct, guard_err = extract_codex_window_guard_config(
        policy_data
    )
    if guard_err:
        print(f"[codex_room_review] BLOCKED: {guard_err}", file=sys.stderr)
        return 2
    max_transient_retries = extract_max_transient_retries(policy_data)
    policy_file = git_top / "harness" / "model-policy.json"
    policy_sha256 = (
        hashlib.sha256(policy_file.read_bytes()).hexdigest()
        if policy_file.is_file()
        else hashlib.sha256(b"").hexdigest()
    )

    resume_dir: Path | None = None
    if args.resume_from is not None:
        resume_dir = args.resume_from.resolve()
        if not resume_dir.is_dir():
            print(
                f"[codex_room_review] BLOCKED: --resume-from dir not found: {resume_dir}",
                file=sys.stderr,
            )
            return 2

    candidate_sha = info["candidate_sha"]
    if args.out_dir is not None:
        out_dir = args.out_dir.resolve()
    else:
        ts_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        attempt_id = args.attempt_id or f"{ts_id}-{os.getpid()}"
        out_dir = (
            git_top
            / "harness"
            / "runs"
            / args.task_id
            / "attempts"
            / f"{attempt_id}-{candidate_sha[:12]}"
        ).resolve()

    lock_markers = (
        ".review-attempt.lock",
        "consolidated-review.json",
        "pass1.prompt.md",
        "pass2.prompt.md",
        "frozen",
    )
    if any((out_dir / marker).exists() for marker in lock_markers):
        print(
            f"[codex_room_review] Write-once refusal: {out_dir} already contains "
            "review artifacts or attempt lock.",
            file=sys.stderr,
        )
        return 2

    active_ledger_path: Path | None = None
    active_tracker_path: Path | None = None
    active_reservation_id: str | None = None
    if _should_enforce_g4_caps(args, out_dir, git_top):
        active_ledger_path = _resolve_g4_ledger_path(args, git_top)
        preflight_rc, active_tracker_path, active_reservation_id = _preflight_g4_caps(
            args, out_dir, git_top, candidate_sha, active_ledger_path
        )
        if preflight_rc is not None:
            return preflight_rc

    def _run_after_preflight() -> int:
        return _execute_after_preflight(
            args=args,
            git_top=git_top,
            out_dir=out_dir,
            info=info,
            candidate_sha=candidate_sha,
            policy_data=policy_data,
            effective_prefixes=effective_prefixes,
            qualified_routes=qualified_routes,
            max_transient_retries=max_transient_retries,
            window_guard_enabled=window_guard_enabled,
            window_exhausted_pct=window_exhausted_pct,
            policy_sha256=policy_sha256,
            schema=schema,
            schema_sha256=schema_sha256,
            resume_dir=resume_dir,
            active_ledger_path=active_ledger_path,
            active_tracker_path=active_tracker_path,
            active_reservation_id=active_reservation_id,
        )

    try:
        return _run_after_preflight()
    finally:
        _release_g4_reservation_on_abort(
            active_tracker_path,
            active_ledger_path,
            active_reservation_id,
            git_top=git_top,
            candidate_sha=candidate_sha,
        )


def _execute_after_preflight(
    *,
    args: argparse.Namespace,
    git_top: Path,
    out_dir: Path,
    info: dict[str, Any],
    candidate_sha: str,
    policy_data: dict[str, Any] | None,
    effective_prefixes: tuple[str, ...],
    qualified_routes: list[str],
    max_transient_retries: int,
    window_guard_enabled: bool = True,
    window_exhausted_pct: float = 90.0,
    policy_sha256: str,
    schema: dict[str, Any],
    schema_sha256: str,
    resume_dir: Path | None,
    active_ledger_path: Path | None,
    active_tracker_path: Path | None,
    active_reservation_id: str | None,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".review-attempt.lock"
    try:
        with lock_path.open("x", encoding="utf-8") as lock_fh:
            lock_fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "candidate_sha": candidate_sha,
                        "task_id": args.task_id,
                        "dry_run": bool(args.dry_run),
                        "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
    except FileExistsError:
        print(
            f"[codex_room_review] Write-once refusal: concurrent or duplicate attempt "
            f"lock exists at {lock_path}.",
            file=sys.stderr,
        )
        return 2

    if args.consolidate_only:
        p1_path, p2_path = args.consolidate_only
        p1_data = json.loads(p1_path.read_text(encoding="utf-8")) if p1_path.is_file() else None
        p2_data = json.loads(p2_path.read_text(encoding="utf-8")) if p2_path.is_file() else None
        report = consolidate_reviews(p1_data, p2_data, expected_sha=candidate_sha)
        tmp_dir = tempfile.mkdtemp(prefix="crr-consolidate-")
        try:
            frozen_meta = materialize_frozen_tree(
                git_top,
                candidate_sha,
                Path(tmp_dir) / "topstep-chart-app",
                base_sha=info["base_sha"],
            )
            report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
        except (ValueError, RuntimeError, TimeoutError) as exc:
            report["errors"].append(f"Unable to compute frozen_tree_sha256: {exc}")
            report["review_gate_status"] = "BLOCKED"
        finally:
            cleanup_readonly_tree(Path(tmp_dir))
        if not _write_once_outputs(out_dir, report):
            return 2
        print(f"[codex_room_review] status={report['review_gate_status']} out={out_dir}")
        if report["review_gate_status"] == "BLOCKED":
            return 2
        if args.fail_on_blocking and report["blocking_open_count"] > 0:
            return 1
        return 0

    frozen_dir = out_dir / "frozen"
    frozen_src_dir = frozen_dir / "topstep-chart-app"
    try:
        frozen_meta = materialize_frozen_tree(
            git_top, candidate_sha, frozen_src_dir, base_sha=info["base_sha"]
        )
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"[codex_room_review] {exc}", file=sys.stderr)
        return 2
    info["frozen_src_dir"] = frozen_meta["dest_dir"]
    info["patch_path"] = frozen_meta["patch_path"]
    patch_file_path = Path(frozen_meta["patch_path"]) if frozen_meta["patch_path"] else None
    patch_sha_before = (
        hashlib.sha256(patch_file_path.read_bytes()).hexdigest()
        if patch_file_path and patch_file_path.is_file()
        else None
    )

    def _patch_modified() -> bool:
        if patch_file_path is None:
            return False
        if not patch_file_path.is_file():
            return True
        return hashlib.sha256(patch_file_path.read_bytes()).hexdigest() != patch_sha_before

    default_task_text = (
        "No Task Packet file supplied; review candidate commit against AGENTS.md, "
        "implementation_plan.md, and docs/WORKSPACE_PROTOCOL.md."
    )
    task_text = (
        args.task_packet.read_text(encoding="utf-8")
        if args.task_packet and args.task_packet.is_file()
        else default_task_text
    )
    packet_sha256 = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
    prompt1 = render_prompt(PROMPTS_DIR / "pass1-correctness.md", info, task_text)
    prompt2 = render_prompt(PROMPTS_DIR / "pass2-security.md", info, task_text)
    prompt1_path = out_dir / "pass1.prompt.md"
    prompt2_path = out_dir / "pass2.prompt.md"
    prompt1_path.write_text(prompt1, encoding="utf-8")
    prompt2_path.write_text(prompt2, encoding="utf-8")

    frozen_prompts_dir = frozen_dir / "prompts"
    frozen_prompts_dir.mkdir(parents=True, exist_ok=True)
    frozen_prompt1_path = frozen_prompts_dir / "pass1.prompt.md"
    frozen_prompt2_path = frozen_prompts_dir / "pass2.prompt.md"
    frozen_prompt1_path.write_text(prompt1, encoding="utf-8")
    frozen_prompt2_path.write_text(prompt2, encoding="utf-8")
    os.chmod(frozen_prompt1_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(frozen_prompt2_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    if args.dry_run:
        try:
            resolve_agy_command()
            agy_available = True
        except FileNotFoundError:
            agy_available = False
        meta = {
            "dry_run": True,
            "candidate_sha": candidate_sha,
            "base_sha": info["base_sha"],
            "frozen_src_dir": str(frozen_src_dir),
            "patch_path": frozen_meta["patch_path"],
            "frozen_tree_sha256": frozen_meta["frozen_tree_sha256"],
            "worktree_dirty": info["worktree_dirty"],
            "schema_valid": True,
            "backend": args.backend,
            "fallback_model": args.fallback_model,
            "qualified_routes": qualified_routes,
            "agy_available": agy_available,
            "out_dir": str(out_dir),
            "runtime_pass1": str(args.runtime_root / "topstep-review-pass1"),
            "runtime_pass2": str(args.runtime_root / "topstep-review-pass2"),
        }
        print(json.dumps(meta, indent=2))
        return 0

    expected_prov_by_pass = {
        pass_name: _build_expected_provenance(
            candidate_sha=candidate_sha,
            base_sha=info["base_sha"],
            packet_sha256=packet_sha256,
            policy_sha256=policy_sha256,
            schema_sha256=schema_sha256,
            lens=lens,
            frozen_tree_sha256=frozen_meta["frozen_tree_sha256"],
        )
        for pass_name, lens in PASS_LENS_MAP.items()
    }
    states: dict[str, PassState] = {
        pass_name: PassState(pass_name=pass_name, lens=lens)
        for pass_name, lens in PASS_LENS_MAP.items()
    }
    reused_passes: list[str] = []
    exhausted_quota_pools: list[str] = []

    def _mark_pool_exhausted(model_name: str | None) -> str:
        pool = resolve_quota_pool(model_name, policy_data)
        if pool not in exhausted_quota_pools:
            exhausted_quota_pools.append(pool)
        return pool

    prior_unreused_findings: dict[str, dict[str, Any]] = {}
    resume_block_errors: list[str] = []
    if resume_dir is not None:
        resume_consolidated = task_runner._read_optional_json_dict(
            resume_dir / "consolidated-review.json"
        )
        consol_same_sha = (
            isinstance(resume_consolidated, dict)
            and str(resume_consolidated.get("revision_sha", "")).strip() == candidate_sha
        )
        for pass_name, state in states.items():
            loaded = _try_load_reusable_pass(
                resume_dir,
                pass_name,
                expected_prov_by_pass[pass_name],
                schema,
                effective_prefixes,
            )
            if loaded is not None:
                loaded_payload, loaded_prov = loaded
                (out_dir / f"{pass_name}.json").write_text(
                    json.dumps(loaded_payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                (out_dir / f"{pass_name}.provenance.json").write_text(
                    json.dumps(loaded_prov, indent=2) + "\n",
                    encoding="utf-8",
                )
                state.payload = loaded_payload
                state.backend = loaded_prov["backend"]
                state.model = loaded_prov["model"]
                state.reused = True
                state.is_complete = True
                reused_passes.append(pass_name)
                continue

            consol_pass_cr = consol_same_sha and isinstance(resume_consolidated, dict) and (
                resume_consolidated.get(f"{pass_name}_verdict") == "CHANGES_REQUESTED"
                or any(
                    isinstance(f, dict)
                    and f.get("blocking") is True
                    and pass_name in (f.get("detected_by") or [])
                    for f in (resume_consolidated.get("consolidated_findings") or [])
                )
            )
            resume_pass_path = resume_dir / f"{pass_name}.json"
            if not resume_pass_path.is_file():
                if consol_pass_cr:
                    resume_block_errors.append(
                        f"Missing {pass_name}.json in --resume-from {resume_dir} "
                        f"despite prior CHANGES_REQUESTED/blocking findings."
                    )
                continue
            try:
                raw_pass_obj = json.loads(resume_pass_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                resume_block_errors.append(
                    f"Malformed {pass_name}.json in --resume-from {resume_dir}: {exc}"
                )
                continue
            if not isinstance(raw_pass_obj, dict):
                resume_block_errors.append(
                    f"Invalid {pass_name}.json in --resume-from {resume_dir}: not an object."
                )
                continue
            raw_sha = str(raw_pass_obj.get("revision_sha", "")).strip()
            if raw_sha != candidate_sha:
                if consol_pass_cr:
                    resume_block_errors.append(
                        f"Stale revision_sha in {pass_name}.json under --resume-from "
                        f"{resume_dir} despite prior CHANGES_REQUESTED for {candidate_sha[:12]}."
                    )
                continue
            pass_val_errs = _validate_pass_payload(
                raw_pass_obj,
                schema,
                expected_sha=candidate_sha,
                expected_lens=state.lens,
            )
            if pass_val_errs:
                resume_block_errors.append(
                    f"Invalid {pass_name}.json in --resume-from {resume_dir}: "
                    f"{'; '.join(pass_val_errs)}"
                )
                continue
            if raw_pass_obj.get("verdict") == "BLOCKED":
                resume_block_errors.append(
                    f"Prior {pass_name}.json in --resume-from {resume_dir} has verdict='BLOCKED'."
                )
                continue
            has_blocking = raw_pass_obj.get("verdict") == "CHANGES_REQUESTED" or any(
                isinstance(f, dict)
                and (
                    str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
                    or f.get("blocking") is True
                )
                for f in (raw_pass_obj.get("findings") or [])
            )
            if has_blocking:
                prior_unreused_findings[pass_name] = raw_pass_obj

    prompts_by_pass = {
        "pass1": (prompt1, frozen_prompt1_path),
        "pass2": (prompt2, frozen_prompt2_path),
    }

    active_backend = "codex"
    active_model: str | None = args.model
    primary_model: str | None = args.model
    fallback_used = False
    fallback_reason: str | None = None
    forced_gate_status: str | None = None
    stage_extra_errors: list[str] = []
    route_models_by_tag: dict[str, str] = {}

    def _finalize_and_exit() -> int:
        report = consolidate_reviews(
            states["pass1"].payload,
            states["pass2"].payload,
            expected_sha=candidate_sha,
        )
        if forced_gate_status:
            report["review_gate_status"] = forced_gate_status
        for err in stage_extra_errors:
            if err and err not in report["errors"]:
                report["errors"].append(err)
        frozen_hash_final, _ = compute_tree_sha256(frozen_src_dir)
        if frozen_hash_final != frozen_meta["frozen_tree_sha256"]:
            report["errors"].append(
                "Read-only violation: frozen-src snapshot changed during review."
            )
            report["review_gate_status"] = "BLOCKED"
        if _patch_modified():
            report["errors"].append(
                "Read-only violation: candidate.patch changed during review."
            )
            report["review_gate_status"] = "BLOCKED"
        report["frozen_tree_sha256"] = frozen_meta["frozen_tree_sha256"]
        report["candidate_patch_path"] = frozen_meta["patch_path"]
        report["worktree_dirty_at_review"] = info["worktree_dirty"]
        report["review_backend"] = active_backend
        report["review_model"] = active_model
        report["fallback_used"] = fallback_used
        report["fallback_reason"] = fallback_reason
        report["pass1_backend"] = states["pass1"].backend
        report["pass1_model"] = states["pass1"].model
        report["pass2_backend"] = states["pass2"].backend
        report["pass2_model"] = states["pass2"].model
        report["reused_passes"] = list(reused_passes)
        report["exhausted_quota_pools"] = list(exhausted_quota_pools)
        if active_tracker_path is not None and active_tracker_path.is_file():
            try:
                task_runner.load_g4_tracker(active_tracker_path)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                err_msg = f"Corrupt g4-tracker.json at {active_tracker_path}: {exc}"
                report["review_gate_status"] = "BLOCKED"
                if err_msg not in report["errors"]:
                    report["errors"].append(err_msg)
        if active_ledger_path is not None and active_ledger_path.is_file():
            _, unbillable_errs = _plan_raw_log_billing(
                out_dir=out_dir,
                states=states,
                reused_passes=reused_passes,
                active_backend=active_backend,
                active_model=active_model,
                primary_model=primary_model,
                route_models_by_tag=route_models_by_tag,
            )
            if unbillable_errs:
                report["review_gate_status"] = "BLOCKED"
                for u_err in unbillable_errs:
                    if u_err not in report["errors"]:
                        report["errors"].append(u_err)
        if not _write_once_outputs(out_dir, report):
            return 2

        resulting_ledger_state = _record_g4_outcome(
            active_tracker_path,
            active_ledger_path,
            active_reservation_id,
            task_id=args.task_id,
            git_top=git_top,
            out_dir=out_dir,
            candidate_sha=candidate_sha,
            report=report,
            states=states,
            reused_passes=reused_passes,
            active_backend=active_backend,
            active_model=active_model,
            primary_model=primary_model,
            route_models_by_tag=route_models_by_tag,
            resume_dir=resume_dir,
        )

        print(
            f"[codex_room_review] sha={candidate_sha[:12]} backend={active_backend} "
            f"model={active_model or 'default'} fallback={fallback_used} "
            f"status={report['review_gate_status']} "
            f"blocking_open={report['blocking_open_count']} out={out_dir}"
        )
        if report["review_gate_status"] in {"BLOCKED", "BLOCKED_AUTH", "BLOCKED_INFRA"}:
            return 2
        if resulting_ledger_state in {"PAUSED_BUDGET", "STALE_RESERVATION"} or (
            report["review_gate_status"] == "PASSED"
            and resulting_ledger_state
            in {"BLOCKED", "BLOCKED_AUTH", "BLOCKED_INFRA", "NEEDS_REPLAN"}
        ):
            print(
                f"[codex_room_review] {resulting_ledger_state}: G4 review halted "
                f"after ledger accounting (review_status={report['review_gate_status']}).",
                file=sys.stderr,
            )
            return 2
        if args.fail_on_blocking and report["blocking_open_count"] > 0:
            return 1
        return 0

    if resume_block_errors:
        forced_gate_status = "BLOCKED"
        stage_extra_errors.extend(resume_block_errors)
        return _finalize_and_exit()

    def _verify_agy_readonly(before_status: str) -> str | None:
        try:
            after_status = _git_output(git_top, ["status", "--porcelain"]).stdout
        except TimeoutError as exc:
            return f"Read-only check failed after agy pass: {exc}"
        frozen_hash_after, _ = compute_tree_sha256(frozen_src_dir)
        if after_status != before_status:
            return "Read-only violation: git worktree changed during agy review pass."
        if frozen_hash_after != frozen_meta["frozen_tree_sha256"]:
            return "Read-only violation: frozen-src snapshot modified during agy review pass."
        if _patch_modified():
            return "Read-only violation: candidate.patch modified during agy review pass."
        return None

    def _invoke_passes(
        backend_name: str,
        cli_prefix: list[str],
        model_name: str,
        target_passes: list[str],
    ) -> tuple[dict[str, dict[str, Any] | None], str | None]:
        before_status = ""
        if backend_name == "agy":
            try:
                before_status = _git_output(git_top, ["status", "--porcelain"]).stdout
            except TimeoutError as exc:
                return {p: None for p in target_passes}, f"Read-only check failed: {exc}"

        kwargs_by_pass: dict[str, dict[str, Any]] = {}
        for pass_name in target_passes:
            prompt_text, frozen_prompt_path = prompts_by_pass[pass_name]
            if backend_name == "codex":
                rt_name = f"topstep-review-{pass_name}"
                kwargs_by_pass[pass_name] = {
                    "codex_prefix": cli_prefix,
                    "role": pass_name,
                    "runtime_dir": args.runtime_root / rt_name,
                    "repo_root": frozen_dir,
                    "schema_path": SCHEMA_PATH,
                    "prompt_text": prompt_text,
                    "out_dir": out_dir,
                    "pass_name": pass_name,
                    "model": model_name,
                    "timeout_sec": args.timeout,
                    "idle_timeout_sec": args.idle_timeout,
                }
            else:
                kwargs_by_pass[pass_name] = {
                    "agy_prefix": cli_prefix,
                    "role": pass_name,
                    "repo_root": frozen_dir,
                    "schema_path": SCHEMA_PATH,
                    "prompt_text": prompt_text,
                    "prompt_file": frozen_prompt_path,
                    "out_dir": out_dir,
                    "pass_name": pass_name,
                    "model": model_name,
                    "timeout_sec": args.timeout,
                    "idle_timeout_sec": args.idle_timeout,
                }

        runner = _run_codex_pass if backend_name == "codex" else _run_agy_pass
        results: dict[str, dict[str, Any] | None] = {}
        if len(target_passes) == 2:
            r1, r2 = _execute_two_passes(
                runner,
                kwargs_by_pass[target_passes[0]],
                kwargs_by_pass[target_passes[1]],
                sequential=(args.sequential or backend_name == "agy"),
            )
            results[target_passes[0]] = r1
            results[target_passes[1]] = r2
        else:
            only_p = target_passes[0]
            results[only_p] = runner(**kwargs_by_pass[only_p])

        if backend_name == "agy":
            ro_err = _verify_agy_readonly(before_status)
            if ro_err:
                return results, ro_err
        return results, None

    def _run_provider_stage(
        backend_name: str,
        cli_prefix: list[str],
        model_name: str,
    ) -> tuple[str, list[str], list[str]]:
        needed = [p for p in ("pass1", "pass2") if not states[p].is_complete]
        if not needed:
            return "OK", [], []

        retries_used = {p: 0 for p in needed}
        failures: dict[str, PassFailure] = {}
        classifier = classify_codex_failure if backend_name == "codex" else classify_agy_failure

        def _record_and_classify(
            batch_results: dict[str, dict[str, Any] | None],
        ) -> None:
            for pass_name, payload in batch_results.items():
                rc, term_reason = _pop_pass_exec_meta(out_dir, pass_name)
                state = states[pass_name]
                if payload is not None:
                    val_errs = _validate_pass_payload(
                        payload,
                        schema,
                        expected_sha=candidate_sha,
                        expected_lens=state.lens,
                    )
                    if not val_errs:
                        if pass_name in prior_unreused_findings:
                            merged_payload = _merge_prior_pass_findings(
                                payload, prior_unreused_findings[pass_name], candidate_sha
                            )
                            if merged_payload is not None:
                                payload = merged_payload
                                (out_dir / f"{pass_name}.json").write_text(
                                    json.dumps(payload, indent=2) + "\n",
                                    encoding="utf-8",
                                )
                        state.payload = payload
                        state.backend = backend_name
                        state.model = model_name
                        state.is_complete = True
                        failures.pop(pass_name, None)
                        if (
                            payload.get("verdict") != "BLOCKED"
                            and pass_name not in prior_unreused_findings
                        ):
                            _write_pass_provenance(
                                out_dir,
                                pass_name,
                                expected_prov_by_pass[pass_name],
                                backend=backend_name,
                                model=model_name,
                            )
                        continue
                else:
                    val_errs = None
                failures[pass_name] = classifier(
                    out_dir,
                    pass_name,
                    returncode=rc,
                    termination_reason=term_reason,
                    pass_payload=payload,
                    validation_errors=val_errs,
                )

        batch, ro_err = _invoke_passes(backend_name, cli_prefix, model_name, needed)
        if ro_err:
            return "BLOCKED", [ro_err], list(needed)
        _record_and_classify(batch)

        while failures:
            if any(f.category == OUTCOME_AUTH_ERROR for f in failures.values()):
                reasons = [
                    f.reason for f in failures.values() if f.category == OUTCOME_AUTH_ERROR
                ]
                return "BLOCKED_AUTH", reasons, list(failures.keys())
            if any(f.category == OUTCOME_SCHEMA_OR_POLICY for f in failures.values()):
                reasons = [
                    f.reason
                    for f in failures.values()
                    if f.category == OUTCOME_SCHEMA_OR_POLICY
                ]
                return "BLOCKED", reasons, list(failures.keys())
            if any(f.category == OUTCOME_CONFIRMED_QUOTA for f in failures.values()):
                reasons = [
                    f.reason
                    for f in failures.values()
                    if f.category == OUTCOME_CONFIRMED_QUOTA
                ]
                return "QUOTA_EXHAUSTED", reasons, list(failures.keys())

            retry_targets = [
                p
                for p, f in failures.items()
                if f.category == OUTCOME_TRANSIENT and retries_used[p] < max_transient_retries
            ]
            if not retry_targets:
                reasons = [f.reason for f in failures.values()]
                return "BLOCKED_INFRA", reasons, list(failures.keys())

            max_sleep = max(
                (min(failures[p].retry_after_sec or 0.0, 5.0) for p in retry_targets),
                default=0.0,
            )
            if max_sleep > 0:
                time.sleep(max_sleep)
            safe_model = _sanitize_archive_token(model_name or "default")
            for p in retry_targets:
                retry_tag = f"{backend_name}-{safe_model}-retry{retries_used[p]}"
                actual_tag = _archive_pass_logs(out_dir, p, retry_tag)
                route_models_by_tag[actual_tag] = model_name
                retries_used[p] += 1

            retry_batch, ro_err = _invoke_passes(
                backend_name, cli_prefix, model_name, retry_targets
            )
            if ro_err:
                return "BLOCKED", [ro_err], list(retry_targets)
            _record_and_classify(retry_batch)

        return "OK", [], []

    if all(s.is_complete for s in states.values()):
        active_backend = states["pass1"].backend or "codex"
        active_model = states["pass1"].model
        return _finalize_and_exit()

    if args.backend == "agy":
        active_backend = "agy"
        active_model = (
            args.model
            or args.fallback_model
            or (qualified_routes[0] if qualified_routes else DEFAULT_AGY_FALLBACK_MODEL)
        )
        if is_excluded_model(active_model, effective_prefixes):
            forced_gate_status = "BLOCKED"
            stage_extra_errors.append(f"Excluded model {active_model!r} (MOD-001).")
            return _finalize_and_exit()
        try:
            agy_prefix = resolve_agy_command()
        except FileNotFoundError as exc:
            print(f"[codex_room_review] {exc}", file=sys.stderr)
            forced_gate_status = "BLOCKED_INFRA"
            stage_extra_errors.append(str(exc))
            return _finalize_and_exit()
        outcome, st_errs, _ = _run_provider_stage("agy", agy_prefix, active_model)
        if outcome == "QUOTA_EXHAUSTED":
            _mark_pool_exhausted(active_model)
            forced_gate_status = "BLOCKED_INFRA"
            stage_extra_errors.extend(st_errs)
        elif outcome in {"BLOCKED", "BLOCKED_AUTH", "BLOCKED_INFRA"}:
            forced_gate_status = outcome
            stage_extra_errors.extend(st_errs)
        return _finalize_and_exit()

    if args.model is not None and args.model != EXPECTED_REVIEW_MODEL:
        model_err = (
            f"Primary codex reviewer model is locked to {EXPECTED_REVIEW_MODEL!r}, "
            f"got {args.model!r}"
        )
        print(f"[codex_room_review] {model_err}", file=sys.stderr)
        active_backend = "codex"
        active_model = args.model
        forced_gate_status = "BLOCKED"
        stage_extra_errors.append(model_err)
        return _finalize_and_exit()

    active_model = EXPECTED_REVIEW_MODEL
    primary_model = EXPECTED_REVIEW_MODEL
    v1 = _call_verify_role("pass1", args.runtime_root, git_top)
    v2 = _call_verify_role("pass2", args.runtime_root, git_top)
    if not (v1["ok"] and v2["ok"]):
        preflight_err = (
            "Codex Room runtime failed safety/model preflight: "
            f"pass1={v1.get('errors', [])}; pass2={v2.get('errors', [])}"
        )
        print(f"[codex_room_review] {preflight_err}", file=sys.stderr)
        active_backend = "codex"
        forced_gate_status = "BLOCKED"
        stage_extra_errors.append(preflight_err)
        return _finalize_and_exit()

    codex_prefix: list[str] | None = None
    try:
        codex_prefix = resolve_codex_command()
    except FileNotFoundError:
        if args.backend == "codex":
            msg = "[codex_room_review] Codex CLI (`codex`) not found on PATH."
            print(msg, file=sys.stderr)
            forced_gate_status = "BLOCKED_INFRA"
            stage_extra_errors.append(msg)
            return _finalize_and_exit()
        fallback_reason = "codex_cli_not_found"
        _mark_pool_exhausted(EXPECTED_REVIEW_MODEL)

    if codex_prefix is not None and window_guard_enabled and any(
        not s.is_complete for s in states.values()
    ):
        probe_role = "pass1" if not states["pass1"].is_complete else "pass2"
        probe_rt = args.runtime_root / f"topstep-review-{probe_role}"
        if not probe_rt.is_dir():
            probe_rt = args.runtime_root
        rl_result = probe_codex_rate_limits(codex_prefix, probe_rt)
        rl_exhausted, rl_reason, rl_snapshot = evaluate_codex_window_exhaustion(
            rl_result, threshold_pct=window_exhausted_pct
        )
        if rl_snapshot is not None:
            (out_dir / "codex-ratelimits-preflight.json").write_text(
                json.dumps(rl_snapshot, indent=2) + "\n",
                encoding="utf-8",
            )
        if rl_exhausted and rl_reason:
            _mark_pool_exhausted(EXPECTED_REVIEW_MODEL)
            if args.backend == "codex":
                forced_gate_status = "BLOCKED_INFRA"
                stage_extra_errors.append(rl_reason)
                return _finalize_and_exit()
            fallback_reason = rl_reason
            codex_prefix = None

    if codex_prefix is not None:
        outcome, st_errs, failed_passes = _run_provider_stage(
            "codex", codex_prefix, EXPECTED_REVIEW_MODEL
        )
        if outcome == "OK":
            if states["pass1"].backend and states["pass1"].backend != "codex":
                active_backend = states["pass1"].backend
                active_model = states["pass1"].model
            return _finalize_and_exit()
        if outcome == "QUOTA_EXHAUSTED":
            _mark_pool_exhausted(EXPECTED_REVIEW_MODEL)
            if args.backend == "codex":
                forced_gate_status = "BLOCKED_INFRA"
                stage_extra_errors.extend(st_errs)
                return _finalize_and_exit()
            fallback_reason = "; ".join(st_errs)
            for tag in _archive_failed_codex_logs(out_dir, tuple(failed_passes)):
                route_models_by_tag[tag] = primary_model or EXPECTED_REVIEW_MODEL
        else:
            forced_gate_status = outcome
            stage_extra_errors.extend(st_errs)
            return _finalize_and_exit()

    if fallback_reason is not None and args.backend == "auto":
        for p_name, p_state in states.items():
            if p_state.is_complete:
                loaded = _try_load_reusable_pass(
                    out_dir,
                    p_name,
                    expected_prov_by_pass[p_name],
                    schema,
                    effective_prefixes,
                )
                if (
                    loaded is None
                    and p_state.payload
                    and p_state.payload.get("verdict") != "BLOCKED"
                ):
                    p_state.is_complete = False

        candidate_routes = (
            [args.fallback_model] if args.fallback_model is not None else list(qualified_routes)
        )
        if not candidate_routes:
            forced_gate_status = "BLOCKED_INFRA"
            stage_extra_errors.append(
                f"Codex unavailable ({fallback_reason}) and no qualified_routes configured."
            )
            return _finalize_and_exit()

        for route_idx, route_model in enumerate(candidate_routes):
            pool = resolve_quota_pool(route_model, policy_data)
            if pool in exhausted_quota_pools:
                continue
            try:
                agy_prefix = resolve_agy_command()
            except FileNotFoundError as exc:
                print(f"[codex_room_review] Fallback failed: {exc}", file=sys.stderr)
                forced_gate_status = "BLOCKED_INFRA"
                stage_extra_errors.append(str(exc))
                return _finalize_and_exit()

            print(
                f"[codex_room_review] Codex CLI unavailable ({fallback_reason}); "
                f"falling back to agy ({route_model}).",
                file=sys.stderr,
            )
            active_backend = "agy"
            active_model = route_model
            fallback_used = True
            route_models_by_tag[f"agy-failed-{route_idx}"] = route_model

            outcome, st_errs, failed_passes = _run_provider_stage("agy", agy_prefix, route_model)
            if outcome == "OK":
                return _finalize_and_exit()
            if outcome == "QUOTA_EXHAUSTED":
                _mark_pool_exhausted(route_model)
                for fp in failed_passes:
                    actual_tag = _archive_pass_logs(out_dir, fp, f"agy-failed-{route_idx}")
                    route_models_by_tag[actual_tag] = route_model
                stage_extra_errors.extend(st_errs)
                continue
            forced_gate_status = outcome
            stage_extra_errors.extend(st_errs)
            return _finalize_and_exit()

        forced_gate_status = "BLOCKED_INFRA"
        stage_extra_errors.append(
            f"All fallback routes exhausted quota pools: {exhausted_quota_pools}"
        )

    return _finalize_and_exit()


if __name__ == "__main__":
    sys.exit(main())
