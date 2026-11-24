"""Tests for the 4-case qualification harness (`harness/scripts/reviewer_canary.py`)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.scripts import reviewer_canary

BLOCKING_FINDING = {
    "id": "C-1",
    "severity": "high",
    "category": "defect",
    "file": "backend/app/file.py",
    "line_start": 10,
    "line_end": 12,
    "title": "Detected canary defect",
    "behavior_affected": "Core behavior",
    "trigger_condition": "Error path",
    "evidence": "Line 10",
    "verification_method": "Unit test",
    "plan_ref": "R03",
}


def _write_fake_review_script(
    script_path: Path,
    *,
    mode: str = "all_pass",
    wire_model_override: str | None = None,
) -> Path:
    body = f"""import argparse, hashlib, json, pathlib, sys
parser = argparse.ArgumentParser()
parser.add_argument("--commit", required=True)
parser.add_argument("--base", default=None)
parser.add_argument("--repo-root", required=True)
parser.add_argument("--task-id", required=True)
parser.add_argument("--task-packet", default=None)
parser.add_argument("--out-dir", required=True)
parser.add_argument("--backend", default="agy")
parser.add_argument("--model", required=True)
parser.add_argument("--fallback-model", default=None)
parser.add_argument("--timeout", type=float, default=480.0)
parser.add_argument("--idle-timeout", type=float, default=0.0)
args = parser.parse_args()

