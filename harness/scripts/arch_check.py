"""Static architecture, security, Prop Firm Read-Only, and anti-slop checker.

Implements Lauren Tan's Inverted Pyramid enforcement for topstep-chart-app:
every violated invariant emits a concrete ``fix`` instruction explaining
the root-cause correction rather than hiding behind prose rules.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    ".vite",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".references",
    ".agents",
    ".codex-runtime",
}

SKIP_REL_PREFIXES = (
    "harness/evidence/",
    "harness/runs/",
    "docs/",
)

EXEMPT_RE = re.compile(
    r"EXEMPT\(\s*(?P<reason>[^,]+?)\s*,\s*expires=(?P<expires>\d{4}-\d{2}-\d{2})\s*,\s*approved=(?P<approved>[^)]+?)\s*\)"
)

SEC_001_URL_RE = re.compile(r"(/api/order|/api/position|/hubs/user)", re.IGNORECASE)
SEC_001_METHOD_RE = re.compile(r"\b(placeOrder|cancelOrder|modifyOrder|closePosition)\b")

SEC_002_SECRET_RE = re.compile(r"\b(TOPSTEPX_API_KEY|TELEGRAM_BOT_TOKEN|TOPSTEPX_USERNAME)\b")
SEC_002_STORAGE_RE = re.compile(
    r"localStorage\b[^\n]*(apiKey|api_key|topstep[^\n]*token|botToken)",
    re.IGNORECASE,
)

SEC_003_HOST_RE = re.compile(r"""host\s*=\s*['"]0\.0\.0\.0['"]""")
SEC_003_LISTEN_RE = re.compile(r"""\.listen\(\s*[^,)\n]+\s*(?:,\s*(?:\(\s*\)|function\s*\())""")

PY_SLOP_RE = re.compile(r"#\s*(type:\s*ignore|noqa\b)")
JS_SLOP_RE = re.compile(r"(?://|/\*)\s*(@ts-ignore|@ts-expect-error|eslint-disable)\b")
SLOP_002_TIMER_RE = re.compile(r"\b(setTimeout|setInterval)\s*\(")

ARCH_004_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)

FORBIDDEN_ENGINE_IMPORTS = {
    "fastapi",
    "starlette",
    "httpx",
    "websockets",
    "app.api",
    "app.notify",
    "app.topstep",
}
FORBIDDEN_STORAGE_IMPORTS = {"app.api", "app.topstep", "app.notify"}
FORBIDDEN_TOPSTEP_IMPORTS = {"app.api", "app.engine.backtester", "app.mcp"}

MANDATORY_EXCLUDED_MODEL_PREFIXES = (
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-pro-orchestrator-argon",
    "gemini-3.7-flash",
    "gemini-2.5-pro",
)
MOD001_PROHIBITION_RE = re.compile(
    r"\b(exclude|excluded|forbid|forbidden|prohibit|prohibited|never|"
    r"discontinued|retired|banned|denylist|deny|withdrawal|withdrawn|"
    r"mod-001|deprecated|rejected|reject|must\s+not|do\s+not\s+use)\b",
    re.IGNORECASE,
)
MOD001_SUBAGENT_PEER_RE = re.compile(
    r"(?:invoke_subagent\([^\n]*\bModel\s*[:=]\s*['\"]?(?:flash|flash_lite)\b|"
    r"\bpeer-coder\b)",
    re.IGNORECASE,
)
MOD001_PY_DENYLIST_BLOCK_RE = re.compile(
    r"^\s*(?:MANDATORY_EXCLUDED_MODEL_PREFIXES|DEFAULT_EXCLUDED_MODEL_PREFIXES|"
    r"EXCLUDED_MODEL_PREFIXES|DISCONTINUED_SUBAGENT_TIERS|MOD001_)"
)


@dataclass(frozen=True)
class Violation:
    rule_id: str
    file: str
    line: int
    message: str
    fix: str

    def baseline_key(self) -> tuple[str, str, int]:
        return (self.rule_id, self.file, self.line)


