"""Windows-native Codex Room runtime sync for topstep-chart-app Lead Review.

Creates isolated CODEX_HOME directories outside the git repository so each
review pass uses its own session traces/SQLite while sharing auth.json and
enforcing read-only sandbox + disabled native multi-agent features.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAYS_DIR = REPO_ROOT / "harness" / "codex-room" / "overlays"

ROLE_MAP: dict[str, tuple[str, str]] = {
    "pass1": ("topstep-review-pass1", "review-pass1-correctness.config.toml"),
    "pass2": ("topstep-review-pass2", "review-pass2-security.config.toml"),
}
EXPECTED_REVIEW_MODEL = "gpt-6.1-sol"
EXPECTED_REASONING_EFFORT = "high"

BARE_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return _sha256_bytes(path.read_bytes())


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into a copy of ``base``."""
    merged = copy.deepcopy(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], val)
        else:
            merged[key] = copy.deepcopy(val)
    return merged


def apply_safety_locks(cfg: dict[str, Any], model_override: str | None = None) -> dict[str, Any]:
    """Enforce non-overridable read-only Codex Room invariants."""
    if model_override is not None and model_override != EXPECTED_REVIEW_MODEL:
        raise ValueError(
            f"Primary Codex Room reviewer model is locked to {EXPECTED_REVIEW_MODEL!r}, got {model_override!r}"
        )
    locked = copy.deepcopy(cfg)
    locked["model"] = EXPECTED_REVIEW_MODEL
    locked["model_reasoning_effort"] = EXPECTED_REASONING_EFFORT
    locked["sandbox_mode"] = "read-only"
    locked["approval_policy"] = "never"
    locked.pop("agents", None)

    features = locked.get("features")
    if not isinstance(features, dict):
        features = {}
    features["multi_agent"] = False
    features["multi_agent_v2"] = False
    features["agent_message_board"] = False
    features.pop("dangerously-bypass-approvals-and-sandbox", None)
    locked["features"] = features
    return locked


def normalize_toml_v11(src: str) -> str:
    """Normalize TOML 1.1 multiline inline tables so Python 3.14 tomllib (TOML 1.0) parses them."""
    out: list[str] = []
    i = 0
    n = len(src)
    brace_depth = 0
    string_mode: str | None = None

    while i < n:
        ch = src[i]

        if string_mode is not None:
            if string_mode in ('"""', "'''"):
                if src.startswith(string_mode, i):
                    out.append(string_mode)
                    i += 3
                    string_mode = None
                    continue
                if string_mode == '"""' and ch == "\\" and i + 1 < n:
                    out.append(src[i : i + 2])
                    i += 2
                    continue
                out.append(ch)
                i += 1
                continue

            if string_mode == '"' and ch == "\\" and i + 1 < n:
                out.append(src[i : i + 2])
                i += 2
                continue
            out.append(ch)
            if ch == string_mode:
                string_mode = None
            i += 1
            continue

        if src.startswith('"""', i) or src.startswith("'''", i):
            delim = src[i : i + 3]
            out.append(delim)
            string_mode = delim
            i += 3
            continue

        if ch in ('"', "'"):
            out.append(ch)
            string_mode = ch
            i += 1
            continue

        if ch == "#":
            end_nl = src.find("\n", i)
            if brace_depth > 0:
                if end_nl == -1:
                    break
                out.append(" ")
                i = end_nl + 1
                continue
            if end_nl == -1:
                out.append(src[i:])
                break
            out.append(src[i : end_nl + 1])
            i = end_nl + 1
            continue

        if ch == "{":
            brace_depth += 1
            out.append(ch)
            i += 1
            continue

        if ch == "}":
            if brace_depth > 0:
                # Drop trailing comma before '}' permitted in TOML 1.1 inline tables.
                j = len(out) - 1
                while j >= 0 and out[j] in (" ", "\t"):
                    j -= 1
                if j >= 0 and out[j] == ",":
                    del out[j:]
                brace_depth -= 1
            out.append(ch)
            i += 1
            continue

        if brace_depth > 0 and ch in ("\r", "\n"):
            out.append(" ")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def loads_toml(src: str) -> dict[str, Any]:
    """Parse TOML string, normalizing TOML 1.1 inline tables when needed."""
    try:
        return tomllib.loads(src)
    except tomllib.TOMLDecodeError:
        return tomllib.loads(normalize_toml_v11(src))


