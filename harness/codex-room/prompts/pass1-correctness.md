# Codex Room Review — Pass 1 (Correctness, Concurrency & Test Quality)

Review **strictly read-only** against immutable git commit:
- **Candidate SHA**: `{{CANDIDATE_SHA}}`
- **Base SHA**: `{{BASE_SHA}}`

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
1. Inspect the exact commit snapshot using read-only git commands:
   - `git show {{CANDIDATE_SHA}}` (or `git diff {{BASE_SHA}} {{CANDIDATE_SHA}}` when `Base SHA != ROOT_COMMIT`)
   - Read surrounding modules (`AGENTS.md`, `implementation_plan.md`, `docs/statecharts.md`, `.references/test-required.md`) as needed to verify callers and invariants.
2. Check correctness, state transitions, race conditions, CME Globex session/timestamp contracts (`R05`), stale-response guards (`F06`), and backtest/replay causality (`F08`).
3. Audit every new or modified test against `.references/test-required.md` §§1–5:
   - Flag tests that mock the logic under test, mirror production formulas, assert only truthiness/not-null, swallow exceptions, or weaken gates.
4. Set `"revision_sha": "{{CANDIDATE_SHA}}"` and `"lens": "correctness_and_tests"`.
5. Output ONLY JSON conforming to the output schema.
