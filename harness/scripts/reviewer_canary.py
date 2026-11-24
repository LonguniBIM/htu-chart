"""4-case disposable qualification canary harness for fallback reviewer models.

Validates a candidate Antigravity CLI (`agy`) reviewer model across four
synthetic isolated git repositories:
  1. case1_clean_feature        -> must PASS with 0 blocking findings
  2. case2_rollback_defect      -> must return CHANGES_REQUESTED (Pass 1)
  3. case3_sec001_order_violation -> must return CHANGES_REQUESTED (Pass 2)
  4. case4_test001_self_confirming -> must return CHANGES_REQUESTED
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from harness.scripts.codex_room_sync import is_excluded_model, load_model_policy
    from harness.scripts.schema_utils import (
        cleanup_readonly_tree,
        is_strict_int,
        run_managed_cmd,
    )
except ImportError:
    from codex_room_sync import is_excluded_model, load_model_policy
    from schema_utils import (
        cleanup_readonly_tree,
        is_strict_int,
        run_managed_cmd,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_SCRIPT = Path(__file__).resolve().with_name("codex_room_review.py")
BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})


@dataclass(frozen=True)
class CanaryCaseSpec:
    """Definition of one disposable synthetic canary repository case."""

    case_id: str
    task_packet: str
    files: dict[str, str]
    expected_status: str
    require_pass1_blocking: bool = False
    require_pass2_blocking: bool = False


CANARY_CASES: tuple[CanaryCaseSpec, ...] = (
    CanaryCaseSpec(
        case_id="case1_clean_feature",
        task_packet=(
            "# Canary Case 1: Clean Feature\n"
            "Implement pure `tick_distance(price_a, price_b, tick_size)` helper "
            "in `backend/app/engine/ticks.py` with concrete boundary unit tests "
            "in `backend/tests/test_ticks.py`.\n"
        ),
        files={
            "backend/app/engine/ticks.py": (
                '"""Pure tick-distance helper for futures price comparisons."""\n\n'
                "from __future__ import annotations\n\n"
                "from decimal import Decimal, InvalidOperation\n\n\n"
                "def tick_distance(\n"
                "    price_a: float | Decimal,\n"
                "    price_b: float | Decimal,\n"
                "    tick_size: float | Decimal,\n"
                ") -> int:\n"
                '    """Return signed integer tick count from price_a to price_b."""\n'
                "    try:\n"
                "        dec_a = Decimal(str(price_a))\n"
                "        dec_b = Decimal(str(price_b))\n"
                "        dec_tick = Decimal(str(tick_size))\n"
                "    except (InvalidOperation, ValueError) as exc:\n"
                '        raise ValueError("Inputs must be finite numbers.") from exc\n'
                "    if not (dec_a.is_finite() and dec_b.is_finite() and dec_tick.is_finite()):\n"
                '        raise ValueError("Inputs must be finite numbers.")\n'
                "    if dec_tick <= 0:\n"
                '        raise ValueError("tick_size must be strictly positive.")\n'
                "    ticks = (dec_b - dec_a) / dec_tick\n"
                "    if ticks != ticks.to_integral_value():\n"
                '        raise ValueError("Price diff must be an exact tick multiple.")\n'
                "    return int(ticks)\n"
            ),
            "backend/tests/test_ticks.py": (
                '"""Boundary tests for tick_distance helper."""\n\n'
                "from __future__ import annotations\n\n"
                "import pytest\n\n"
                "from app.engine.ticks import tick_distance\n\n\n"
                "def test_tick_distance_computes_exact_signed_ticks() -> None:\n"
                "    assert tick_distance(5000.25, 5001.00, 0.25) == 3\n"
                "    assert tick_distance(5001.00, 5000.25, 0.25) == -3\n"
                "    assert tick_distance(5000.00, 5000.00, 0.25) == 0\n\n\n"
                "def test_tick_distance_rejects_invalid_tick_or_non_multiple() -> None:\n"
                "    with pytest.raises(ValueError, match='strictly positive'):\n"
                "        tick_distance(100.0, 101.0, 0.0)\n"
                "    with pytest.raises(ValueError, match='exact tick multiple'):\n"
                "        tick_distance(100.0, 100.1, 0.25)\n"
                "    with pytest.raises(ValueError, match='finite'):\n"
                "        tick_distance(float('nan'), 100.0, 0.25)\n"
            ),
        },
        expected_status="PASSED",
    ),
    CanaryCaseSpec(
        case_id="case2_rollback_defect",
        task_packet=(
            "# Canary Case 2: Async State Rollback Defect\n"
            "Implement atomic `subscribe(contract_id)` in `backend/app/topstep/stream.py`. "
            "On transport failure during `await`, `active_contracts` must not retain "
            "unconfirmed subscriptions.\n"
        ),
        files={
            "backend/app/topstep/stream.py": (
                '"""Market stream subscription manager."""\n\n'
                "from __future__ import annotations\n\n"
                "from typing import Any\n\n\n"
                "class MarketStreamClient:\n"
                "    def __init__(self, transport: Any) -> None:\n"
                "        self._transport = transport\n"
                "        self.active_contracts: set[str] = set()\n\n"
                "    async def subscribe(self, contract_id: str) -> None:\n"
                "        # BUG: mutates state before await without rollback on error.\n"
                "        self.active_contracts.add(contract_id)\n"
                "        await self._transport.send_subscribe(contract_id)\n"
            ),
        },
        expected_status="CHANGES_REQUESTED",
        require_pass1_blocking=True,
    ),
    CanaryCaseSpec(
        case_id="case3_sec001_order_violation",
        task_packet=(
            "# Canary Case 3: Prop Firm Hard Read-Only Check (SEC-001)\n"
            "Per AGENTS.md, TopstepX integration must remain 100% passive read-only: "
            "zero `/api/Order/*`, `/api/Position/*`, `/hubs/user`, or order placement.\n"
        ),
        files={
            "backend/app/topstep/order_client.py": (
                '"""TopstepX client helper."""\n\n'
                "from __future__ import annotations\n\n"
                "from typing import Any\n\n\n"
                "async def submit_order(http_client: Any, account_id: int) -> dict[str, Any]:\n"
                "    resp = await http_client.post(\n"
                '        "/api/Order/place",\n'
                '        json={"accountId": account_id, "size": 1},\n'
                "    )\n"
                "    return resp.json()\n"
            ),
        },
        expected_status="CHANGES_REQUESTED",
        require_pass2_blocking=True,
    ),
    CanaryCaseSpec(
        case_id="case4_test001_self_confirming",
        task_packet=(
            "# Canary Case 4: Anti-Self-Confirming Tests (TEST-001)\n"
            "Per .references/test-required.md, tests must exercise the real SUT "
            "and never mock the function under test or assert tautologies (`x == x`).\n"
        ),
        files={
            "backend/app/engine/pnl.py": (
                '"""Simple PnL helper."""\n\n'
                "from __future__ import annotations\n\n\n"
                "def calculate_pnl(entry: float, exit_p: float, qty: int) -> float:\n"
                "    return (exit_p - entry) * qty * 5.0\n"
            ),
            "backend/tests/test_pnl.py": (
                '"""Tests for calculate_pnl."""\n\n'
                "from __future__ import annotations\n\n"
                "from unittest.mock import patch\n\n"
                "from app.engine import pnl\n\n\n"
                "def test_calculate_pnl_returns_expected() -> None:\n"
                "    with patch.object(pnl, 'calculate_pnl', return_value=250.0):\n"
                "        res = pnl.calculate_pnl(5000.0, 5010.0, 5)\n"
                "        assert res == res\n"
            ),
        },
        expected_status="CHANGES_REQUESTED",
    ),
)


def _slugify_model(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", model.strip()).strip("-") or "model"


def _extract_wire_init_model(raw_jsonl_path: Path) -> str | None:
    if not raw_jsonl_path.is_file():
        return None
    for line in raw_jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("event") != "init":
            continue
        init_obj = ev.get("init")
        if isinstance(init_obj, dict) and isinstance(init_obj.get("model"), str):
            return init_obj["model"]
        if isinstance(ev.get("model"), str):
            return ev["model"]
    return None


def _count_blocking_findings(pass_json_path: Path) -> int:
    if not pass_json_path.is_file():
        return 0
    try:
        data = json.loads(pass_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return 0
    return sum(
        1
        for f in findings
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in BLOCKING_SEVERITIES
    )


def _git_run(repo_dir: Path, args: list[str]) -> str:
    proc = run_managed_cmd(["git", "-C", str(repo_dir), *args], timeout_sec=60.0)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _create_synthetic_repo(repo_dir: Path, spec: CanaryCaseSpec) -> tuple[str, str]:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git_run(repo_dir, ["init"])
    _git_run(repo_dir, ["config", "user.email", "canary@example.com"])
    _git_run(repo_dir, ["config", "user.name", "Canary Runner"])

    base_files = {
        "README.md": "# Synthetic Topstep Canary Repo\n",
        "AGENTS.md": (
            "# Rules\n"
            "- Hard Read-Only Prop Firm: zero /api/Order/*, /api/Position/*, /hubs/user.\n"
            "- Test quality: no mocking SUT, no assert x == x, no swallowed exceptions.\n"
        ),
        "backend/app/__init__.py": "",
        "backend/app/engine/__init__.py": "",
        "backend/app/topstep/__init__.py": "",
        "backend/tests/__init__.py": "",
    }
    for rel, content in base_files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    _git_run(repo_dir, ["add", "."])
    _git_run(repo_dir, ["commit", "-m", "base"])
    base_sha = _git_run(repo_dir, ["rev-parse", "HEAD"])

    for rel, content in spec.files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    _git_run(repo_dir, ["add", "."])
    _git_run(repo_dir, ["commit", "-m", f"canary: {spec.case_id}"])
    cand_sha = _git_run(repo_dir, ["rev-parse", "HEAD"])
    return base_sha, cand_sha


def _evaluate_case(
    spec: CanaryCaseSpec,
    *,
    candidate_model: str,
    out_dir: Path,
    review_script: Path,
    timeout_sec: float,
    idle_timeout_sec: float,
) -> dict[str, Any]:
    temp_root = Path(tempfile.mkdtemp(prefix=f"canary-{spec.case_id}-"))
    repo_dir = temp_root / "repo"
    case_out = out_dir / spec.case_id
    packet_file = temp_root / f"{spec.case_id}.packet.md"
    packet_file.write_text(spec.task_packet, encoding="utf-8")

    failure_reasons: list[str] = []
    rc = -1
    report: dict[str, Any] = {}
    p1_wire_model: str | None = None
    p2_wire_model: str | None = None
    p1_blocking = 0
    p2_blocking = 0

    try:
        base_sha, cand_sha = _create_synthetic_repo(repo_dir, spec)
        cmd = [
            sys.executable,
            str(review_script),
            "--commit",
            cand_sha,
            "--base",
            base_sha,
            "--repo-root",
            str(repo_dir),
            "--task-id",
            f"canary-{spec.case_id}",
            "--task-packet",
            str(packet_file),
            "--out-dir",
            str(case_out),
            "--backend",
            "agy",
            "--model",
            candidate_model,
            "--fallback-model",
            candidate_model,
            "--timeout",
            str(timeout_sec),
            "--idle-timeout",
            str(idle_timeout_sec),
        ]
        subprocess_timeout = (timeout_sec * 2.0 + 60.0) if timeout_sec > 0 else 0.0
        proc = run_managed_cmd(cmd, timeout_sec=subprocess_timeout)
        rc = proc.returncode
        if rc != 0:
            failure_reasons.append(
                f"review_script exited {rc}: {proc.stderr.strip() or proc.stdout.strip()}"
            )

        report_path = case_out / "consolidated-review.json"
        if not report_path.is_file():
            failure_reasons.append("Missing consolidated-review.json")
        else:
            report = json.loads(report_path.read_text(encoding="utf-8"))

        passes = report.get("passes_completed")
        if not is_strict_int(passes) or passes != 2:
            failure_reasons.append(f"Expected passes_completed == 2, got {passes!r}")
        if report.get("independent_contexts") is not True:
            failure_reasons.append(
                f"Expected independent_contexts is True, got {report.get('independent_contexts')!r}"
            )
        if report.get("errors") != []:
            failure_reasons.append(f"Expected errors == [], got {report.get('errors')!r}")
        frozen_sha = report.get("frozen_tree_sha256")
        if not isinstance(frozen_sha, str) or not frozen_sha.strip():
            failure_reasons.append("Missing non-empty frozen_tree_sha256")

        p1_wire_model = _extract_wire_init_model(case_out / "pass1.raw.jsonl")
        p2_wire_model = _extract_wire_init_model(case_out / "pass2.raw.jsonl")
        if p1_wire_model != candidate_model:
            failure_reasons.append(
                f"pass1 wire init.model mismatch: {p1_wire_model!r} != {candidate_model!r}"
            )
        if p2_wire_model != candidate_model:
            failure_reasons.append(
                f"pass2 wire init.model mismatch: {p2_wire_model!r} != {candidate_model!r}"
            )

        gate_status = report.get("review_gate_status")
        if gate_status != spec.expected_status:
            failure_reasons.append(
                f"Expected review_gate_status == {spec.expected_status!r}, got {gate_status!r}"
            )

        blocking_cnt = report.get("blocking_open_count")
        if spec.expected_status == "PASSED":
            if blocking_cnt != 0:
                failure_reasons.append(f"Expected blocking_open_count == 0, got {blocking_cnt!r}")
        else:
            if not is_strict_int(blocking_cnt) or blocking_cnt < 1:
                failure_reasons.append(
                    f"Expected blocking_open_count >= 1, got {blocking_cnt!r}"
                )

        p1_blocking = _count_blocking_findings(case_out / "pass1.json")
        p2_blocking = _count_blocking_findings(case_out / "pass2.json")
        if spec.require_pass1_blocking and p1_blocking < 1:
            failure_reasons.append(
                "Expected Pass 1 (correctness_and_tests) >= 1 blocking finding"
            )
        if spec.require_pass2_blocking and p2_blocking < 1:
            failure_reasons.append(
                "Expected Pass 2 (security_and_invariants) >= 1 blocking finding"
            )
    except Exception as exc:
        failure_reasons.append(f"Canary case raised {type(exc).__name__}: {exc}")
    finally:
        cleanup_readonly_tree(temp_root)

    return {
        "case_id": spec.case_id,
        "passed": len(failure_reasons) == 0,
        "exit_code": rc,
        "expected_status": spec.expected_status,
        "review_gate_status": report.get("review_gate_status"),
        "blocking_open_count": report.get("blocking_open_count"),
        "passes_completed": report.get("passes_completed"),
        "independent_contexts": report.get("independent_contexts"),
        "frozen_tree_sha256": report.get("frozen_tree_sha256"),
        "pass1_wire_model": p1_wire_model,
        "pass2_wire_model": p2_wire_model,
        "pass1_blocking_count": p1_blocking,
        "pass2_blocking_count": p2_blocking,
        "errors": report.get("errors", []),
        "failure_reasons": failure_reasons,
    }


def run_canary(
    *,
    candidate_model: str,
    out_dir: Path,
    review_script: Path = DEFAULT_REVIEW_SCRIPT,
    repo_root: Path = REPO_ROOT,
    max_workers: int = 2,
    timeout_sec: float = 480.0,
    idle_timeout_sec: float = 0.0,
) -> tuple[int, dict[str, Any]]:
    """Execute the 4-case qualification canary and write canary-report.json once."""
    _, excluded_prefixes, policy_err = load_model_policy(repo_root, require_exists=False)
    if policy_err:
        print(f"[reviewer_canary] BLOCKED: {policy_err}", file=sys.stderr)
        return 2, {"passed": False, "error": policy_err}

    if is_excluded_model(candidate_model, excluded_prefixes):
        msg = f"Candidate model {candidate_model!r} is excluded by model-policy.json (MOD-001)."
        print(f"[reviewer_canary] BLOCKED: {msg}", file=sys.stderr)
        return 2, {"passed": False, "error": msg}

    out_dir = out_dir.resolve()
    report_path = out_dir / "canary-report.json"
    if report_path.exists():
        msg = f"Write-once refusal: {report_path} already exists."
        print(f"[reviewer_canary] {msg}", file=sys.stderr)
        return 2, {"passed": False, "error": msg}

    out_dir.mkdir(parents=True, exist_ok=True)
    results_by_id: dict[str, dict[str, Any]] = {}
    workers = max(1, min(max_workers, len(CANARY_CASES)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _evaluate_case,
                spec,
                candidate_model=candidate_model,
                out_dir=out_dir,
                review_script=review_script,
                timeout_sec=timeout_sec,
                idle_timeout_sec=idle_timeout_sec,
            ): spec.case_id
            for spec in CANARY_CASES
        }
        for fut in concurrent.futures.as_completed(futs):
            cid = futs[fut]
            results_by_id[cid] = fut.result()

    ordered_cases = [results_by_id[spec.case_id] for spec in CANARY_CASES]
    passed_count = sum(1 for item in ordered_cases if item["passed"])
    all_passed = passed_count == len(CANARY_CASES)

    summary = {
        "candidate_model": candidate_model,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "out_dir": str(out_dir),
        "review_script": str(review_script),
        "passed": all_passed,
        "overall_passed": all_passed,
        "passed_count": passed_count,
        "total_cases": len(CANARY_CASES),
        "cases": ordered_cases,
    }

    try:
        with report_path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, indent=2) + "\n")
    except FileExistsError:
        msg = f"Write-once refusal: {report_path} already exists."
        print(f"[reviewer_canary] {msg}", file=sys.stderr)
        return 2, {"passed": False, "error": msg}

    return (0 if all_passed else 1), summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run 4-case qualification canary for a fallback reviewer model."
    )
    parser.add_argument("--model", required=True, help="Candidate AGY reviewer model ID.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Write-once output dir.")
    parser.add_argument("--max-workers", type=int, default=2, help="Concurrent case workers.")
    parser.add_argument("--timeout", type=float, default=480.0, help="Per-pass timeout (sec).")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=0.0,
        help="Per-pass idle timeout (sec; 0 disables). Default: 0.",
    )
    parser.add_argument(
        "--review-script",
        type=Path,
        default=DEFAULT_REVIEW_SCRIPT,
        help="Path to codex_room_review.py (override for testing).",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.out_dir is not None:
        out_dir = args.out_dir.resolve()
    else:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = (
            repo_root / "harness" / "runs" / "canary" / f"{_slugify_model(args.model)}-{ts}"
        ).resolve()

    rc, summary = run_canary(
        candidate_model=args.model,
        out_dir=out_dir,
        review_script=args.review_script.resolve(),
        repo_root=repo_root,
        max_workers=args.max_workers,
        timeout_sec=args.timeout,
        idle_timeout_sec=args.idle_timeout,
    )
    if "cases" in summary:
        print(
            f"[reviewer_canary] model={args.model} passed={summary['passed_count']}/"
            f"{summary['total_cases']} overall_passed={summary['passed']} out={out_dir}"
        )
        for case in summary["cases"]:
            status_lbl = "PASS" if case["passed"] else "FAIL"
            reasons = "; ".join(case["failure_reasons"])
            suffix = f" ({reasons})" if reasons else ""
            print(f"  - {case['case_id']}: {status_lbl}{suffix}")
    return rc


if __name__ == "__main__":
    _ = asdict
    sys.exit(main())
