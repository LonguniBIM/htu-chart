# Codex Room Review — Pass 1 (Correctness, Concurrency & Test Quality)

Review **strictly read-only** against immutable git commit:
- **Candidate SHA**: `{{CANDIDATE_SHA}}`
- **Base SHA**: `{{BASE_SHA}}`
- **Frozen Snapshot Dir**: `{{FROZEN_SRC_DIR}}`
- **Candidate Diff Patch**: `{{PATCH_PATH}}`

## Changed Files
```text
{{CHANGED_FILES}}
```

## Diff Stat
```text
{{DIFF_STAT}}
```

## Task Packet / Acceptance Contract
{{TASK_PACKET}}

## Mandatory Inspection Instructions
1. Inspect ONLY the immutable frozen snapshot at `{{FROZEN_SRC_DIR}}` and the candidate patch at `{{PATCH_PATH}}` (never read dirty working-tree files outside the frozen snapshot):
   - Read changed files inside `{{FROZEN_SRC_DIR}}` and review the diff in `{{PATCH_PATH}}` (or read-only git inspection if `.git` is available).
   - Read surrounding modules (`AGENTS.md`, `implementation_plan.md`, `docs/statecharts.md`, `.references/test-required.md`) inside `{{FROZEN_SRC_DIR}}` as needed to verify callers and invariants.
2. Check correctness, state transitions, race conditions, CME Globex session/timestamp contracts (`R05`), stale-response guards (`F06`), and backtest/replay causality (`F08`).
3. Audit every new or modified test against `.references/test-required.md` §§1–5:
   - Flag tests that mock the logic under test, mirror production formulas, assert only truthiness/not-null, swallow exceptions, or weaken gates.
4. Set `"revision_sha": "{{CANDIDATE_SHA}}"` and `"lens": "correctness_and_tests"`.
5. Output ONLY JSON conforming to the output schema.