def parse_exempt(text: str, today: date | None = None) -> tuple[bool, str | None]:
    """Validate an inline ``EXEMPT(reason, expires=YYYY-MM-DD, approved=owner)`` tag."""
    match = EXEMPT_RE.search(text)
    if not match:
        return False, None
    current_day = today or date.today()
    reason = match.group("reason").strip()
    approved = match.group("approved").strip()
    expires_raw = match.group("expires")
    if not reason or not approved:
        return False, "EXEMPT tag requires non-empty reason and approved owner"
    try:
        expires_date = date.fromisoformat(expires_raw)
    except ValueError:
        return False, f"Invalid EXEMPT expires date: {expires_raw}"
    if expires_date < current_day:
        return False, f"EXEMPT expired on {expires_raw} (today={current_day.isoformat()})"
    return True, None


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _should_skip(path: Path, root: Path) -> bool:
    rel = _rel_posix(path, root)
    if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
        return True
    return any(rel.startswith(prefix) for prefix in SKIP_REL_PREFIXES)


def _is_app_source(rel: str) -> bool:
    if rel.startswith(("backend/app/", "frontend/src/")):
        return True
    return "/" not in rel and Path(rel).suffix in {".mjs", ".js", ".html", ".py"}


def _imported_module_names(tree: ast.AST) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    return imports


def _matches_prefix(mod: str, forbidden: set[str]) -> str | None:
    for prefix in sorted(forbidden):
        if mod == prefix or mod.startswith(prefix + "."):
            return prefix
    return None


def _check_python_imports(tree: ast.AST, rel: str) -> list[Violation]:
    violations: list[Violation] = []
    if rel.startswith("backend/app/engine/"):
        forbidden = FORBIDDEN_ENGINE_IMPORTS
        rule_id = "ARCH-001"
        fix = (
            "Keep backend/app/engine/ pure domain logic. Pass data in via typed arguments "
            "and move HTTP/WebSocket/Topstep/Telegram orchestration to app.api, app.topstep, or app.notify."
        )
    elif rel.startswith("backend/app/storage/"):
        forbidden = FORBIDDEN_STORAGE_IMPORTS
        rule_id = "ARCH-002"
        fix = (
            "Keep backend/app/storage/ isolated from API routes, TopstepX clients, and Telegram. "
            "Expose storage repositories consumed by upper layers instead."
        )
    elif rel.startswith("backend/app/topstep/"):
        forbidden = FORBIDDEN_TOPSTEP_IMPORTS
        rule_id = "ARCH-003"
        fix = (
            "Keep backend/app/topstep/ focused solely on TopstepX auth/REST/SignalR transport. "
            "Do not couple it to FastAPI routes, backtester, or MCP server."
        )
    else:
        return violations

    for lineno, mod in _imported_module_names(tree):
        matched = _matches_prefix(mod, forbidden)
        if matched:
            violations.append(
                Violation(
                    rule_id=rule_id,
                    file=rel,
                    line=lineno,
                    message=f"Forbidden layer import '{mod}' (matches '{matched}') in {rel}",
                    fix=fix,
                )
            )
    return violations


def _check_python_utcnow(tree: ast.AST, rel: str) -> list[Violation]:
    if not rel.startswith(("backend/app/engine/", "backend/app/storage/")):
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "utcnow"
        ):
            violations.append(
                Violation(
                    rule_id="ARCH-005",
                    file=rel,
                    line=node.lineno,
                    message="Naive datetime.utcnow() is forbidden in engine/storage (R05 CME calendar contract).",
                    fix="Use timezone-aware datetime.now(timezone.utc) and app.engine.session_calendar helpers.",
                )
            )
    return violations


def _is_swallowed_except(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return False
    meaningful = [
        stmt
        for stmt in handler.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    if len(meaningful) != 1 or not isinstance(meaningful[0], ast.Pass):
        return False
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
        return True
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(elt, ast.Name) and elt.id in {"Exception", "BaseException"}
            for elt in handler.type.elts
        )
    return False


def _is_skip_or_xfail(expr: ast.expr) -> str | None:
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Attribute) and target.attr in {"skip", "xfail", "skipif"}:
        return target.attr
    return None


