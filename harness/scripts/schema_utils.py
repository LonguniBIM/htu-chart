"""Shared strict JSON Schema validation and frozen git-archive helpers.

Provides strict primitive checks (rejecting string booleans and bool-as-int),
recursive JSON Schema instance validation, and deterministic read-only
materialization of immutable git commits for verification and review.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

HEX_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def is_strict_int(value: Any) -> bool:
    """Return True only for actual integers (never booleans)."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_strict_bool(value: Any) -> bool:
    """Return True only for actual booleans."""
    return isinstance(value, bool)


def is_hex_sha40(value: Any) -> bool:
    """Return True only for lowercase 40-character hex commit SHAs."""
    return isinstance(value, str) and bool(HEX_SHA40_RE.fullmatch(value))


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return is_strict_int(value)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return is_strict_bool(value)
    if schema_type == "null":
        return value is None
    return False


def validate_instance_against_schema(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    """Validate ``instance`` strictly against a JSON Schema subset."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: invalid schema node"]

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if not any(_matches_json_type(instance, str(t)) for t in schema_type):
            errors.append(f"{path}: expected one of types {schema_type!r}, got {type(instance).__name__}")
            return errors
    elif isinstance(schema_type, str):
        if not _matches_json_type(instance, schema_type):
            errors.append(f"{path}: expected type '{schema_type}', got {type(instance).__name__}")
            return errors

    if "enum" in schema:
        allowed = schema["enum"]
        if not any((isinstance(instance, bool) is isinstance(opt, bool)) and instance == opt for opt in allowed):
            errors.append(f"{path}: value {instance!r} is not in enum {allowed!r}")

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for req_key in required:
                if req_key not in instance:
                    errors.append(f"{path}: missing required property '{req_key}'")
        if schema.get("additionalProperties") is False and isinstance(props, dict):
            extra_keys = sorted(k for k in instance if k not in props)
            if extra_keys:
                errors.append(f"{path}: unexpected additionalProperties {extra_keys!r}")
        if isinstance(props, dict):
            for key, subschema in props.items():
                if key in instance and isinstance(subschema, dict):
                    errors.extend(validate_instance_against_schema(instance[key], subschema, f"{path}.{key}"))

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(instance):
                errors.extend(validate_instance_against_schema(item, item_schema, f"{path}[{idx}]"))

    return errors


def cleanup_readonly_tree(path: Path) -> None:
    """Remove a directory tree or file, clearing Windows read-only attributes first."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_file() or path.is_symlink():
        if not path.is_symlink():
            os.chmod(path, stat.S_IWRITE)
        path.unlink(missing_ok=True)
        return

    def _clear_ro(func: Any, target: str, _exc: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_ro)
    else:
        shutil.rmtree(path, onerror=_clear_ro)


def compute_tree_sha256(root_dir: Path) -> tuple[str, int]:
    """Compute a deterministic SHA-256 across sorted relative paths and file bytes."""
    tree_hasher = hashlib.sha256()
    files = sorted(
        (p for p in root_dir.rglob("*") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.relative_to(root_dir).as_posix(),
    )
    for file_path in files:
        rel_posix = file_path.relative_to(root_dir).as_posix()
        file_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        tree_hasher.update(f"{rel_posix}:{file_digest}\n".encode("utf-8"))
    return tree_hasher.hexdigest(), len(files)


def _run_git(
    cmd: list[str],
    *,
    timeout_sec: float = 60.0,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    try:
        return run_managed_cmd(cmd, timeout_sec=timeout_sec, text=not binary)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Git command timed out after {timeout_sec:.1f}s: {' '.join(cmd)}") from exc


def resolve_git_toplevel(repo_root: Path, *, timeout_sec: float = 60.0) -> Path:
    """Resolve the git repository root even when ``repo_root`` is a nested/gitignored subdir."""
    resolved = repo_root.resolve()
    top = _run_git(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"], timeout_sec=timeout_sec)
    if top.returncode != 0 or not top.stdout.strip():
        raise ValueError(f"Cannot resolve git repository root from '{resolved}': {top.stderr.strip()}")
    return Path(top.stdout.strip()).resolve()


def materialize_frozen_tree(
    repo_root: Path,
    candidate_sha: str,
    dest_dir: Path,
    base_sha: str | None = None,
    *,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Extract an immutable ``git archive`` snapshot into ``dest_dir`` and mark files read-only."""
    deadline = (time.monotonic() + timeout_sec) if timeout_sec > 0 else None

    def _rem() -> float:
        if deadline is None:
            return 60.0
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"materialize_frozen_tree timed out after {timeout_sec:.1f}s")
        return rem

    git_top = resolve_git_toplevel(repo_root, timeout_sec=_rem())
    dest_dir = dest_dir.resolve()

    rev = _run_git(["git", "-C", str(git_top), "rev-parse", "--verify", f"{candidate_sha}^{{commit}}"], timeout_sec=_rem())
    if rev.returncode != 0:
        raise ValueError(f"Cannot resolve commit '{candidate_sha}': {rev.stderr.strip()}")
    resolved_sha = rev.stdout.strip()

    cleanup_readonly_tree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    arch = _run_git(["git", "-C", str(git_top), "archive", "--format=tar", resolved_sha], timeout_sec=_rem(), binary=True)
    if arch.returncode != 0:
        err_msg = arch.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git archive failed for {resolved_sha}: {err_msg}")

    with tarfile.open(fileobj=io.BytesIO(arch.stdout), mode="r:") as tf:
        tf.extractall(dest_dir, filter="data")

    frozen_tree_sha256, file_count = compute_tree_sha256(dest_dir)
    for file_path in dest_dir.rglob("*"):
        if file_path.is_file() and not file_path.is_symlink():
            os.chmod(file_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    resolved_base: str | None = None
    patch_path: Path | None = None
    if base_sha and base_sha != "ROOT_COMMIT":
        base_rev = _run_git(
            ["git", "-C", str(git_top), "rev-parse", "--verify", f"{base_sha}^{{commit}}"],
            timeout_sec=_rem(),
        )
        if base_rev.returncode != 0:
            raise ValueError(f"Cannot resolve base commit '{base_sha}': {base_rev.stderr.strip()}")
        resolved_base = base_rev.stdout.strip()
        diff_proc = _run_git(
            ["git", "-C", str(git_top), "diff", "--binary", f"{resolved_base}..{resolved_sha}"],
            timeout_sec=_rem(),
            binary=True,
        )
        if diff_proc.returncode != 0:
            err_msg = diff_proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git diff failed for {resolved_base}..{resolved_sha}: {err_msg}")
        patch_path = dest_dir.parent / "candidate.patch"
        cleanup_readonly_tree(patch_path)
        patch_path.write_bytes(diff_proc.stdout)
        os.chmod(patch_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    return {
        "candidate_sha": resolved_sha,
        "base_sha": resolved_base,
        "dest_dir": str(dest_dir),
        "patch_path": str(patch_path) if patch_path else None,
        "file_count": file_count,
        "frozen_tree_sha256": frozen_tree_sha256,
    }


def _configure_kernel32() -> Any:
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _configure_ntdll() -> Any:
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return ntdll


def _create_kill_on_close_job(k32: Any) -> int:
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _BasicLimitInfo(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInfo),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("Failed to attach Windows Job Object for process-tree containment")

    info = _ExtendedLimitInfo()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        raise RuntimeError("Failed to attach Windows Job Object for process-tree containment")
    return int(job)


def spawn_managed_process(cmd: list[str], **popen_kwargs: Any) -> tuple[subprocess.Popen[Any], int | None]:
    """Spawn a subprocess contained in a Win32 Job Object (from CREATE_SUSPENDED) or POSIX session."""
    kwargs = dict(popen_kwargs)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
        return subprocess.Popen(cmd, **kwargs), None

    from ctypes import wintypes

    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x00000004  # CREATE_SUSPENDED
    proc = subprocess.Popen(cmd, **kwargs)
    job: int | None = None
    try:
        k32 = _configure_kernel32()
        ntdll = _configure_ntdll()
        job = _create_kill_on_close_job(k32)
        proc_handle = wintypes.HANDLE(int(proc._handle))
        if not k32.AssignProcessToJobObject(wintypes.HANDLE(job), proc_handle):
            raise RuntimeError("Failed to attach Windows Job Object for process-tree containment")
        status = ntdll.NtResumeProcess(proc_handle)
        if status != 0:
            raise RuntimeError(f"Failed to attach Windows Job Object for process-tree containment (NtResumeProcess={status})")
        return proc, job
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        if job is not None:
            try:
                _configure_kernel32().CloseHandle(wintypes.HANDLE(job))
            except OSError:
                pass
        raise


def run_managed_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: float = 60.0,
    text: bool = True,
    errors: str = "replace",
) -> subprocess.CompletedProcess[Any]:
    """Run a command inside a managed process tree, killing descendants on timeout."""
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": text,
        "env": env,
    }
    if text:
        popen_kwargs["encoding"] = "utf-8"
        popen_kwargs["errors"] = errors

    proc, job_handle = spawn_managed_process(cmd, **popen_kwargs)
    try:
        effective_timeout = timeout_sec if timeout_sec > 0 else None
        stdout, stderr = proc.communicate(timeout=effective_timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode if proc.returncode is not None else 1,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc, job_handle=job_handle)
        job_handle = None
        try:
            proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        close_job_handle(job_handle)


def close_job_handle(job_handle: int | None) -> None:
    """Close a Win32 Job Object handle if open."""
    if os.name != "nt" or not job_handle:
        return
    from ctypes import wintypes

    k32 = _configure_kernel32()
    k32.CloseHandle(wintypes.HANDLE(job_handle))


def terminate_process_tree(proc: subprocess.Popen[str], job_handle: int | None = None) -> None:
    """Terminate ``proc`` and all descendants, even if the immediate parent already exited."""
    try:
        if os.name == "nt":
            close_job_handle(job_handle)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            except OSError:
                if proc.poll() is None:
                    proc.terminate()
            if proc.poll() is None:
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.15)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if proc.poll() is None:
                    proc.kill()
    except OSError:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        pass
