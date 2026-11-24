"""Antigravity PreToolUse safety hook for topstep-chart-app.

Protects:
  1. Upstream fork lightweight-charts/ ('deny' in Peer worktrees, 'ask' otherwise).
  2. Secret .env* files excluding .env.example ('deny' in Peer, 'ask' otherwise).
  3. Protected harness files (.agents/hooks.json, harness/runs/**, model-policy.json).
  4. Prop Firm Hard Read-Only boundary inside app code (hard 'deny', NC-08 canonicalized).
  5. Model policy & orchestration guards for swarm, invoke_subagent, and run_command.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANDATORY_EXCLUDED_MODEL_PREFIXES = (
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-pro-orchestrator-argon",
    "gemini-3.7-flash",
    "gemini-2.5-pro",
)
DISCONTINUED_SUBAGENT_TIERS = frozenset({"flash", "flash_lite"})

FORBIDDEN_READONLY_URL_RE = re.compile(r"(/api/Order|/api/Position|/hubs/user)", re.IGNORECASE)
FORBIDDEN_READONLY_METHOD_RE = re.compile(
    r"\b(placeOrder|cancelOrder|modifyOrder|closePosition)\b"
)
NET_CLIENT_RE = re.compile(
    r"\b(curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b",
    re.IGNORECASE,
)
SHELL_WRITE_OP_RE = re.compile(
    r"(?<![-=>&])>>?(?![=&>])|\b(?:Out-File|Set-Content|Add-Content|"
    r"Copy-Item|Move-Item|Remove-Item|rm|del)\b",
    re.IGNORECASE,
)
ENV_TARGET_RE = re.compile(r"(?<![A-Za-z0-9_])\.env(?:\.[A-Za-z0-9_-]+)?", re.IGNORECASE)
GIT_COMMIT_HEAD_RE = re.compile(r"^\s*git\b(?:\s+-C\s+\S+)?\s+commit\b", re.IGNORECASE)


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason}


def _ask(reason: str) -> dict[str, str]:
    return {"decision": "ask", "reason": reason}


def _allow() -> dict[str, str]:
    return {"decision": "allow"}


def _extract_path(args: dict[str, Any]) -> str:
    for key in ("Path", "TargetFile", "path", "file_path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_written_text(args: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in (
        "Content",
        "Replacement",
        "CodeContent",
        "content",
        "replacement",
        "ReplacementContent",
    ):
        val = args.get(key)
        if isinstance(val, str):
            chunks.append(val)
    chunks_list = args.get("ReplacementChunks")
    if isinstance(chunks_list, list):
        for chunk in chunks_list:
            if isinstance(chunk, dict) and isinstance(chunk.get("ReplacementContent"), str):
                chunks.append(chunk["ReplacementContent"])
    return "\n".join(chunks)


def _is_peer_worktree(cwd_str: str) -> bool:
    norm = cwd_str.replace("\\", "/").lower()
    return "/worktrees/" in norm or "/swarm-peer-" in norm


def _resolve_target_path(raw_path: str, base_cwd: Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base_cwd / candidate
    return candidate.resolve(strict=False)


def _normalize_model_slug(val: str) -> str:
    return re.sub(r"[\s_()]+", "-", val.strip().lower()).strip("-")


def _matches_excluded_model(model: str, prefixes: tuple[str, ...]) -> bool:
    low = model.strip().lower()
    if not low:
        return False
    slug = _normalize_model_slug(model)
    if "orchestrator-argon" in slug:
        return True
    for prefix in prefixes:
        p_low = prefix.strip().lower()
        p_slug = _normalize_model_slug(prefix)
        if low.startswith(p_low) or slug.startswith(p_slug):
            return True
    return False


def _validate_policy_obj(data: Any) -> str | None:
    if not isinstance(data, dict):
        return "model-policy.json must be a JSON object."
    prefixes = data.get("excluded_model_prefixes")
    if not isinstance(prefixes, list) or not all(
        isinstance(p, str) and p.strip() for p in prefixes
    ):
        return "model-policy.json 'excluded_model_prefixes' must be a list of non-empty strings."
    present_low = {p.strip().lower() for p in prefixes}
    missing = [p for p in MANDATORY_EXCLUDED_MODEL_PREFIXES if p.lower() not in present_low]
    if missing:
        return f"model-policy.json missing mandatory excluded_model_prefixes: {missing}"

    merged_prefixes = tuple(
        dict.fromkeys([*MANDATORY_EXCLUDED_MODEL_PREFIXES, *(p.strip() for p in prefixes)])
    )
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        return "model-policy.json 'roles' must be an object."

    for role_key, role_val in roles.items():
        if not isinstance(role_val, dict):
            continue
        for field in (
            "selected_model",
            "fallback_model",
            "planner_label",
            "internal_executor_label",
        ):
            val = role_val.get(field)
            if isinstance(val, str) and _matches_excluded_model(val, merged_prefixes):
                return f"Excluded model {val!r} found in active role '{role_key}.{field}'."
        if role_key == "independent_reviewer_fallbacks":
            routes = role_val.get("qualified_routes", [])
            if not isinstance(routes, list):
                return "independent_reviewer_fallbacks.qualified_routes must be a list."
            for item in routes:
                model_val = item.get("model") if isinstance(item, dict) else item
                if isinstance(model_val, str) and _matches_excluded_model(
                    model_val, merged_prefixes
                ):
                    return f"Excluded model {model_val!r} found in qualified_routes."
    return None


def _load_disk_policy(base_cwd: Path) -> tuple[tuple[str, ...], str | None]:
    candidates = [base_cwd / "harness" / "model-policy.json"]
    repo_policy = REPO_ROOT / "harness" / "model-policy.json"
    if repo_policy not in candidates:
        candidates.append(repo_policy)
    policy_path = next((p for p in candidates if p.is_file()), None)
    if policy_path is None:
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, None

    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, f"Corrupt model-policy.json: {exc}"

    err = _validate_policy_obj(data)
    if err:
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, err

    prefixes = [p.strip() for p in data["excluded_model_prefixes"]]
    merged = tuple(dict.fromkeys([*MANDATORY_EXCLUDED_MODEL_PREFIXES, *prefixes]))
    return merged, None


def _compute_resulting_policy_text(
    tool_name: str,
    args: dict[str, Any],
    resolved: Path,
) -> str | None:
    is_write_op = (
        tool_name in {"write_file", "write_to_file"}
        or "Content" in args
        or "CodeContent" in args
        or "content" in args
    )
    if is_write_op:
        if bool(args.get("Append") or args.get("append")):
            return None
        for key in ("Content", "CodeContent", "content"):
            if key in args:
                val = args.get(key)
                return val if isinstance(val, str) else None
        return None

    if not resolved.is_file():
        return None

    text = resolved.read_text(encoding="utf-8", errors="replace")
    if "Target" in args and "Replacement" in args:
        target = args["Target"]
        replacement = args["Replacement"]
        if not isinstance(target, str) or not isinstance(replacement, str):
            return None
        if target not in text:
            return None
        return text.replace(target, replacement)

    chunks = args.get("ReplacementChunks")
    if isinstance(chunks, list):
        if not chunks:
            return None
        for chunk in chunks:
            if not isinstance(chunk, dict):
                return None
            target = chunk.get("TargetContent")
            repl = chunk.get("ReplacementContent")
            if not isinstance(target, str) or not isinstance(repl, str) or target not in text:
                return None
            if chunk.get("AllowMultiple") is True:
                text = text.replace(target, repl)
            else:
                text = text.replace(target, repl, 1)
        return text

    if "TargetContent" in args and "ReplacementContent" in args:
        target = args["TargetContent"]
        repl = args["ReplacementContent"]
        if not isinstance(target, str) or not isinstance(repl, str) or target not in text:
            return None
        if args.get("AllowMultiple") is True:
            return text.replace(target, repl)
        return text.replace(target, repl, 1)

    return None


def _is_protected_app_code(rel_posix: str, norm_resolved: str) -> bool:
    if rel_posix.startswith(("backend/app/", "frontend/src/")):
        return True
    if "/backend/app/" in norm_resolved or "/frontend/src/" in norm_resolved:
        return True
    if rel_posix and "/" not in rel_posix and Path(rel_posix).suffix in {".mjs", ".js", ".html"}:
        return True
    if re.search(r"/topstep-chart-app/[^/]+\.(mjs|js|html)$", norm_resolved):
        return True
    if re.search(r"/swarm-peer-[^/]+/[^/]+\.(mjs|js|html)$", norm_resolved):
        return True
    return False


def _evaluate_file_tool(
    tool_name: str,
    args: dict[str, Any],
    base_cwd: Path,
    is_peer: bool,
) -> dict[str, str]:
    raw_path = _extract_path(args)
    if not raw_path:
        return _allow()

    resolved = _resolve_target_path(raw_path, base_cwd)
    norm_resolved = resolved.as_posix().lower()
    base_resolved = base_cwd.resolve(strict=False)
    try:
        rel_posix = resolved.relative_to(base_resolved).as_posix().lower()
    except ValueError:
        rel_posix = ""

    filename = resolved.name.lower()

    if "/lightweight-charts/" in norm_resolved or norm_resolved.endswith("/lightweight-charts"):
        if is_peer:
            return _deny("Peer worktrees must never modify upstream fork lightweight-charts/.")
        return _ask("Upstream fork lightweight-charts/ is protected. Confirm intentional edit.")

    if (filename == ".env" or filename.startswith(".env.")) and filename != ".env.example":
        if is_peer:
            return _deny("Peer worktrees must never read or modify .env secret files.")
        return _ask("Secret environment file is protected. Confirm intentional edit.")

    if rel_posix == ".agents/hooks.json" or norm_resolved.endswith("/.agents/hooks.json"):
        if is_peer:
            return _deny("Peer worktrees must never tamper with .agents/hooks.json.")
        return _ask("Hook configuration .agents/hooks.json is protected. Confirm intentional edit.")

    if rel_posix == "harness/model-policy.json" or norm_resolved.endswith(
        "/harness/model-policy.json"
    ):
        candidate_text = _compute_resulting_policy_text(tool_name, args, resolved)
        if candidate_text is None:
            return _deny("Unable to verify resulting harness/model-policy.json integrity.")
        try:
            parsed = json.loads(candidate_text)
        except json.JSONDecodeError as exc:
            return _deny(f"Invalid JSON in harness/model-policy.json: {exc}")
        err = _validate_policy_obj(parsed)
        if err:
            return _deny(f"Model policy integrity violation: {err}")
        return _allow()

    if _is_protected_app_code(rel_posix, norm_resolved):
        written = _extract_written_text(args)
        if FORBIDDEN_READONLY_URL_RE.search(written) or FORBIDDEN_READONLY_METHOD_RE.search(
            written
        ):
            return _deny(
                "Prop Firm Hard Read-Only Violation: TopstepX order/position/user-hub "
                "endpoints and order mutation calls are strictly forbidden."
            )

    is_harness_runs = (
        rel_posix == "harness/runs"
        or rel_posix.startswith("harness/runs/")
        or (
            rel_posix == ""
            and "/harness/runs/" in norm_resolved
            and "/backend/app/" not in norm_resolved
            and "/frontend/src/" not in norm_resolved
        )
    )
    if is_harness_runs:
        if is_peer:
            return _deny(
                "Peer worktrees must never modify Coordinator/Runner artifacts in harness/runs/."
            )
        return _allow()

    return _allow()


def _evaluate_swarm_tool(
    args: dict[str, Any],
    is_peer: bool,
    excluded_prefixes: tuple[str, ...],
) -> dict[str, str]:
    action = str(args.get("action") or "").strip().lower()
    if is_peer and action in {"add", "kill", "model", "title"}:
        return _deny(f"Peer worktrees are forbidden from executing swarm(action={action!r}).")

    model = args.get("model")
    model_str = model.strip() if isinstance(model, str) else ""
    if action == "add" and not model_str:
        return _deny("swarm(action='add') requires an explicit pinned 'model' ID.")
    if action in {"add", "model"} and model_str and _matches_excluded_model(
        model_str, excluded_prefixes
    ):
        return _deny(f"Model {model_str!r} is excluded by harness/model-policy.json (MOD-001).")
    return _allow()


def _evaluate_invoke_subagent_tool(
    args: dict[str, Any],
    is_peer: bool,
    excluded_prefixes: tuple[str, ...],
) -> dict[str, str]:
    if is_peer:
        return _deny("Peer worktrees must never spawn subagents via invoke_subagent.")

    type_name = str(args.get("TypeName") or args.get("type_name") or "").strip().lower()
    model = str(args.get("Model") or args.get("model") or "").strip()
    role = str(args.get("Role") or args.get("role") or "").strip().lower()

    if model and _matches_excluded_model(model, excluded_prefixes):
        return _deny(f"Subagent model {model!r} is excluded by harness/model-policy.json.")
    if type_name == "peer-coder":  # MOD-001 guard: forbid legacy subagent
        return _deny("Legacy subagent is discontinued; use swarm(workspace='share').")
    if type_name != "browser" and "peer" in role:
        return _deny("Peer roles must be dispatched via swarm(workspace='share'), not subagents.")
    if type_name != "browser" and model.lower() in DISCONTINUED_SUBAGENT_TIERS:
        return _deny(f"Discontinued non-browser subagent tier {model!r} is forbidden (MOD-001).")
    return _allow()


def _has_unquoted_shell_chain(cmd: str) -> bool:
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote is not None:
            if quote == '"' and ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch in (";", "|") or cmd.startswith("&&", i):
            return True
        i += 1
    return False


def _evaluate_run_command_tool(args: dict[str, Any]) -> dict[str, str]:
    cmd = str(args.get("CommandLine") or args.get("command") or args.get("Command") or "").strip()
    if not cmd:
        return _allow()

    if GIT_COMMIT_HEAD_RE.match(cmd) and not _has_unquoted_shell_chain(cmd):
        return _allow()

    if NET_CLIENT_RE.search(cmd) and FORBIDDEN_READONLY_URL_RE.search(cmd):
        return _deny(
            "Prop Firm Hard Read-Only Violation: network call targeting forbidden TopstepX "
            "order/position/user-hub endpoint."
        )

    if SHELL_WRITE_OP_RE.search(cmd):
        lowered = cmd.replace("\\", "/").lower()
        if "lightweight-charts" in lowered or ".agents/hooks.json" in lowered:
            return _deny(
                "Shell modification of lightweight-charts or .agents/hooks.json is denied."
            )
        for match in ENV_TARGET_RE.finditer(cmd):
            if match.group(0).lower() != ".env.example":
                return _deny("Shell modification or removal of .env secret files is denied.")

    return _allow()


def evaluate_hook_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Evaluate a PreToolUse hook payload and return allow/ask/deny decision."""
    raw_cwd = payload.get("cwd")
    cwd_str = raw_cwd.strip() if isinstance(raw_cwd, str) else ""
    if cwd_str:
        cwd_path = Path(cwd_str)
        base_cwd = (
            cwd_path if cwd_path.is_absolute() else (REPO_ROOT / cwd_path)
        ).resolve(strict=False)
    else:
        base_cwd = REPO_ROOT.resolve(strict=False)
    is_peer = _is_peer_worktree(base_cwd.as_posix())

    excluded_prefixes, policy_err = _load_disk_policy(base_cwd)
    if policy_err:
        return _deny(f"Fail-closed model policy error: {policy_err}")

    tool_call = payload.get("toolCall", {})
    if not isinstance(tool_call, dict):
        return _allow()
    tool_name = str(tool_call.get("name") or "").strip().lower()
    args = tool_call.get("args", {})
    if not isinstance(args, dict):
        args = {}

    if tool_name == "swarm":
        return _evaluate_swarm_tool(args, is_peer, excluded_prefixes)
    if tool_name == "invoke_subagent":
        return _evaluate_invoke_subagent_tool(args, is_peer, excluded_prefixes)
    if tool_name == "run_command":
        return _evaluate_run_command_tool(args)

    return _evaluate_file_tool(tool_name, args, base_cwd, is_peer)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps(_allow()))
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps(_deny("Invalid hook payload JSON; failing closed.")))
        return 0

    decision = evaluate_hook_payload(payload)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