def _check_python_tests(tree: ast.AST, lines: list[str], rel: str, today: date) -> list[Violation]:
    if not rel.startswith(("backend/tests/", "harness/tests/")):
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and bool(node.test.value) is True:
                violations.append(
                    Violation(
                        rule_id="TEST-001",
                        file=rel,
                        line=node.lineno,
                        message="Self-confirming literal truthy assert detected (e.g. assert True).",
                        fix="Assert a concrete contract outcome on real product output instead of a constant.",
                    )
                )
            elif isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and len(node.test.comparators) == 1:
                op = node.test.ops[0]
                if isinstance(op, (ast.Eq, ast.Is)) and ast.dump(node.test.left) == ast.dump(node.test.comparators[0]):
                    violations.append(
                        Violation(
                            rule_id="TEST-001",
                            file=rel,
                            line=node.lineno,
                            message="Tautological comparison comparing identical expressions (assert x == x).",
                            fix="Compare the actual system output against an independently derived expected value.",
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.ExceptHandler) and _is_swallowed_except(inner):
                    violations.append(
                        Violation(
                            rule_id="TEST-001",
                            file=rel,
                            line=inner.lineno,
                            message=f"Swallowed exception ('except ...: pass') inside {node.name}.",
                            fix="Let failures surface visibly or assert the specific expected exception with pytest.raises.",
                        )
                    )
            for dec in node.decorator_list:
                mark_name = _is_skip_or_xfail(dec)
                if mark_name:
                    line_text = lines[dec.lineno - 1] if 0 < dec.lineno <= len(lines) else ""
                    ok, reason = parse_exempt(line_text, today=today)
                    if not ok:
                        extra = f" ({reason})" if reason else ""
                        violations.append(
                            Violation(
                                rule_id="TEST-001",
                                file=rel,
                                line=dec.lineno,
                                message=f"Unapproved @pytest.mark.{mark_name} weakens the test gate{extra}.",
                                fix="Remove skip/xfail and fix the test, or add EXEMPT(<reason>, expires=YYYY-MM-DD, approved=<owner>).",
                            )
                        )
        elif isinstance(node, ast.Assign):
            is_pytestmark = any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
            if is_pytestmark:
                mark_name = _is_skip_or_xfail(node.value)
                if mark_name:
                    line_text = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
                    ok, reason = parse_exempt(line_text, today=today)
                    if not ok:
                        extra = f" ({reason})" if reason else ""
                        violations.append(
                            Violation(
                                rule_id="TEST-001",
                                file=rel,
                                line=node.lineno,
                                message=f"Unapproved module pytestmark.{mark_name} weakens the test gate{extra}.",
                                fix="Remove skip/xfail or add EXEMPT(<reason>, expires=YYYY-MM-DD, approved=<owner>).",
                            )
                        )
    return violations


def _scan_common_security(lines: list[str], rel: str) -> list[Violation]:
    violations: list[Violation] = []
    if not _is_app_source(rel):
        return violations
    for idx, line in enumerate(lines, start=1):
        if SEC_001_URL_RE.search(line) or SEC_001_METHOD_RE.search(line):
            violations.append(
                Violation(
                    rule_id="SEC-001",
                    file=rel,
                    line=idx,
                    message="Prop Firm Hard Read-Only violation: forbidden TopstepX order/position/user-hub reference.",
                    fix="Remove all /api/Order, /api/Position, /hubs/user, and order mutation symbols. TopstepX access must remain 100% passive Read-Only.",
                )
            )
        if SEC_003_HOST_RE.search(line) or SEC_003_LISTEN_RE.search(line):
            violations.append(
                Violation(
                    rule_id="SEC-003",
                    file=rel,
                    line=idx,
                    message="Blind 0.0.0.0 / unspecified host bind violates F01 caller-auth boundary.",
                    fix="Bind explicitly to APP_BIND_HOST (default 127.0.0.1) or configured Tailscale IP; never bind all interfaces implicitly.",
                )
            )
    return violations


def _scan_python_file(path: Path, rel: str, today: date) -> list[Violation]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    violations = _scan_common_security(lines, rel)

    if rel.startswith(("backend/", "harness/scripts/")):
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for tok_type, tok_str, start, _, _ in tokens:
                if tok_type == tokenize.COMMENT and PY_SLOP_RE.search(tok_str):
                    ok, reason = parse_exempt(tok_str, today=today)
                    if not ok:
                        extra = f" ({reason})" if reason else ""
                        violations.append(
                            Violation(
                                rule_id="SLOP-001",
                                file=rel,
                                line=start[0],
                                message=f"Unapproved lint/type suppression comment '{tok_str.strip()}'{extra}.",
                                fix="Fix the underlying type or lint error at its root cause, or document EXEMPT(<reason>, expires=YYYY-MM-DD, approved=<owner>).",
                            )
                        )
        except tokenize.TokenError:
            pass

    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        violations.append(
            Violation(
                rule_id="SYNTAX-001",
                file=rel,
                line=exc.lineno or 1,
                message=f"Python syntax error prevents static analysis: {exc.msg}",
                fix="Fix the Python syntax error before running verification gates.",
            )
        )
        return violations

    violations.extend(_check_python_imports(tree, rel))
    violations.extend(_check_python_utcnow(tree, rel))
    violations.extend(_check_python_tests(tree, lines, rel, today))
    return violations


def _scan_js_ts_html_file(path: Path, rel: str, today: date) -> list[Violation]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    violations = _scan_common_security(lines, rel)

    check_secrets = rel.startswith("frontend/src/") or path.suffix == ".html"
    check_drawings = rel.startswith("frontend/src/drawings/")
    check_slop_comments = rel.startswith("frontend/")
    check_raw_timers = rel.startswith(("frontend/src/chart/", "frontend/src/drawings/"))

    for idx, line in enumerate(lines, start=1):
        if check_secrets and (SEC_002_SECRET_RE.search(line) or SEC_002_STORAGE_RE.search(line)):
            violations.append(
                Violation(
                    rule_id="SEC-002",
                    file=rel,
                    line=idx,
                    message="Zero-Secret-in-Browser violation: TopstepX/Telegram credential reference in client code.",
                    fix="Keep all API keys, usernames, and tokens strictly in backend .env; expose only redacted status metadata.",
                )
            )
        if check_drawings:
            for match in ARCH_004_IMPORT_RE.finditer(line):
                target = match.group(1)
                if any(seg in target for seg in ("/replay", "/backtest", "/agent")):
                    violations.append(
                        Violation(
                            rule_id="ARCH-004",
                            file=rel,
                            line=idx,
                            message=f"Drawing primitive '{rel}' imports forbidden module '{target}'.",
                            fix="Keep frontend/src/drawings/ reusable and decoupled from replay, backtest, and agent modules.",
                        )
                    )
        if check_slop_comments and JS_SLOP_RE.search(line):
            ok, reason = parse_exempt(line, today=today)
            if not ok:
                extra = f" ({reason})" if reason else ""
                violations.append(
                    Violation(
                        rule_id="SLOP-001",
                        file=rel,
                        line=idx,
                        message=f"Unapproved TypeScript/ESLint suppression comment{extra}.",
                        fix="Fix the underlying type/lint issue or add EXEMPT(<reason>, expires=YYYY-MM-DD, approved=<owner>).",
                    )
                )
        if check_raw_timers and SLOP_002_TIMER_RE.search(line):
            ok, reason = parse_exempt(line, today=today)
            if not ok:
                extra = f" ({reason})" if reason else ""
                violations.append(
                    Violation(
                        rule_id="SLOP-002",
                        file=rel,
                        line=idx,
                        message=f"Raw timer call in chart/drawings risks GC jank and stale state{extra}.",
                        fix="Drive updates from Lightweight Charts events/requestAnimationFrame lifecycle or add EXEMPT(<reason>, expires=YYYY-MM-DD, approved=<owner>).",
                    )
                )
    return violations


def _check_upstream_fork(root: Path) -> list[Violation]:
    sibling = root.parent / "lightweight-charts"
    if not (sibling / ".git").exists():
        return []
    checks = [
        (["git", "-C", str(sibling), "diff", "--name-only"], "unstaged change"),
        (["git", "-C", str(sibling), "diff", "--cached", "--name-only"], "staged change"),
        (
            ["git", "-C", str(sibling), "ls-files", "--others", "--exclude-standard"],
            "untracked file",
        ),
    ]
    dirty: list[str] = []
    for cmd, kind in checks:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return [
                Violation(
                    rule_id="ARCH-006",
                    file="../lightweight-charts",
                    line=1,
                    message=f"Unable to verify upstream lightweight-charts cleanliness ({kind}): {proc.stderr.strip()}",
                    fix="Ensure sibling lightweight-charts git worktree is readable and clean.",
                )
            ]
        for ln in proc.stdout.splitlines():
            item = ln.strip()
            if item:
                dirty.append(f"{kind}: {item}")
    if dirty:
        return [
            Violation(
                rule_id="ARCH-006",
                file="../lightweight-charts",
                line=1,
                message=f"Upstream fork lightweight-charts/ has {len(dirty)} uncommitted change(s): {dirty[0]}",
                fix="Revert unintended edits in ../lightweight-charts/ so the upstream fork stays clean for git pull upstream.",
            )
        ]
    return []


def _validate_model_policy_file(
    root: Path,
) -> tuple[tuple[str, ...], list[Violation]]:
    policy_path = root / "harness" / "model-policy.json"
    rel = "harness/model-policy.json"
    fix = (
        "Maintain valid harness/model-policy.json with all mandatory "
        "excluded_model_prefixes and zero excluded models in active roles."
    )
    has_governance_surface = (
        (root / "AGENTS.md").is_file()
        or (root / ".agents").is_dir()
        or (root / "harness" / "codex-room").is_dir()
    )
    if not policy_path.is_file():
        if not has_governance_surface:
            return MANDATORY_EXCLUDED_MODEL_PREFIXES, []
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, [
            Violation(
                rule_id="MOD-001",
                file=rel,
                line=1,
                message="Missing required harness/model-policy.json governance file.",
                fix=fix,
            )
        ]
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, [
            Violation(
                rule_id="MOD-001",
                file=rel,
                line=1,
                message=f"Invalid JSON in harness/model-policy.json: {exc}",
                fix=fix,
            )
        ]
    if not isinstance(data, dict):
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, [
            Violation(
                rule_id="MOD-001",
                file=rel,
                line=1,
                message="harness/model-policy.json root must be a JSON object.",
                fix=fix,
            )
        ]

    prefixes = data.get("excluded_model_prefixes")
    if not isinstance(prefixes, list) or not all(
        isinstance(p, str) and p.strip() for p in prefixes
    ):
        return MANDATORY_EXCLUDED_MODEL_PREFIXES, [
            Violation(
                rule_id="MOD-001",
                file=rel,
                line=1,
                message=(
                    "harness/model-policy.json 'excluded_model_prefixes' "
                    "must be non-empty strings."
                ),
                fix=fix,
            )
        ]

    present_low = {p.strip().lower() for p in prefixes}
    missing = [p for p in MANDATORY_EXCLUDED_MODEL_PREFIXES if p.lower() not in present_low]
    merged = tuple(
        dict.fromkeys([*MANDATORY_EXCLUDED_MODEL_PREFIXES, *(p.strip() for p in prefixes)])
    )
    violations: list[Violation] = []
    if missing:
        violations.append(
            Violation(
                rule_id="MOD-001",
                file=rel,
                line=1,
                message=f"harness/model-policy.json missing mandatory prefixes: {missing}",
                fix=fix,
            )
        )

    roles = data.get("roles", {})
    if isinstance(roles, dict):
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
                if isinstance(val, str) and any(
                    val.strip().lower().startswith(p.lower()) for p in merged
                ):
                    violations.append(
                        Violation(
                            rule_id="MOD-001",
                            file=rel,
                            line=1,
                            message=(
                                f"Excluded model {val!r} configured in "
                                f"roles.{role_key}.{field}."
                            ),
                            fix=fix,
                        )
                    )
            if role_key == "independent_reviewer_fallbacks":
                routes = role_val.get("qualified_routes", [])
                if isinstance(routes, list):
                    for item in routes:
                        m_val = item.get("model") if isinstance(item, dict) else item
                        if isinstance(m_val, str) and any(
                            m_val.strip().lower().startswith(p.lower()) for p in merged
                        ):
                            violations.append(
                                Violation(
                                    rule_id="MOD-001",
                                    file=rel,
                                    line=1,
                                    message=f"Excluded model {m_val!r} in qualified_routes.",
                                    fix=fix,
                                )
                            )
    return merged, violations


def _collect_mod001_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    for rel_file in ("AGENTS.md", "docs/WORKSPACE_PROTOCOL.md", "harness/README.md"):
        candidate = root / rel_file
        if candidate.is_file():
            targets.append(candidate)
    for rel_dir in (".agents/skills", "harness/codex-room", "harness/scripts"):
        base_dir = root / rel_dir
        if not base_dir.is_dir():
            continue
        for item in sorted(base_dir.rglob("*")):
            if not item.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache"} for part in item.parts):
                continue
            if item.suffix.lower() in {".md", ".py", ".toml", ".json", ".yaml", ".yml", ".txt"}:
                targets.append(item)
    return targets


def _scan_model_policy_compliance(root: Path, today: date) -> list[Violation]:
    prefixes, violations = _validate_model_policy_file(root)
    escaped = "|".join(re.escape(p) for p in prefixes)
    excl_re = re.compile(rf"\b(?:{escaped})(?:-[a-z0-9._-]+)?\b", re.IGNORECASE)
    fix_msg = (
        "Replace excluded models and discontinued peer-coder/flash dispatch with "
        "qualified models from harness/model-policy.json (e.g. swarm with gemini-4-hydrogen-high)."
    )

    for path in _collect_mod001_targets(root):
        rel = _rel_posix(path, root)
        is_py = path.suffix.lower() == ".py"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        in_py_denylist = False
        paren_depth = 0
        for idx, line in enumerate(lines, start=1):
            if is_py:
                if MOD001_PY_DENYLIST_BLOCK_RE.match(line):
                    in_py_denylist = True
                    paren_depth = (
                        line.count("(") + line.count("[") - line.count(")") - line.count("]")
                    )
                    if paren_depth <= 0:
                        in_py_denylist = False
                    continue
                if in_py_denylist:
                    paren_depth += (
                        line.count("(") + line.count("[") - line.count(")") - line.count("]")
                    )
                    if paren_depth <= 0:
                        in_py_denylist = False
                    continue

            has_excl = bool(excl_re.search(line))
            has_legacy_peer = bool(MOD001_SUBAGENT_PEER_RE.search(line))
            if not (has_excl or has_legacy_peer):
                continue

            if MOD001_PROHIBITION_RE.search(line):
                continue
            ok, _ = parse_exempt(line, today=today)
            if ok:
                continue

            matched = excl_re.search(line) or MOD001_SUBAGENT_PEER_RE.search(line)
            tok = matched.group(0) if matched else "excluded-model"
            violations.append(
                Violation(
                    rule_id="MOD-001",
                    file=rel,
                    line=idx,
                    message=f"Active/unprohibited excluded model or legacy peer dispatch '{tok}'.",
                    fix=fix_msg,
                )
            )
    return violations


def run_checks(
    root: Path,
    baseline_path: Path | None = None,
    use_baseline: bool = True,
    today: date | None = None,
) -> dict[str, object]:
    """Scan ``root`` for architecture, security, and test-quality violations."""
    current_day = today or date.today()
    root = root.resolve()
    all_violations: list[Violation] = []
    scanned_files = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file() or _should_skip(path, root):
            continue
        rel = _rel_posix(path, root)
        suffix = path.suffix.lower()
        if suffix == ".py":
            scanned_files += 1
            all_violations.extend(_scan_python_file(path, rel, current_day))
        elif suffix in {".ts", ".tsx", ".js", ".mjs", ".html"}:
            scanned_files += 1
            all_violations.extend(_scan_js_ts_html_file(path, rel, current_day))

    all_violations.extend(_check_upstream_fork(root))
    all_violations.extend(_scan_model_policy_compliance(root, current_day))

    baseline_keys: set[tuple[str, str, int]] = set()
    if use_baseline and baseline_path and baseline_path.exists():
        raw_base = json.loads(baseline_path.read_text(encoding="utf-8"))
        for item in raw_base.get("legacy_violations", []):
            baseline_keys.add((str(item["rule_id"]), str(item["file"]), int(item["line"])))

    active: list[Violation] = []
    baselined: list[Violation] = []
    for v in all_violations:
        if v.baseline_key() in baseline_keys:
            baselined.append(v)
        else:
            active.append(v)

    return {
        "root": str(root),
        "scanned_files": scanned_files,
        "status": "PASSED" if not active else "FAILED",
        "active_count": len(active),
        "baselined_count": len(baselined),
        "violations": [asdict(v) for v in active],
        "baselined": [asdict(v) for v in baselined],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run topstep-chart-app static architecture and security gates.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Target topstep-chart-app directory (defaults to repository root).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional path to arch-baseline.json (defaults to <root>/harness/arch-baseline.json).",
    )
    parser.add_argument("--no-baseline", action="store_true", help="Ignore legacy baseline suppressions.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    baseline_path = args.baseline or (root / "harness" / "arch-baseline.json")
    result = run_checks(root, baseline_path=baseline_path, use_baseline=not args.no_baseline)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"[arch_check] status={result['status']} scanned={result['scanned_files']} "
            f"active={result['active_count']} baselined={result['baselined_count']}"
        )
        for v in result["violations"]:
            print(f"  FAIL [{v['rule_id']}] {v['file']}:{v['line']} - {v['message']}")
            print(f"       FIX: {v['fix']}")

    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
