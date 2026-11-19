"""Runtime verification driver for topstep-chart-app (Lauren Tan control skill).

Subcommands:
  doctor  - Read-only check of tools, build artifacts, .env presence, git & Codex Room.
  launch  - Start app on 127.0.0.1 with isolated temp data dir and track PID.
  smoke   - Probe live app endpoints for Read-Only compliance and zero-secret safety.
  cleanup - Terminate only PIDs started by launch while preserving saved evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
    from harness.scripts.codex_room_sync import verify_role
except ImportError:
    from codex_room_sync import verify_role

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "harness" / "evidence"
ACTIVE_RUN_FILE = EVIDENCE_DIR / "active-run.json"

SECRET_FIELD_NAMES = (
    "apiKey",
    "api_key",
    "TOPSTEPX_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "botToken",
    "token",
    "password",
)
RAW_SECRET_VALUE_RE = re.compile(
    r"""(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]+|"""
    r"""\b(?:apiKey|api_key|TOPSTEPX_API_KEY|TELEGRAM_BOT_TOKEN|botToken|access_token|token|password)"""
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
    pass1_runtime = verify_role("pass1", runtime_root)
    pass2_runtime = verify_role("pass2", runtime_root)

    required_tools_ok = all(tools[k] is not None for k in ("python", "uv", "ruff", "pytest", "node", "pnpm", "git"))
    bundle_ok = lwc_bundle.is_file()
    lwc_clean_ok = lwc_git["clean"] is not False

    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "ok": required_tools_ok and bundle_ok and lwc_clean_ok,
        "tools": tools,
        "lightweight_charts_bundle_exists": bundle_ok,
        "lightweight_charts_bundle_path": str(lwc_bundle),
        "lightweight_charts_git": lwc_git,
        "topstep_git_initialized": app_git_proc.returncode == 0,
        "topstep_git_sha": app_git_proc.stdout.strip() if app_git_proc.returncode == 0 else None,
        "env_file_present": (root / ".env").is_file(),
        "backend_present": (root / "backend" / "pyproject.toml").is_file(),
        "frontend_present": (root / "frontend" / "package.json").is_file(),
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
            if k in SECRET_FIELD_NAMES and v not in (None, "", False):
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