out_dir = pathlib.Path(args.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
mode = {mode!r}
wire_model = {wire_model_override!r} or args.model

def write_raw(name: str) -> None:
    raw = out_dir / f"{{name}}.raw.jsonl"
    raw.write_text(
        json.dumps({{"event": "init", "init": {{"model": wire_model}}}}) + "\\n",
        encoding="utf-8",
    )

def make_pass(lens: str, verdict: str, findings: list) -> dict:
    return {{
        "revision_sha": args.commit,
        "lens": lens,
        "verdict": verdict,
        "summary": "canary",
        "findings": findings,
        "test_quality_issues": [],
        "unverified_areas": [],
    }}

finding = {json.dumps(BLOCKING_FINDING)}
p1_findings, p2_findings = [], []
p1_verdict, p2_verdict = "APPROVE", "APPROVE"
gate_status = "PASSED"

cid = args.task_id
if cid == "canary-case1_clean_feature":
    if mode == "false_positive_case1":
        p1_findings = [finding]
        p1_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"
elif cid == "canary-case2_rollback_defect":
    if mode == "miss_case2":
        pass
    elif mode == "wrong_pass_case2":
        p2_findings = [finding]
        p2_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"
    else:
        p1_findings = [finding]
        p1_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"
elif cid == "canary-case3_sec001_order_violation":
    if mode == "miss_case3":
        pass
    elif mode == "wrong_pass_case3":
        p1_findings = [finding]
        p1_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"
    else:
        p2_findings = [finding]
        p2_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"
elif cid == "canary-case4_test001_self_confirming":
    if mode != "miss_case4":
        p1_findings = [finding]
        p1_verdict = "CHANGES_REQUESTED"
        gate_status = "CHANGES_REQUESTED"

write_raw("pass1")
write_raw("pass2")
p1 = make_pass("correctness_and_tests", p1_verdict, p1_findings)
p2 = make_pass("security_and_invariants", p2_verdict, p2_findings)
(out_dir / "pass1.json").write_text(json.dumps(p1), encoding="utf-8")
(out_dir / "pass2.json").write_text(json.dumps(p2), encoding="utf-8")
blocking_cnt = len(p1_findings) + len(p2_findings)
report = {{
    "revision_sha": args.commit,
    "passes_completed": 2,
    "independent_contexts": True,
    "review_gate_status": gate_status,
    "blocking_open_count": blocking_cnt,
    "advisory_count": 0,
    "consolidated_findings": p1_findings + p2_findings,
    "errors": [],
    "frozen_tree_sha256": hashlib.sha256(args.commit.encode()).hexdigest(),
}}
(out_dir / "consolidated-review.json").write_text(json.dumps(report), encoding="utf-8")
"""
    script_path.write_text(body, encoding="utf-8")
    return script_path


def test_reviewer_canary_passes_all_four_cases_and_enforces_write_once(
    tmp_path: Path,
) -> None:
    """When all 4 cases meet their contracts and wire init.model matches, exit 0 once."""
    script = _write_fake_review_script(tmp_path / "fake_rev_ok.py", mode="all_pass")
    out_dir = tmp_path / "canary_ok"
    rc = reviewer_canary.main(
        [
            "--model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(out_dir),
            "--review-script",
            str(script),
            "--max-workers",
            "2",
        ]
    )
    assert rc == 0
    report = json.loads((out_dir / "canary-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["passed_count"] == 4
    assert len(report["cases"]) == 4
    for case in report["cases"]:
        assert case["passed"] is True
        assert case["pass1_wire_model"] == "gemini-4-carbon-high"
        assert case["pass2_wire_model"] == "gemini-4-carbon-high"

    rc_dup = reviewer_canary.main(
        [
            "--model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(out_dir),
            "--review-script",
            str(script),
        ]
    )
    assert rc_dup == 2


def test_reviewer_canary_fails_on_false_positive_missed_defects_and_wire_mismatch(
    tmp_path: Path,
) -> None:
    """False positive on clean case, missed defect on Case 2/3/4, or wire mismatch must fail."""
    scenarios = [
        ("false_positive_case1", None, "case1_clean_feature"),
        ("miss_case2", None, "case2_rollback_defect"),
        ("wrong_pass_case2", None, "case2_rollback_defect"),
        ("miss_case3", None, "case3_sec001_order_violation"),
        ("wrong_pass_case3", None, "case3_sec001_order_violation"),
        ("miss_case4", None, "case4_test001_self_confirming"),
        ("all_pass", "claude-opus-5-5-medium", "case1_clean_feature"),
    ]
    for idx, (mode, wire_override, expected_failed_case) in enumerate(scenarios):
        script = _write_fake_review_script(
            tmp_path / f"fake_rev_{idx}.py",
            mode=mode,
            wire_model_override=wire_override,
        )
        out_dir = tmp_path / f"canary_fail_{idx}"
        rc = reviewer_canary.main(
            [
                "--model",
                "gemini-4-carbon-high",
                "--out-dir",
                str(out_dir),
                "--review-script",
                str(script),
            ]
        )
        assert rc != 0
        report = json.loads((out_dir / "canary-report.json").read_text(encoding="utf-8"))
        assert report["passed"] is False
        failed = next(c for c in report["cases"] if c["case_id"] == expected_failed_case)
        assert failed["passed"] is False


def test_reviewer_canary_rejects_excluded_model_before_running_cases(
    tmp_path: Path,
) -> None:
    """Excluded candidate models must fail closed (exit 2) without running review script."""
    marker = tmp_path / "should_not_exist.txt"
    forbid_script = tmp_path / "forbid_review.py"
    forbid_script.write_text(
        f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\nsys.exit(9)\n",
        encoding="utf-8",
    )
    for bad_model in ("gemini-3.8-flash-high", "Gemini 4 Argon Medium"):
        rc = reviewer_canary.main(
            [
                "--model",
                bad_model,
                "--out-dir",
                str(tmp_path / f"excl_{bad_model[:6]}"),
                "--review-script",
                str(forbid_script),
            ]
        )
        assert rc == 2
        assert not marker.exists()


def test_reviewer_canary_e2e_with_real_codex_room_review_and_fake_agy_cli(
    tmp_path: Path,
) -> None:
    """End-to-end: synthetic repos flow through real codex_room_review.py and fake agy CLI."""
    fake_agy_py = tmp_path / "fake_agy_cli.py"
    fake_agy_py.write_text(
        f"""import json, pathlib, re, sys
args = sys.argv[1:]
model = args[args.index("--model") + 1]
cli_prompt = args[args.index("-p") + 1]
m = re.search(r"`([^`]+\\.prompt\\.md)`", cli_prompt)
prompt_text = pathlib.Path(m.group(1)).read_text(encoding="utf-8") if m else cli_prompt
sha_m = re.search(r"Candidate SHA\\*\\*:\\s*`([0-9a-f]{{40}})`", prompt_text)
sha = sha_m.group(1) if sha_m else "0" * 40
is_pass2 = "security_and_invariants" in prompt_text
lens = "security_and_invariants" if is_pass2 else "correctness_and_tests"
finding = {json.dumps(BLOCKING_FINDING)}
findings = []
verdict = "APPROVE"
frozen_src = pathlib.Path.cwd() / "topstep-chart-app"
py_blob = "\\n".join(
    p.read_text(encoding="utf-8", errors="replace") for p in sorted(frozen_src.rglob("*.py"))
)
if "self.active_contracts.add" in py_blob and not is_pass2:
    findings = [finding]
    verdict = "CHANGES_REQUESTED"
elif "/api/Order/place" in py_blob and is_pass2:
    findings = [finding]
    verdict = "CHANGES_REQUESTED"
elif "patch.object" in py_blob and not is_pass2:
    findings = [finding]
    verdict = "CHANGES_REQUESTED"
payload = {{
    "revision_sha": sha,
    "lens": lens,
    "verdict": verdict,
    "summary": "e2e canary",
    "findings": findings,
    "test_quality_issues": [],
    "unverified_areas": [],
}}
print(json.dumps({{"event": "init", "init": {{"model": model}}}}), flush=True)
res_ev = {{"event": "result", "result": {{"status": "SUCCESS", "structured_output": payload}}}}
print(json.dumps(res_ev), flush=True)
""",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[2]
    wrapper = tmp_path / "real_review_wrapper.py"
    wrapper.write_text(
        (
            "import sys\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from harness.scripts import codex_room_review\n"
            "codex_room_review.resolve_agy_command = lambda: "
            f"[sys.executable, {str(fake_agy_py)!r}]\n"
            "sys.exit(codex_room_review.main())\n"
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "e2e_canary_out"
    rc = reviewer_canary.main(
        [
            "--model",
            "gemini-4-carbon-high",
            "--out-dir",
            str(out_dir),
            "--review-script",
            str(wrapper),
            "--max-workers",
            "2",
            "--timeout",
            "60",
        ]
    )

    assert rc == 0
    report = json.loads((out_dir / "canary-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["passed_count"] == 4
