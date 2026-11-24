"""Executable Brief Readiness Gate (G0.5) for S-L-P Task Packets.

Performs two-layer validation before any Peer session can launch:
  Layer 1: Structural headers, git-anchor commit/blob existence, scope overlap,
           peer-system-prompt SHA-256 verification, and local check commands.
  Layer 2: Risk-dependent semantic contracts and eligible peer model filtering.

Writes an immutable write-once readiness.json compatible with receipt.py.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from harness.scripts.schema_utils import (
        is_hex_sha40,
        resolve_git_toplevel,
        run_managed_cmd,
    )
except ImportError:
    from schema_utils import (
        is_hex_sha40,
        resolve_git_toplevel,
        run_managed_cmd,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
PEER_PROMPT_REL = ".agents/skills/slp-orchestrator/references/peer-system-prompt.md"
DEFAULT_POLICY_REL = "harness/model-policy.json"
DEFAULT_EXCLUDED_MODEL_PREFIXES = (
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-pro-orchestrator-argon",
    "gemini-3.7-flash",
    "gemini-2.5-pro",
)
ALLOWED_PEER_HIGH = "gemini-4-hydrogen-high"
ALLOWED_PEER_LOW = "gemini-4-hydrogen-low"
HEX_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")

METADATA_BULLET_RE = re.compile(
    r"^(?P<indent>[ \t]*)[-*]\s*(?:\*\*|__)?(?P<key>[A-Za-z0-9 _/()-]+?)"
    r"(?:\*\*|__)?\s*:\s*(?P<val>.*)$"
)
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$")
OWNED_MODE_RE = re.compile(r"^(?P<path>.+?)\s*\((?P<mode>new|modify)\)\s*$", re.IGNORECASE)
TEST_SENSITIVITY_RE = re.compile(r"\bwhen\b.+\bif\b.+\btest\s+fails\s+at\b", re.IGNORECASE)
CHECK_CMD_RE = re.compile(r"\b(arch_check|pytest|ruff|build_verifier|pnpm)\b", re.IGNORECASE)

PERSISTENCE_PATH_MARKERS = (
    "storage/",
    "sqlite",
    "parquet",
    "duckdb",
    "ledger",
    "outbox",
    "receipt.py",
    "task_runner.py",
)
CONCURRENCY_PATH_MARKERS = (
    "ws_",
    "live_feed",
    "signalr",
    "hub",
    "routes_market",
    "broadcaster",
    "task_runner.py",
    "watchdog",
)
SECURITY_PATH_MARKERS = (
    "security",
    "auth",
    "config.py",
    "safety_hook",
    "hooks.json",
    "model-policy",
    "arch_check",
)
CALENDAR_PATH_MARKERS = (
    "session",
    "calendar",
    "globex",
    "aggregation",
    "vwap",
)

KNOWN_META_KEYS = frozenset(
    {
        "root task id",
        "task id",
        "packet version",
        "phase",
        "audit refs",
        "base sha",
        "is bugfix",
        "risk tags",
        "budget usd",
        "peer prompt sha256",
    }
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_backticks(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1].strip()
    return text


def _extract_scalar_value(raw: str) -> str:
    text = raw.strip()
    lead_bt = re.match(r"^`([^`]+)`(?:\s*\(.*\))?$", text)
    if lead_bt:
        return lead_bt.group(1).strip()
    if text.startswith("`") and text.count("`") >= 2:
        end_idx = text.find("`", 1)
        if end_idx > 1:
            return text[1:end_idx].strip()
    return text.strip("`").strip()


def _normalize_key(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().lower())


def _normalize_rel_path(raw: str) -> str:
    cleaned = _strip_backticks(raw).replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _has_traversal(path_str: str, *, allow_sibling_charts: bool = False) -> bool:
    if not path_str:
        return True
    if path_str.startswith("/") or re.match(r"^[A-Za-z]:", path_str):
        return True
    parts = [p for p in path_str.split("/") if p]
    if ".." not in parts:
        return False
    if allow_sibling_charts and (
        path_str == "../lightweight-charts"
        or path_str.startswith("../lightweight-charts/")
    ):
        return ".." in parts[1:]
    return True


def _glob_base(pattern: str) -> str | None:
    if pattern.endswith("/**"):
        return pattern[:-3].rstrip("/")
    if pattern.endswith("/*"):
        return pattern[:-2].rstrip("/")
    return None


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in ("*", "?", "["))


def _paths_overlap(owned: str, denied: str) -> bool:
    o = owned.rstrip("/")
    d = denied.rstrip("/")
    if o == d:
        return True
    if fnmatch.fnmatchcase(o, d) or fnmatch.fnmatchcase(d, o):
        return True

    d_base = _glob_base(d)
    if d_base and (o == d_base or o.startswith(d_base + "/")):
        return True
    o_base = _glob_base(o)
    if o_base and (d == o_base or d.startswith(o_base + "/")):
        return True

    if not _has_glob(d) and o.startswith(d + "/"):
        return True
    if not _has_glob(o) and d.startswith(o + "/"):
        return True
    return False


def _parse_bool_strict(raw: str) -> bool | None:
    val = _extract_scalar_value(raw).lower()
    if val in {"yes", "true"}:
        return True
    if val in {"no", "false"}:
        return False
    return None


def _parse_int_strict(raw: str) -> int | None:
    val = _extract_scalar_value(raw)
    if not re.fullmatch(r"-?\d+", val):
        return None
    return int(val)


def _parse_pos_float(raw: str) -> float | None:
    val = _extract_scalar_value(raw)
    try:
        parsed = float(val)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0.0:
        return None
    return parsed


def _parse_risk_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    cleaned = raw.replace("`", "")
    tags: list[str] = []
    for part in cleaned.split(","):
        tag = part.strip().lower()
        if tag and tag != "none":
            tags.append(tag)
    return tags


def _iter_non_fenced_lines(text: str) -> list[tuple[int, str]]:
    in_fence = False
    result: list[tuple[int, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            result.append((idx, line))
    return result


def _parse_packet_structure(
    text: str,
) -> tuple[dict[str, str], list[str], list[str], dict[str, str]]:
    """Parse metadata bullets, owned_paths raw entries, denied_paths, and named sections."""
    lines = _iter_non_fenced_lines(text)
    metadata: dict[str, str] = {}
    owned_raw: list[str] = []
    denied_raw: list[str] = []
    sections: dict[str, list[str]] = {}

    active_scope: str | None = None
    active_scope_indent: int = -1
    active_section: str | None = None
    active_section_level: int = 99

    for _, line in lines:
        stripped = line.strip()
        h_match = HEADING_RE.match(stripped)
        if h_match:
            level = len(h_match.group("level"))
            title = h_match.group("title").strip()
            norm_title = _normalize_key(title.replace("`", ""))
            active_scope = None
            active_scope_indent = -1
            if "owned paths" in norm_title:
                active_scope = "owned"
                active_scope_indent = -1
            elif "denied paths" in norm_title or "do not touch" in norm_title:
                active_scope = "denied"
                active_scope_indent = -1

            if level <= active_section_level:
                active_section = norm_title
                active_section_level = level
                sections.setdefault(active_section, [])
            elif active_section is not None:
                sections[active_section].append(stripped)
                sub_key = norm_title
                sections.setdefault(sub_key, [])
                active_section = sub_key
                active_section_level = level
            continue

        item_match = re.match(r"^(?P<indent>[ \t]*)[-*]\s+(?P<body>.+?)\s*$", line)
        b_match = METADATA_BULLET_RE.match(line)
        b_key_norm = _normalize_key(b_match.group("key")) if b_match else ""
        is_scope_header = b_key_norm.startswith(
            ("owned paths", "denied paths", "do not touch")
        )
        if item_match and active_scope is not None and not is_scope_header:
            indent = len(item_match.group("indent").expandtabs(2))
            if active_scope_indent == -1 or indent > active_scope_indent:
                body = item_match.group("body").strip()
                if active_scope == "owned":
                    owned_raw.append(body)
                else:
                    denied_raw.append(body)
                continue
            active_scope = None
            active_scope_indent = -1

        if b_match:
            indent = len(b_match.group("indent").expandtabs(2))
            key_norm = _normalize_key(b_match.group("key"))
            val = b_match.group("val").strip()

            if active_scope is not None and indent <= active_scope_indent:
                active_scope = None
                active_scope_indent = -1

            if key_norm in KNOWN_META_KEYS and indent <= 1 and key_norm not in metadata:
                metadata[key_norm] = val
            elif key_norm.startswith("owned paths"):
                active_scope = "owned"
                active_scope_indent = indent
            elif key_norm.startswith("denied paths") or key_norm.startswith("do not touch"):
                active_scope = "denied"
                active_scope_indent = indent
            else:
                if active_section is not None:
                    sections[active_section].append(stripped)
                sections.setdefault(key_norm, []).append(val)
            continue

        num_match = re.match(
            r"^\s*\d+\.\s*\*\*(?P<title>[^*]+)\*\*\s*:?\s*(?P<rest>.*)$",
            line,
        )
        if num_match:
            label_norm = _normalize_key(num_match.group("title"))
            rest = num_match.group("rest").strip()
            if active_section is not None:
                sections[active_section].append(stripped)
            sections.setdefault(label_norm, [])
            if rest:
                sections[label_norm].append(rest)
            active_section = label_norm
            active_section_level = 7
            continue

        if active_section is not None and stripped:
            sections[active_section].append(stripped)

    section_text = {k: "\n".join(v).strip() for k, v in sections.items()}
    return metadata, owned_raw, denied_raw, section_text


def _find_section_body(sections: dict[str, str], patterns: tuple[str, ...]) -> str | None:
    for title, body in sections.items():
        if any(re.search(pat, title, re.IGNORECASE) for pat in patterns):
            if body.strip():
                return body.strip()
    return None


def _load_policy(
    repo_root: Path,
    policy_path: Path | None,
) -> tuple[tuple[str, ...], str | None, str | None]:
    target = policy_path.resolve() if policy_path is not None else (repo_root / DEFAULT_POLICY_REL)
    if policy_path is not None and not target.is_file():
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, f"Policy file not found: {target}"
    if not target.is_file():
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, None

    try:
        raw_bytes = target.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, f"Invalid model policy JSON ({target}): {exc}"

    if not isinstance(data, dict):
        return DEFAULT_EXCLUDED_MODEL_PREFIXES, None, "Model policy JSON must be an object."

    prefixes = data.get("excluded_model_prefixes", list(DEFAULT_EXCLUDED_MODEL_PREFIXES))
    if not isinstance(prefixes, list) or not all(
        isinstance(p, str) and p.strip() for p in prefixes
    ):
        return (
            DEFAULT_EXCLUDED_MODEL_PREFIXES,
            None,
            "Policy 'excluded_model_prefixes' must be a list of non-empty strings.",
        )
    return tuple(p.strip() for p in prefixes), _sha256_bytes(raw_bytes), None


def _compute_peer_prompt_sha256(
    repo_root: Path,
    git_top: Path | None,
    base_sha: str | None,
) -> tuple[str | None, str | None]:
    if git_top is not None and base_sha and is_hex_sha40(base_sha):
        try:
            proc = run_managed_cmd(
                ["git", "-C", str(git_top), "show", f"{base_sha}:{PEER_PROMPT_REL}"],
                timeout_sec=30.0,
                text=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return _sha256_bytes(proc.stdout), None
        except (subprocess.TimeoutExpired, OSError):
            pass

    local_prompt = repo_root / PEER_PROMPT_REL
    if local_prompt.is_file():
        raw = local_prompt.read_bytes()
        if raw.strip():
            return _sha256_bytes(raw), None
    return None, f"Missing peer system prompt at {PEER_PROMPT_REL}"


def _build_readiness_payload(
    *,
    root_task_id: str,
    task_id: str,
    packet_version: int,
    phase: int,
    packet_path: str,
    packet_sha256: str | None,
    base_sha: str,
    is_bugfix: bool,
    budget_usd: float,
    owned_paths: list[dict[str, str]],
    denied_paths: list[str],
    peer_prompt_sha256: str | None,
    policy_sha256: str | None,
    risk_flags: dict[str, bool],
    eligible_peer_models: list[str],
    status: str,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "root_task_id": root_task_id,
        "task_id": task_id,
        "packet_version": packet_version,
        "phase": phase,
        "packet_path": packet_path,
        "packet_sha256": packet_sha256,
        "base_sha": base_sha,
        "is_bugfix": is_bugfix,
        "budget_usd": budget_usd,
        "owned_paths": owned_paths,
        "denied_paths": denied_paths,
        "peer_prompt_sha256": peer_prompt_sha256,
        "policy_sha256": policy_sha256,
        "risk_flags": risk_flags,
        "eligible_peer_models": eligible_peer_models,
        "status": status,
        "errors": errors,
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }


def _compute_risk_flags(
    *,
    is_bugfix: bool,
    risk_tags: list[str],
    owned_paths: list[dict[str, str]],
) -> dict[str, bool]:
    tag_set = set(risk_tags)
    paths_lower = [item["path"].lower() for item in owned_paths]

    def _matches(markers: tuple[str, ...]) -> bool:
        return any(m in p for p in paths_lower for m in markers)

    return {
        "is_bugfix": bool(is_bugfix),
        "touches_persistence": ("persistence" in tag_set) or _matches(PERSISTENCE_PATH_MARKERS),
        "touches_concurrency_or_ws": (
            ("concurrency" in tag_set)
            or ("websocket" in tag_set)
            or _matches(CONCURRENCY_PATH_MARKERS)
        ),
        "touches_security_or_auth": (
            ("security" in tag_set) or ("auth" in tag_set) or _matches(SECURITY_PATH_MARKERS)
        ),
        "touches_session_calendar": (
            ("session_calendar" in tag_set) or _matches(CALENDAR_PATH_MARKERS)
        ),
    }


def validate_brief(
    packet_path: Path,
    *,
    repo_root: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Validate a Task Packet against G0.5 Layer 1 & Layer 2 rules."""
    resolved_root = (repo_root or REPO_ROOT).resolve()
    resolved_packet = packet_path.resolve()
    blocked_errors: list[str] = []
    replan_errors: list[str] = []

    excluded_prefixes, policy_sha256, policy_err = _load_policy(resolved_root, policy_path)
    if policy_err:
        blocked_errors.append(policy_err)

    default_flags = {
        "is_bugfix": False,
        "touches_persistence": False,
        "touches_concurrency_or_ws": False,
        "touches_security_or_auth": False,
        "touches_session_calendar": False,
    }

    if not resolved_packet.is_file():
        blocked_errors.append(f"Task packet file not found: {resolved_packet}")
        return _build_readiness_payload(
            root_task_id="",
            task_id="",
            packet_version=0,
            phase=0,
            packet_path=resolved_packet.as_posix(),
            packet_sha256=None,
            base_sha="",
            is_bugfix=False,
            budget_usd=0.0,
            owned_paths=[],
            denied_paths=[],
            peer_prompt_sha256=None,
            policy_sha256=policy_sha256,
            risk_flags=default_flags,
            eligible_peer_models=[],
            status="BLOCKED_BRIEF",
            errors=blocked_errors,
        )

    try:
        raw_bytes = resolved_packet.read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        blocked_errors.append(f"Unable to read task packet {resolved_packet}: {exc}")
        return _build_readiness_payload(
            root_task_id="",
            task_id="",
            packet_version=0,
            phase=0,
            packet_path=resolved_packet.as_posix(),
            packet_sha256=None,
            base_sha="",
            is_bugfix=False,
            budget_usd=0.0,
            owned_paths=[],
            denied_paths=[],
            peer_prompt_sha256=None,
            policy_sha256=policy_sha256,
            risk_flags=default_flags,
            eligible_peer_models=[],
            status="BLOCKED_BRIEF",
            errors=blocked_errors,
        )

    packet_sha256 = _sha256_bytes(raw_bytes)
    if len(raw_bytes) == 0 or not text.strip():
        blocked_errors.append("Task packet is empty (0 bytes or whitespace-only).")
        return _build_readiness_payload(
            root_task_id="",
            task_id="",
            packet_version=0,
            phase=0,
            packet_path=resolved_packet.as_posix(),
            packet_sha256=packet_sha256,
            base_sha="",
            is_bugfix=False,
            budget_usd=0.0,
            owned_paths=[],
            denied_paths=[],
            peer_prompt_sha256=None,
            policy_sha256=policy_sha256,
            risk_flags=default_flags,
            eligible_peer_models=[],
            status="BLOCKED_BRIEF",
            errors=blocked_errors,
        )

    metadata, owned_raw, denied_raw, sections = _parse_packet_structure(text)

    root_task_id = _extract_scalar_value(metadata.get("root task id", ""))
    if not root_task_id:
        blocked_errors.append("Missing required metadata header: Root Task ID.")

    task_id = _extract_scalar_value(metadata.get("task id", ""))
    if not task_id:
        blocked_errors.append("Missing required metadata header: Task ID.")

    packet_version = _parse_int_strict(metadata.get("packet version", ""))
    if packet_version is None or packet_version < 1:
        blocked_errors.append("Missing or invalid Packet Version (requires integer >= 1).")
        packet_version = 0

    phase = _parse_int_strict(metadata.get("phase", ""))
    if phase is None or phase < 0 or phase > 6:
        blocked_errors.append("Missing or invalid Phase (requires integer in 0..6).")
        phase = 0

    base_sha = _extract_scalar_value(metadata.get("base sha", ""))
    if not is_hex_sha40(base_sha):
        blocked_errors.append(
            f"Missing or invalid Base SHA (requires 40-char lowercase hex, got {base_sha!r})."
        )

    is_bugfix_raw = metadata.get("is bugfix", "")
    is_bugfix = _parse_bool_strict(is_bugfix_raw)
    if is_bugfix is None:
        blocked_errors.append(
            f"Missing or invalid Is Bugfix (requires Yes/No/true/false, got {is_bugfix_raw!r})."
        )
        is_bugfix = False

    budget_usd = _parse_pos_float(metadata.get("budget usd", ""))
    if budget_usd is None:
        blocked_errors.append("Missing or invalid Budget USD (requires positive finite float).")
        budget_usd = 0.0

    risk_tags = _parse_risk_tags(metadata.get("risk tags"))

    git_top: Path | None = None
    git_commit_ok = False
    try:
        git_top = resolve_git_toplevel(resolved_root, timeout_sec=30.0)
    except (ValueError, TimeoutError) as exc:
        blocked_errors.append(f"Git repository unavailable: {exc}")

    if git_top is not None and is_hex_sha40(base_sha):
        try:
            rev = run_managed_cmd(
                ["git", "-C", str(git_top), "rev-parse", "--verify", f"{base_sha}^{{commit}}"],
                timeout_sec=30.0,
            )
            if rev.returncode == 0:
                git_commit_ok = True
            else:
                blocked_errors.append(f"Unreachable Base SHA {base_sha} in git repository.")
        except subprocess.TimeoutExpired:
            blocked_errors.append(f"Git timed out verifying Base SHA {base_sha}.")

    owned_paths: list[dict[str, str]] = []
    for entry in owned_raw:
        mode_match = OWNED_MODE_RE.match(entry.strip())
        if not mode_match:
            blocked_errors.append(
                f"Owned path entry missing explicit (new) or (modify) marker: {entry!r}"
            )
            continue
        rel_p = _normalize_rel_path(mode_match.group("path"))
        mode = mode_match.group("mode").lower()
        if _has_traversal(rel_p, allow_sibling_charts=False):
            blocked_errors.append(f"Unsafe or traversal path in Owned Paths: {rel_p!r}")
            continue
        owned_paths.append({"path": rel_p, "mode": mode})

    if not owned_paths and not owned_raw:
        blocked_errors.append("Owned Paths list cannot be empty.")

    denied_paths: list[str] = []
    for entry in denied_raw:
        rel_d = _normalize_rel_path(entry)
        if _has_traversal(rel_d, allow_sibling_charts=True):
            blocked_errors.append(f"Unsafe or traversal path in Denied Paths: {rel_d!r}")
            continue
        denied_paths.append(rel_d)

    if not denied_paths and not denied_raw:
        blocked_errors.append("Denied Paths / Do Not Touch list cannot be empty.")

    for owned_item in owned_paths:
        for denied_item in denied_paths:
            if _paths_overlap(owned_item["path"], denied_item):
                blocked_errors.append(
                    f"Write scope overlap: owned {owned_item['path']!r} conflicts with "
                    f"denied {denied_item!r}."
                )

    peer_prompt_sha256, prompt_err = _compute_peer_prompt_sha256(
        resolved_root,
        git_top,
        base_sha if git_commit_ok else None,
    )
    if prompt_err:
        blocked_errors.append(prompt_err)

    expected_prompt_sha = metadata.get("peer prompt sha256")
    if expected_prompt_sha is not None:
        cleaned_expected = _extract_scalar_value(expected_prompt_sha)
        if not HEX_SHA64_RE.fullmatch(cleaned_expected):
            blocked_errors.append(
                f"Invalid Peer Prompt SHA256 header format: {cleaned_expected!r}."
            )
        elif peer_prompt_sha256 and cleaned_expected != peer_prompt_sha256:
            blocked_errors.append(
                f"Peer Prompt SHA256 mismatch ({cleaned_expected} != {peer_prompt_sha256})."
            )

    checks_body = _find_section_body(
        sections,
        (r"required\s+local\s+checks", r"verification\s+commands"),
    )
    if not checks_body or not CHECK_CMD_RE.search(checks_body):
        blocked_errors.append(
            "Missing non-empty Required Local Checks / Verification Commands section "
            "with executable check commands (arch_check/pytest/ruff/build_verifier/pnpm)."
        )

    if git_top is not None and git_commit_ok:
        for owned_item in owned_paths:
            rel_p = owned_item["path"]
            mode = owned_item["mode"]
            try:
                cat = run_managed_cmd(
                    ["git", "-C", str(git_top), "cat-file", "-t", f"{base_sha}:{rel_p}"],
                    timeout_sec=30.0,
                )
            except subprocess.TimeoutExpired:
                blocked_errors.append(f"Git timed out checking {rel_p!r} in {base_sha}.")
                continue
            exists_blob = cat.returncode == 0 and cat.stdout.strip() == "blob"
            exists_any = cat.returncode == 0
            if mode == "modify" and not exists_blob:
                replan_errors.append(
                    f"Owned path {rel_p!r} marked (modify) does not exist as blob in {base_sha}."
                )
            elif mode == "new" and exists_any:
                replan_errors.append(
                    f"Owned path {rel_p!r} marked (new) already exists in {base_sha}."
                )

    risk_flags = _compute_risk_flags(
        is_bugfix=is_bugfix,
        risk_tags=risk_tags,
        owned_paths=owned_paths,
    )

    non_fenced_lines = [line for _, line in _iter_non_fenced_lines(text)]
    if not any(TEST_SENSITIVITY_RE.search(line) for line in non_fenced_lines):
        replan_errors.append(
            "Missing test sensitivity statement matching 'When ... if ... test fails at ...'."
        )

    if risk_flags["is_bugfix"]:
        red_body = _find_section_body(sections, (r"pre-fix\s+red", r"red\s+reproduction"))
        if not red_body:
            replan_errors.append(
                "Bugfix packet requires a non-empty 'Pre-Fix RED' / 'RED Reproduction' section."
            )

    if risk_flags["touches_persistence"] or risk_flags["touches_concurrency_or_ws"]:
        arc_body = _find_section_body(
            sections,
            (
                r"atomicity.*concurrency\s+contract",
                r"(?:atomicity|rollback|concurrency).*contract",
            ),
        )
        if not arc_body:
            replan_errors.append(
                "Persistence/concurrency packet requires an 'Atomicity / Rollback / "
                "Concurrency Contract' section."
            )

    if risk_flags["touches_security_or_auth"]:
        sec_body = _find_section_body(
            sections,
            (r"security.*read-only", r"security\s*/\s*read-only\s+invariants", r"security.*auth"),
        )
        if not sec_body or not re.search(
            r"(SEC-00[123]|read-only|hook|policy|F01|auth|secret)", sec_body, re.IGNORECASE
        ):
            replan_errors.append(
                "Security/auth packet requires a 'Security / Read-Only Invariants' section "
                "referencing applicable invariants."
            )

    if risk_flags["touches_session_calendar"]:
        cal_body = _find_section_body(sections, (r"session\s+calendar\s+contract",))
        if not cal_body or not re.search(
            r"(America/New_York|18:00|R05)", cal_body, re.IGNORECASE
        ):
            replan_errors.append(
                "Session calendar packet requires a 'Session Calendar Contract' section "
                "referencing America/New_York / 18:00 / R05."
            )

    all_flags_false = not any(risk_flags.values())
    raw_candidates = [ALLOWED_PEER_HIGH]
    if all_flags_false:
        raw_candidates.append(ALLOWED_PEER_LOW)

    lower_prefixes = tuple(p.lower() for p in excluded_prefixes)
    eligible_peer_models = [
        m for m in raw_candidates if not any(m.lower().startswith(p) for p in lower_prefixes)
    ]

    if blocked_errors:
        status = "BLOCKED_BRIEF"
        all_errors = blocked_errors + replan_errors
    elif replan_errors:
        status = "NEEDS_REPLAN"
        all_errors = replan_errors
    else:
        status = "VALID"
        all_errors = []

    return _build_readiness_payload(
        root_task_id=root_task_id,
        task_id=task_id,
        packet_version=packet_version,
        phase=phase,
        packet_path=resolved_packet.as_posix(),
        packet_sha256=packet_sha256,
        base_sha=base_sha,
        is_bugfix=is_bugfix,
        budget_usd=budget_usd,
        owned_paths=owned_paths,
        denied_paths=denied_paths,
        peer_prompt_sha256=peer_prompt_sha256,
        policy_sha256=policy_sha256,
        risk_flags=risk_flags,
        eligible_peer_models=eligible_peer_models,
        status=status,
        errors=all_errors,
    )


