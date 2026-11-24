# `topstep-chart-app` Harness Operations Guide

The harness synthesizes three engineering control systems:
1. **Small-Codebase S-L-P Topology**:
   - **Coordinator** (`gemini-4-neon-medium` CLI default; App `Orchestrator` `Gemini 4 Neon Medium` planner + `Gemini 4 Hydrogen High` internal executor forbidden from editing Peer-owned product files): Guards scope & Prop Firm Read-Only compliance, authors Task Packets, runs `brief_gate.py` (`G0.5`), debates Peer signals as equal colleagues, verifies commit reachability before `swarm(action="kill")`, runs `build_verifier.py` (`G3`), dispatches Independent Review (`G4`), and issues write-once receipts (`G5`).
   - **Peer** (`swarm(action="add", model="gemini-4-hydrogen-high", workspace="share")` default; `gemini-4-hydrogen-low` constrained to `G0.5` zero-risk pure domain packets): Implements the scoped Task Packet in an isolated `share` worktree, writes value-first tests per `test-required.md`, challenges flawed specs (`REOPEN_REQUEST`, `DEPENDENCY_REQUEST`, `BLOCKED`) via `post_message`, and delivers an immutable `CANDIDATE_READY` commit SHA. Legacy `peer-coder` subagents and `flash` tiers are discontinued (`MOD-001`).
   - **Independent Reviewer** (`codex-room`: `topstep-review-pass1` & `topstep-review-pass2` on `gpt-6.1-sol` `high` primary; canary-qualified `agy` routes only per `harness/model-policy.json`): Executes 2 independent **read-only** passes against a materialized `frozen-src` snapshot of the exact candidate SHA.
2. **Lauren Tan Inverted Pyramid**: Enforces invariants mechanically via `.agents/hooks.json`, `safety_hook.py`, `arch_check.py`, `brief_gate.py`, `task_runner.py`, `build_verifier.py`, `verify_app.py`, statecharts, and the feature map.
3. **`test-required.md`**: Value-first tests protecting observable behavior, zero self-confirming mocks/tautologies, mandatory pre-fix **RED → GREEN** evidence for bug fixes, 2-pass consolidated review on identical SHAs, and write-once receipts (`VERIFIED` / `FAILED` / `BLOCKED`).

---

## Daily Operations Commands (run from `D:\01_DCMvn\Workspace\Lightchart`)

### 1. Environment, Hook & Policy Health (`doctor`)
```powershell
python topstep-chart-app/harness/scripts/verify_app.py doctor --json
```

### 2. Sync Isolated Codex Room Runtimes (stored outside repo in `%USERPROFILE%\.codex-runtime`)
```powershell
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all --verify-only
```

### 3. Validate Task Packet (`G0.5`) & Static Gates (`arch_check.py`, `build-verifier`)
```powershell
python topstep-chart-app/harness/scripts/brief_gate.py --packet <PACKET_PATH> --repo-root topstep-chart-app --out <READINESS_JSON>
python topstep-chart-app/harness/scripts/arch_check.py --root topstep-chart-app
python topstep-chart-app/harness/scripts/build_verifier.py --root topstep-chart-app --phase 0 --strict-clean --commit <SHA> --out <VERIFIER_JSON>
```
*(Replace `--phase 1` .. `6` for the target phase.)*

### 4. Run 2-Pass Independent Lead Review (`G4`)
```powershell
# Dry-run prompt rendering & schema validation without API calls:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit HEAD --task-id P1-01 --dry-run

# Execute 2 independent read-only passes against frozen-src for the exact candidate SHA:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --task-packet <PACKET_PATH> --fail-on-blocking

# Strict Codex CLI only, or explicit canary-qualified Antigravity fallback:
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --backend codex --fail-on-blocking
python topstep-chart-app/harness/scripts/codex_room_review.py --commit <SHA> --task-id P1-01 --backend agy --fallback-model <QUALIFIED_ROUTE> --fail-on-blocking
```

### 5. Generate Write-Once Acceptance Receipt (`G5`)
```powershell
python topstep-chart-app/harness/scripts/receipt.py `
  --task-id P1-01 --phase 1 --base-sha <BASE_SHA> --candidate-sha <CANDIDATE_SHA> `
  --verifier-json <VERIFIER_JSON> `
  --review-json <CONSOLIDATED_REVIEW_JSON>
```
*(For bug fixes, add `--bugfix --red-proof-json <RED_PROOF_JSON>`.)*

---

## Starting Phase 1 with Coordinator (S-L-P)
1. Confirm `verify_app.py doctor --json` reports `ok=true`, `hooks.ok=true`, and `model_policy.ok=true`, and `codex_room_sync.py --role all --verify-only` passes.
2. Prompt Coordinator (`gemini-4-neon-medium`) to start the target task from [task.md](../task.md) using the `slp-orchestrator` skill.
3. Coordinator authors the Task Packet, validates it through `brief_gate.py` (`G0.5`), dispatches Peer via `swarm(action="add", model="gemini-4-hydrogen-high", workspace="share")`, verifies commit reachability and lands `<candidate_sha>` before `swarm(action="kill")`, runs `build_verifier.py` (`G3`) and `codex_room_review.py` (`G4`), and only marks [task.md](../task.md) complete when `receipt.py` (`G5`) exits `0` (`VERIFIED`).