def _escape_basic_string(val: str) -> str:
    escaped = (
        val.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    out = []
    for ch in escaped:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return f'"{"".join(out)}"'


def _format_toml_key(key: str) -> str:
    if key and all(ch in BARE_KEY_CHARS for ch in key):
        return key
    return _escape_basic_string(key)


def _format_toml_string(val: str) -> str:
    if "\n" in val and "\r" not in val:
        body = val.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        if body.endswith('"'):
            body = body[:-1] + '\\"'
        return f'"""\n{body}"""'
    return _escape_basic_string(val)


def _format_toml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        return _format_toml_string(val)
    if isinstance(val, list):
        if all(isinstance(item, dict) for item in val):
            return "[" + ", ".join(_format_inline_table(item) for item in val) + "]"
        return "[" + ", ".join(_format_toml_value(item) for item in val) + "]"
    if isinstance(val, dict):
        return _format_inline_table(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    raise TypeError(f"Unsupported TOML value type: {type(val).__name__}")


def _format_inline_table(tbl: dict[str, Any]) -> str:
    parts = [f"{_format_toml_key(k)} = {_format_toml_value(v)}" for k, v in tbl.items()]
    return "{ " + ", ".join(parts) + " }"


def _emit_table(lines: list[str], data: dict[str, Any], prefix: list[str]) -> None:
    scalars: list[tuple[str, Any]] = []
    nested_dicts: list[tuple[str, dict[str, Any]]] = []
    array_tables: list[tuple[str, list[dict[str, Any]]]] = []

    for k, v in data.items():
        if isinstance(v, dict):
            nested_dicts.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(item, dict) for item in v):
            array_tables.append((k, v))
        else:
            scalars.append((k, v))

    if prefix and (scalars or (not nested_dicts and not array_tables)):
        header = ".".join(_format_toml_key(p) for p in prefix)
        lines.append(f"[{header}]")

    for k, v in scalars:
        lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")

    for k, list_items in array_tables:
        header = ".".join(_format_toml_key(p) for p in [*prefix, k])
        for item in list_items:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[[{header}]]")
            _emit_table_body_only(lines, item, [*prefix, k])

    for k, subdict in nested_dicts:
        if lines and lines[-1] != "":
            lines.append("")
        _emit_table(lines, subdict, [*prefix, k])


def _emit_table_body_only(lines: list[str], data: dict[str, Any], prefix: list[str]) -> None:
    scalars: list[tuple[str, Any]] = []
    nested_dicts: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            nested_dicts.append((k, v))
        else:
            scalars.append((k, v))
    for k, v in scalars:
        lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
    for k, subdict in nested_dicts:
        lines.append("")
        _emit_table(lines, subdict, [*prefix, k])


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize nested dict to TOML and validate exact round-trip via tomllib."""
    lines: list[str] = []
    _emit_table(lines, data, [])
    rendered = "\n".join(lines).rstrip() + "\n"
    reparsed = tomllib.loads(rendered)
    if reparsed != data:
        raise ValueError("TOML round-trip mismatch detected before writing config.toml")
    return rendered


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(content)
            fh.flush()
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _atomic_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dst.parent,
            prefix=f".{dst.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            with src.open("rb") as src_fh:
                shutil.copyfileobj(src_fh, fh)
            fh.flush()
        os.replace(tmp_path, dst)
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def sync_role(
    role: str,
    base_home: Path,
    runtime_root: Path,
    overlays_dir: Path = OVERLAYS_DIR,
    model_override: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge base Codex config + role overlay and write isolated runtime dir."""
    if role not in ROLE_MAP:
        raise ValueError(f"Unknown Codex Room role '{role}'. Expected one of: {sorted(ROLE_MAP)}")

    runtime_name, overlay_filename = ROLE_MAP[role]
    overlay_path = overlays_dir / overlay_filename
    if not overlay_path.is_file():
        raise FileNotFoundError(f"Missing Codex Room overlay: {overlay_path}")

    base_config_path = base_home / "config.toml"
    base_cfg: dict[str, Any] = {}
    if base_config_path.is_file():
        base_cfg = loads_toml(base_config_path.read_text(encoding="utf-8"))

    overlay_cfg = loads_toml(overlay_path.read_text(encoding="utf-8"))
    merged = apply_safety_locks(deep_merge(base_cfg, overlay_cfg), model_override=model_override)
    rendered_toml = dumps_toml(merged)

    runtime_dir = runtime_root / runtime_name
    auth_src = base_home / "auth.json"
    auth_present = auth_src.is_file()

    receipt = {
        "role": role,
        "runtime_dir": str(runtime_dir),
        "base_home": str(base_home),
        "base_config_present": base_config_path.is_file(),
        "base_config_sha256": _sha256_file(base_config_path),
        "overlay_path": str(overlay_path),
        "overlay_sha256": _sha256_file(overlay_path),
        "rendered_config_sha256": _sha256_bytes(rendered_toml.encode("utf-8")),
        "auth_copied": False,
        "model": merged.get("model"),
        "sandbox_mode": merged.get("sandbox_mode"),
        "approval_policy": merged.get("approval_policy"),
        "features": {
            "multi_agent": merged["features"]["multi_agent"],
            "multi_agent_v2": merged["features"]["multi_agent_v2"],
            "agent_message_board": merged["features"]["agent_message_board"],
        },
        "dry_run": dry_run,
        "synced_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    if not dry_run:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(runtime_dir / "config.toml", rendered_toml)
        if auth_present:
            _atomic_copy_file(auth_src, runtime_dir / "auth.json")
            receipt["auth_copied"] = True
        import json

        _atomic_write_text(runtime_dir / "sync-receipt.json", json.dumps(receipt, indent=2) + "\n")

    return receipt


def verify_role(role: str, runtime_root: Path) -> dict[str, Any]:
    """Verify an existing isolated Codex Room runtime satisfies all safety invariants."""
    runtime_name, _ = ROLE_MAP[role]
    runtime_dir = runtime_root / runtime_name
    cfg_path = runtime_dir / "config.toml"
    errors: list[str] = []

    if not cfg_path.is_file():
        return {"role": role, "runtime_dir": str(runtime_dir), "ok": False, "errors": ["Missing config.toml"]}

    try:
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return {"role": role, "runtime_dir": str(runtime_dir), "ok": False, "errors": [f"Invalid TOML: {exc}"]}

    if cfg.get("model") != EXPECTED_REVIEW_MODEL:
        errors.append(f"Expected model={EXPECTED_REVIEW_MODEL!r}, got {cfg.get('model')!r}")
    if cfg.get("model_reasoning_effort") != EXPECTED_REASONING_EFFORT:
        errors.append(
            f"Expected model_reasoning_effort={EXPECTED_REASONING_EFFORT!r}, got {cfg.get('model_reasoning_effort')!r}"
        )
    if cfg.get("sandbox_mode") != "read-only":
        errors.append(f"Expected sandbox_mode='read-only', got {cfg.get('sandbox_mode')!r}")
    if cfg.get("approval_policy") != "never":
        errors.append(f"Expected approval_policy='never', got {cfg.get('approval_policy')!r}")
    if "agents" in cfg:
        errors.append("Forbidden [agents] table present in runtime config")

    features = cfg.get("features", {})
    for flag in ("multi_agent", "multi_agent_v2", "agent_message_board"):
        if features.get(flag) is not False:
            errors.append(f"Expected features.{flag}=false, got {features.get(flag)!r}")

    return {
        "role": role,
        "runtime_dir": str(runtime_dir),
        "ok": not errors,
        "auth_present": (runtime_dir / "auth.json").is_file(),
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync isolated Codex Room runtimes for topstep-chart-app.")
    parser.add_argument("--role", choices=["pass1", "pass2", "all"], default="all")
    parser.add_argument("--base-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".codex-runtime")
    parser.add_argument("--model", type=str, default=None, help="Optional Codex model slug override.")
    parser.add_argument("--dry-run", action="store_true", help="Validate merge and TOML without writing.")
    parser.add_argument("--verify-only", action="store_true", help="Verify synced runtimes without modifying.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    roles = ["pass1", "pass2"] if args.role == "all" else [args.role]

    if args.verify_only:
        all_ok = True
        for role in roles:
            res = verify_role(role, args.runtime_root)
            status = "OK" if res["ok"] else "FAIL"
            print(f"[codex_room_sync:verify] {role} ({res['runtime_dir']}): {status} auth={res.get('auth_present')}")
            for err in res["errors"]:
                print(f"  - {err}")
            if not res["ok"]:
                all_ok = False
        return 0 if all_ok else 1

    for role in roles:
        receipt = sync_role(
            role=role,
            base_home=args.base_home,
            runtime_root=args.runtime_root,
            model_override=args.model,
            dry_run=args.dry_run,
        )
        print(
            f"[codex_room_sync] role={role} runtime={receipt['runtime_dir']} "
            f"auth_copied={receipt['auth_copied']} dry_run={receipt['dry_run']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