def _write_once_json(out_path: Path, payload: dict[str, Any]) -> str | None:
    resolved = out_path.resolve()
    if resolved.exists():
        return f"Write-once refusal: {resolved} already exists."
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        with resolved.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
    except FileExistsError:
        return f"Write-once refusal: {resolved} already exists."
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Task Packet readiness (G0.5).")
    parser.add_argument("--packet", type=Path, required=True, help="Path to packet.md")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Git repository root.")
    parser.add_argument("--out", type=Path, default=None, help="Write-once readiness.json path.")
    parser.add_argument("--policy", type=Path, default=None, help="Optional model-policy.json.")
    parser.add_argument("--json", action="store_true", help="Print readiness JSON to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.out is not None and args.out.resolve().exists():
        msg = f"[brief_gate] BLOCKED_BRIEF — Write-once refusal: {args.out.resolve()} exists."
        print(msg, file=sys.stderr)
        return 2

    result = validate_brief(
        args.packet,
        repo_root=args.repo_root,
        policy_path=args.policy,
    )

    if args.out is not None:
        write_err = _write_once_json(args.out, result)
        if write_err:
            print(f"[brief_gate] BLOCKED_BRIEF — {write_err}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"[brief_gate] status={result['status']} task_id={result['task_id']} "
            f"eligible={result['eligible_peer_models']}"
        )
        for err in result["errors"]:
            print(f"  - {err}", file=sys.stderr)

    if result["status"] == "VALID":
        return 0
    if result["status"] == "NEEDS_REPLAN":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
