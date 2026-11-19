"""Antigravity PreToolUse safety hook for topstep-chart-app.

Protects:
  1. Upstream fork lightweight-charts/ (prompts via 'ask').
  2. Secret .env* files excluding .env.example (prompts via 'ask').
  3. Prop Firm Hard Read-Only boundary inside app code (hard 'deny').
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_READONLY_RE = re.compile(r"(/api/Order|/api/Position|/hubs/user)", re.IGNORECASE)


def _extract_path(args: dict[str, Any]) -> str:
    for key in ("Path", "TargetFile", "path", "file_path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_written_text(args: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("Content", "Replacement", "CodeContent", "content", "replacement", "ReplacementContent"):
        val = args.get(key)
        if isinstance(val, str):
            chunks.append(val)
    chunks_list = args.get("ReplacementChunks")
    if isinstance(chunks_list, list):
        for chunk in chunks_list:
            if isinstance(chunk, dict) and isinstance(chunk.get("ReplacementContent"), str):
                chunks.append(chunk["ReplacementContent"])
    return "\n".join(chunks)


def evaluate_hook_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Evaluate a PreToolUse hook payload and return allow/ask/deny decision."""
    tool_call = payload.get("toolCall", {})
    args = tool_call.get("args", {}) if isinstance(tool_call, dict) else {}
    raw_path = _extract_path(args)
    norm_path = raw_path.replace("\\", "/").lower()
    filename = Path(norm_path).name if norm_path else ""

    if "/lightweight-charts/" in norm_path or norm_path.endswith("/lightweight-charts"):
        return {
            "decision": "ask",
            "reason": "Upstream fork lightweight-charts/ is protected by harness policy. Confirm intentional edit.",
        }

    if (filename == ".env" or filename.startswith(".env.")) and filename != ".env.example":
        return {
            "decision": "ask",
            "reason": "Secret environment file is protected. Confirm intentional edit.",
        }

    is_app_code = (
        "/topstep-chart-app/backend/app/" in norm_path
        or "/topstep-chart-app/frontend/src/" in norm_path
    )
    if is_app_code:
        written = _extract_written_text(args)
        if FORBIDDEN_READONLY_RE.search(written):
            return {
                "decision": "deny",
                "reason": "Prop Firm Hard Read-Only Violation: TopstepX order/position/user-hub endpoints are strictly forbidden.",
            }

    return {"decision": "allow"}


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"decision": "allow"}))
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"decision": "allow"}))
        return 0

    decision = evaluate_hook_payload(payload)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
