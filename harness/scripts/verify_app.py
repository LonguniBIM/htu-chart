"""Runtime verification driver for topstep-chart-app (Lauren Tan control skill).

Subcommands:
  doctor  - Read-only check of tools, build artifacts, .env presence, git & Codex Room.
  launch  - Start app on 127.0.0.1 with isolated temp data dir and track PID.
  smoke   - Probe live app endpoints for Read-Only compliance and zero-secret safety.
  cleanup - Terminate only PIDs started by launch while preserving saved evidence.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from harness.scripts.codex_room_sync import (
        is_excluded_model,
        load_model_policy,
        verify_role,
    )
except ImportError:
    from codex_room_sync import is_excluded_model, load_model_policy, verify_role

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "harness" / "evidence"
ACTIVE_RUN_FILE = EVIDENCE_DIR / "active-run.json"

SECRET_FIELD_NAMES = (
    "access_token",
    "app_password_hash",
    "app_session_secret",
    "authorization",
    "apiKey",
    "api_key",
    "TOPSTEPX_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "botToken",
    "token",
    "password",
)
SECRET_FIELD_NAMES_LOWER = frozenset(name.lower() for name in SECRET_FIELD_NAMES)
RAW_SECRET_VALUE_RE = re.compile(
    r"""(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]+|"""
    r"""\b(?:authorization|access_token|apiKey|api_key|TOPSTEPX_API_KEY|"""
    r"""TELEGRAM_BOT_TOKEN|botToken|app_password_hash|app_session_secret|token|password)"""
    r"""["']?\s*[:=]\s*["']?[^\s"'&,}\]]+)"""
)


def _tool_path(name: str) -> str | None:
    for cand in (f"{name}.cmd", f"{name}.exe", f"{name}.ps1", name):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _check_lightweight_charts_clean(sibling: Path) -> dict[str, Any]:
    if not (sibling / ".git").exists():
        return {"present": False, "clean": None, "dirty_items": []}
    dirty_items: list[str] = []
    for args, label in (
        (["diff", "--name-only"], "unstaged"),
        (["diff", "--cached", "--name-only"], "staged"),
        (["ls-files", "--others", "--exclude-standard"], "untracked"),
    ):
        proc = subprocess.run(
            ["git", "-C", str(sibling), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {"present": True, "clean": False, "dirty_items": [proc.stderr.strip()]}
        for ln in proc.stdout.splitlines():
            if ln.strip():
                dirty_items.append(f"{label}:{ln.strip()}")
    return {"present": True, "clean": not dirty_items, "dirty_items": dirty_items}


def _check_model_policy(root: Path) -> dict[str, Any]:
    policy_path = root / "harness" / "model-policy.json"
    data, excluded_prefixes, err = load_model_policy(root, require_exists=True)
    errors: list[str] = [err] if err else []

    if isinstance(data, dict):
        roles = data.get("roles", {})
        if not isinstance(roles, dict):
            errors.append("model-policy.json 'roles' must be an object.")
        else:
            for role_key, role_val in roles.items():
                if not isinstance(role_val, dict):
                    continue
                for field in (
                    "selected_model",
                    "selected_mode",
                    "fallback_model",
                    "planner_label",
                    "internal_executor_label",
                ):
                    val = role_val.get(field)
                    if isinstance(val, str) and is_excluded_model(val, excluded_prefixes):
                        errors.append(
                            f"Excluded model {val!r} configured in roles.{role_key}.{field}."
                        )
                if role_key == "independent_reviewer_fallbacks":
                    routes = role_val.get("qualified_routes", [])
                    if not isinstance(routes, list):
                        errors.append(
                            "independent_reviewer_fallbacks.qualified_routes must be a list."
                        )
                    else:
                        for item in routes:
                            m_val = item.get("model") if isinstance(item, dict) else item
                            if isinstance(m_val, str) and is_excluded_model(
                                m_val, excluded_prefixes
                            ):
                                errors.append(
                                    f"Excluded model {m_val!r} found in qualified_routes."
                                )

    return {
        "ok": not errors,
        "path": str(policy_path),
        "excluded_model_prefixes": list(excluded_prefixes),
        "errors": errors,
    }


def _run_hook_probe(
    argv: list[str],
    agents_dir: Path,
    root: Path,
    tool_id: str,
    rel_path: str,
    content: str,
) -> tuple[str | None, str | None]:
    payload = {
        "hookEventName": "PreToolUse",
        "cwd": str(root),
        "toolCall": {
            "id": tool_id,
            "name": "write_file",
            "args": {"Path": rel_path, "Content": content},
        },
    }
    try:
        proc = subprocess.run(
            argv,
            cwd=str(agents_dir),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Hook execution failed: {exc}"
    if proc.returncode != 0:
        return None, f"Hook exited {proc.returncode}: {proc.stderr.strip()}"
    try:
        out = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        return None, f"Hook emitted invalid JSON: {exc}"
    decision = out.get("decision") if isinstance(out, dict) else None
    if not isinstance(decision, str):
        return None, "Hook JSON missing string 'decision' field."
    return decision, None


def _check_hooks(root: Path) -> dict[str, Any]:
    agents_dir = root / ".agents"
    hooks_path = agents_dir / "hooks.json"
    errors: list[str] = []
    cmd_str: str | None = None
    deny_decision: str | None = None
    allow_decision: str | None = None

    if not hooks_path.is_file():
        errors.append(f"Missing required hook config: {hooks_path}")
        return {
            "ok": False,
            "path": str(hooks_path),
            "command": None,
            "deny_probe": None,
            "allow_probe": None,
            "errors": errors,
        }

    try:
        cfg = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"Malformed .agents/hooks.json: {exc}")
        cfg = None

    if isinstance(cfg, dict):
        guard = cfg.get("topstep-safety-guard")
        if isinstance(guard, dict) and "enabled" in guard:
            enabled_val = guard.get("enabled")
            if not isinstance(enabled_val, bool) or enabled_val is not True:
                errors.append(
                    ".agents/hooks.json topstep-safety-guard.enabled must be boolean true."
                )
        pre_groups = guard.get("PreToolUse") if isinstance(guard, dict) else None
        if not isinstance(pre_groups, list) or not pre_groups:
            errors.append(".agents/hooks.json missing topstep-safety-guard.PreToolUse array.")
        else:
            first_group = pre_groups[0] if isinstance(pre_groups[0], dict) else {}
            matcher = first_group.get("matcher") if isinstance(first_group, dict) else None
            required_tools = (
                "write_file",
                "edit_file",
                "replace_file_content",
                "multi_replace_file_content",
                "write_to_file",
                "run_command",
                "swarm",
                "invoke_subagent",
            )
            if not isinstance(matcher, str) or not matcher.strip():
                errors.append(".agents/hooks.json PreToolUse group missing matcher regex.")
            else:
                try:
                    matcher_re = re.compile(matcher)
                except re.error as exc:
                    errors.append(f".agents/hooks.json invalid matcher regex: {exc}")
                else:
                    missing_tools = [t for t in required_tools if not matcher_re.fullmatch(t)]
                    if missing_tools:
                        errors.append(
                            ".agents/hooks.json matcher misses required tools: "
                            f"{missing_tools}"
                        )
            hooks_list = first_group.get("hooks") if isinstance(first_group, dict) else None
            if not isinstance(hooks_list, list) or not hooks_list:
                errors.append(".agents/hooks.json PreToolUse group missing hooks list.")
            else:
                first_hook = hooks_list[0] if isinstance(hooks_list[0], dict) else {}
                raw_cmd = first_hook.get("command") if isinstance(first_hook, dict) else None
                if (
                    first_hook.get("type") != "command"
                    or not isinstance(raw_cmd, str)
                    or not raw_cmd.strip()
                ):
                    errors.append(
                        ".agents/hooks.json PreToolUse hook requires type='command' and command."
                    )
                else:
                    cmd_str = raw_cmd.strip()
    elif cfg is not None:
        errors.append(".agents/hooks.json root must be a JSON object.")

    if cmd_str:
        argv: list[str] = []
        try:
            raw_argv = shlex.split(cmd_str, posix=(os.name != "nt"))
        except ValueError as exc:
            errors.append(f".agents/hooks.json invalid command syntax: {exc}")
        else:
            argv = [
                tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
                for tok in raw_argv
            ]
            if argv and Path(argv[0]).name.lower() in {"python", "python3", "python.exe"}:
                argv[0] = sys.executable

        if argv:
            deny_decision, deny_err = _run_hook_probe(
                argv,
                agents_dir,
                root,
                "doctor_deny",
                "backend/app/x.py",
                'await client.post("/api/Order/place")',
            )
            if deny_err:
                errors.append(f"deny_probe: {deny_err}")
            elif deny_decision != "deny":
                errors.append(f"deny_probe expected 'deny', got {deny_decision!r}.")

            allow_decision, allow_err = _run_hook_probe(
                argv,
                agents_dir,
                root,
                "doctor_allow",
                "backend/app/engine/instruments.py",
                "MES_TICK_SIZE = 0.25\n",
            )
            if allow_err:
                errors.append(f"allow_probe: {allow_err}")
            elif allow_decision != "allow":
                errors.append(f"allow_probe expected 'allow', got {allow_decision!r}.")

    return {
        "ok": not errors,
        "path": str(hooks_path),
        "command": cmd_str,
        "deny_probe": deny_decision,
        "allow_probe": allow_decision,
        "errors": errors,
    }


def _call_verify_role(role: str, runtime_root: Path, repo_root: Path) -> dict[str, Any]:
    sig = inspect.signature(verify_role)
    has_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD or p.name == "repo_root"
        for p in sig.parameters.values()
    )
    if has_kw:
        return verify_role(role, runtime_root, repo_root=repo_root)
    return verify_role(role, runtime_root)


def run_doctor(root: Path, runtime_root: Path) -> dict[str, Any]:
    """Run read-only diagnostic checks across toolchain, repo, and Codex Room."""
    root = root.resolve()
    tools = {
        name: _tool_path(name)
        for name in ("python", "uv", "ruff", "pytest", "node", "pnpm", "git", "codex")
    }
    lwc_bundle = root.parent / "lightweight-charts" / "dist" / "lightweight-charts.standalone.production.js"
    lwc_git = _check_lightweight_charts_clean(root.parent / "lightweight-charts")

    app_git_proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    pass1_runtime = _call_verify_role("pass1", runtime_root, root)
    pass2_runtime = _call_verify_role("pass2", runtime_root, root)
    hooks_report = _check_hooks(root)
    policy_report = _check_model_policy(root)

    required_tools_ok = all(tools[k] is not None for k in ("python", "uv", "ruff", "pytest", "node", "pnpm", "git"))
    bundle_ok = lwc_bundle.is_file()
    lwc_clean_ok = lwc_git["clean"] is not False

    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "ok": (
            required_tools_ok
            and bundle_ok
            and lwc_clean_ok
            and hooks_report["ok"]
            and policy_report["ok"]
        ),
        "tools": tools,
        "lightweight_charts_bundle_exists": bundle_ok,
        "lightweight_charts_bundle_path": str(lwc_bundle),
        "lightweight_charts_git": lwc_git,
        "topstep_git_initialized": app_git_proc.returncode == 0,
        "topstep_git_sha": app_git_proc.stdout.strip() if app_git_proc.returncode == 0 else None,
        "env_file_present": (root / ".env").is_file(),
        "backend_present": (root / "backend" / "pyproject.toml").is_file(),
        "frontend_present": (root / "frontend" / "package.json").is_file(),
        "hooks": hooks_report,
        "model_policy": policy_report,
        "codex_room": {
            "pass1": pass1_runtime,
            "pass2": pass2_runtime,
        },
    }


def _http_request(url: str, method: str = "GET", timeout: float = 4.0) -> tuple[int, str]:
    req = urllib.request.Request(url=url, method=method)
    if method == "POST":
        req.data = b"{}"
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body


def _find_leaked_secrets(payload: Any, path: str = "$") -> list[str]:
    leaks: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k.lower() in SECRET_FIELD_NAMES_LOWER and v not in (None, "", False):
                leaks.append(f"{path}.{k}")
            leaks.extend(_find_leaked_secrets(v, f"{path}.{k}"))
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            leaks.extend(_find_leaked_secrets(item, f"{path}[{idx}]"))
    elif isinstance(payload, str) and RAW_SECRET_VALUE_RE.search(payload):
        leaks.append(f"{path}:raw_secret_pattern")
    return leaks


def cmd_launch(root: Path, port: int, mode: str) -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    if ACTIVE_RUN_FILE.is_file():
        print(f"[verify_app:launch] Active run already tracked at {ACTIVE_RUN_FILE}. Run cleanup first.", file=sys.stderr)
        return 1

    resolved_mode = mode
    if mode == "auto":
        resolved_mode = "backend" if (root / "backend" / "pyproject.toml").is_file() else "legacy"

    temp_data_dir = Path(tempfile.mkdtemp(prefix="topstep_verify_"))
    env = dict(os.environ)
    env["APP_BIND_HOST"] = "127.0.0.1"
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    env["DATA_DIR"] = str(temp_data_dir)

    if resolved_mode == "backend":
        uv_bin = _tool_path("uv") or "uv"
        cmd = [
            uv_bin,
            "run",
            "--directory",
            str(root / "backend"),
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ]
    else:
        node_bin = _tool_path("node") or "node"
        cmd = [node_bin, str(root / "server.mjs")]

    log_path = EVIDENCE_DIR / f"server-{port}.log"
    log_fh = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    probe_path = "/api/health" if resolved_mode == "backend" else "/api/status"
    url = f"http://127.0.0.1:{port}{probe_path}"
    ready = False
    for _ in range(30):
        if proc.poll() is not None:
            break
        try:
            code, _ = _http_request(url, timeout=1.0)
            if code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.35)

    if not ready:
        proc.terminate()
        log_fh.close()
        shutil.rmtree(temp_data_dir, ignore_errors=True)
        print(f"[verify_app:launch] Server failed to become ready at {url}. See {log_path}", file=sys.stderr)
        return 1

    run_meta = {
        "pid": proc.pid,
        "port": port,
        "mode": resolved_mode,
        "url": f"http://127.0.0.1:{port}",
        "temp_data_dir": str(temp_data_dir),
        "log_path": str(log_path),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    ACTIVE_RUN_FILE.write_text(json.dumps(run_meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(run_meta, indent=2))
    return 0


def cmd_smoke(base_url: str, out_dir: Path | None) -> int:
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = out_dir or (EVIDENCE_DIR / f"smoke-{run_stamp}")
    target_dir.mkdir(parents=True, exist_ok=True)

    root_url = base_url.rstrip("/")
    health_code, health_body = _http_request(f"{root_url}/api/health")
    health_leaks: list[str] = []
    try:
        health_json = json.loads(health_body) if health_body else {}
        health_leaks = _find_leaked_secrets(health_json)
    except json.JSONDecodeError:
        health_leaks = ["invalid_health_json"]

    status_code, status_body = _http_request(f"{root_url}/api/status")
    status_leaks: list[str] = []
    status_json_ok = False
    if not status_body:
        status_leaks = ["empty_status_body"]
    else:
        try:
            status_json = json.loads(status_body)
            status_json_ok = True
            status_leaks = _find_leaked_secrets(status_json)
        except json.JSONDecodeError:
            status_leaks = ["unparseable_status_body"]
            lowered_body = status_body.lower()
            for field in SECRET_FIELD_NAMES:
                if field.lower() in lowered_body:
                    status_leaks.append(f"raw_body:{field}")
            if RAW_SECRET_VALUE_RE.search(status_body):
                status_leaks.append("raw_body:secret_pattern")

    leaks = [*health_leaks, *status_leaks]

    order_code, _ = _http_request(f"{root_url}/api/Order/place", method="POST")
    readonly_ok = order_code in {401, 403, 404, 405}

    passed = health_code == 200 and status_code == 401 and status_json_ok and not leaks and readonly_ok
    evidence = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "health_endpoint_code": health_code,
        "status_endpoint_code": status_code,
        "secret_leaks_detected": leaks,
        "order_endpoint_probe_code": order_code,
        "read_only_boundary_ok": readonly_ok,
        "passed": passed,
    }
    out_file = target_dir / "smoke.json"
    out_file.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0 if passed else 1


def cmd_cleanup() -> int:
    if not ACTIVE_RUN_FILE.is_file():
        print("[verify_app:cleanup] No active run tracked.")
        return 0
    meta = json.loads(ACTIVE_RUN_FILE.read_text(encoding="utf-8"))
    pid = int(meta["pid"])
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    temp_dir = meta.get("temp_data_dir")
    if temp_dir and Path(temp_dir).exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    ACTIVE_RUN_FILE.unlink(missing_ok=True)
    print(f"[verify_app:cleanup] Terminated pid={pid}; preserved evidence in {EVIDENCE_DIR}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TopstepX app runtime verification driver.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="Read-only environment, git, and Codex Room check.")
    p_doc.add_argument("--root", type=Path, default=REPO_ROOT)
    p_doc.add_argument("--runtime-root", type=Path, default=Path.home() / ".codex-runtime")
    p_doc.add_argument("--json", action="store_true")

    p_launch = sub.add_parser("launch", help="Launch app on 127.0.0.1 with isolated temp data.")
    p_launch.add_argument("--root", type=Path, default=REPO_ROOT)
    p_launch.add_argument("--port", type=int, default=18456)
    p_launch.add_argument("--mode", choices=["auto", "backend", "legacy"], default="auto")

    p_smoke = sub.add_parser("smoke", help="Probe endpoints for health, Read-Only, and zero-secret.")
    p_smoke.add_argument("--url", default="http://127.0.0.1:18456")
    p_smoke.add_argument("--out-dir", type=Path, default=None)

    sub.add_parser("cleanup", help="Terminate only tracked launch PIDs while keeping evidence.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        report = run_doctor(args.root, args.runtime_root)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"[doctor] ok={report['ok']} lwc_bundle={report['lightweight_charts_bundle_exists']} "
                f"lwc_clean={report['lightweight_charts_git']['clean']} "
                f"hooks={report['hooks']['ok']} model_policy={report['model_policy']['ok']} "
                f"codex_room_p1={report['codex_room']['pass1']['ok']} "
                f"codex_room_p2={report['codex_room']['pass2']['ok']}"
            )
        return 0 if report["ok"] else 1
    if args.command == "launch":
        return cmd_launch(args.root.resolve(), args.port, args.mode)
    if args.command == "smoke":
        return cmd_smoke(args.url, args.out_dir)
    if args.command == "cleanup":
        return cmd_cleanup()
    return 1


if __name__ == "__main__":
    sys.exit(main())
