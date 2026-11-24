# Codex Room Review — Pass 2 (Security, Prop Firm Read-Only & Reliability F01–F08 / R01–R06)

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
   - Check `implementation_plan.md` Audit Findings `F01–F08` and `R01–R06`, plus `AGENTS.md` and `docs/WORKSPACE_PROTOCOL.md` inside `{{FROZEN_SRC_DIR}}`.
2. Verify Prop Firm Hard Read-Only Compliance:
   - Confirm zero `/api/Order/*`, `/api/Position/*`, `/hubs/user`, or order placement paths in backend, frontend, or MCP tools.
3. Verify Security & Reliability Boundaries:
   - `F01`: Loopback/Tailscale bind, caller session + CSRF + Host/Origin validation, zero secret leakage in responses/logs/frontend.
   - `F02`/`F04`/`F05`/`R04`: `(provider, live_mode, contract_id, timeframe)` isolation, OHLCV validation, per-dataset locking + Windows-safe atomic replacement.
   - `F03`/`F07`/`R03`/`R06`: Non-blocking startup, REST timeouts, reconnect reconciliation, durable TTL alert outbox, WS bounded queue.
   - `R01`/`R02`: Backtest `ProcessPoolExecutor` isolation and fail-closed OS/container sandbox for arbitrary Python.
4. Set `"revision_sha": "{{CANDIDATE_SHA}}"` and `"lens": "security_and_invariants"`.
5. Output ONLY JSON conforming to the output schema.
