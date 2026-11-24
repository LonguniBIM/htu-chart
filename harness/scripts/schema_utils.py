"""Shared strict JSON Schema validation and frozen git-archive helpers.

Provides strict primitive checks (rejecting string booleans and bool-as-int),
recursive JSON Schema instance validation, and deterministic read-only
materialization of immutable git commits for verification and review.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
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


def resolve_git_toplevel(repo_root: Path) -> Path:
    """Resolve the git repository root even when ``repo_root`` is a nested/gitignored subdir."""
    resolved = repo_root.resolve()
    top = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode != 0 or not top.stdout.strip():
        raise ValueError(f"Cannot resolve git repository root from '{resolved}': {top.stderr.strip()}")
    return Path(top.stdout.strip()).resolve()


def materialize_frozen_tree(
    repo_root: Path,
    candidate_sha: str,
    dest_dir: Path,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Extract an immutable ``git archive`` snapshot into ``dest_dir`` and mark files read-only."""
    git_top = resolve_git_toplevel(repo_root)
    dest_dir = dest_dir.resolve()

    rev = subprocess.run(
        ["git", "-C", str(git_top), "rev-parse", "--verify", f"{candidate_sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0:
        raise ValueError(f"Cannot resolve commit '{candidate_sha}': {rev.stderr.strip()}")
    resolved_sha = rev.stdout.strip()

    cleanup_readonly_tree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    arch = subprocess.run(
        ["git", "-C", str(git_top), "archive", "--format=tar", resolved_sha],
        capture_output=True,
        check=False,
    )
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
        base_rev = subprocess.run(
            ["git", "-C", str(git_top), "rev-parse", "--verify", f"{base_sha}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if base_rev.returncode != 0:
            raise ValueError(f"Cannot resolve base commit '{base_sha}': {base_rev.stderr.strip()}")
        resolved_base = base_rev.stdout.strip()
        diff_proc = subprocess.run(
            ["git", "-C", str(git_top), "diff", "--binary", f"{resolved_base}..{resolved_sha}"],
            capture_output=True,
            check=False,
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
