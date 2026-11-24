# AGENTS.md — `topstep-chart-app` S-L-P & Codex Room Harness

> Read-only market data & charting terminal for TopstepX Futures (`MES`, `MNQ`, `MYM`, `MGC`).
> **Prose holds judgment only.** Every deterministic invariant belongs in mechanical checks, not extra bullets here.

## 1. S-L-P Role Topology (Small-Codebase Mode)

| Role | Agent / Runtime | Authority & Responsibility |
| :--- | :--- | :--- |
| **Human** | Project Owner | Product goals, priority, cost, TopstepX credentials (`.env`), irreversible changes. |
| **Coordinator** (Supervisor + Lead) | **Gemini Neon** (`gemini-4-neon-medium` CLI default; App `Orchestrator` `Neon Medium` + `Hydrogen High`, Internal Executor forbidden from editing Peer-owned product files) | Scope & Prop Firm compliance guard, Task Packets, `G0.5` `brief_gate.py`, equal-colleague debate resolution, dispatching `Independent Reviewer` (`G4`), commit-reachability landing before `swarm(action="kill")`, and final acceptance (`test pass ≠ acceptance`). |
| **Peer** (Coder & Debater) | **`swarm(action="add", model="gemini-4-hydrogen-high", workspace="share")`** (`gemini-4-hydrogen-low` constrained to `G0.5` zero-risk pure domain packets; legacy `peer-coder` subagent and `flash` tiers are discontinued) | Implements scoped Task Packet in isolated `share` worktree, writes value-first tests, challenges flawed assumptions (`REOPEN_REQUEST`, `DEPENDENCY_REQUEST`, `BLOCKED`) via `post_message`, delivers immutable git commit (`CANDIDATE_READY`). |
| **Independent Reviewer** | **Codex CLI** (`codex-room`: `pass1` + `pass2` on `gpt-6.1-sol` `high` primary; canary-qualified `agy` fallbacks only per `harness/model-policy.json`) | Independent 2-pass **read-only** review on exact candidate commit SHA (`correctness_and_tests` + `security_and_invariants`). Always prioritizes Codex CLI when available; never edits files or spawns sub-agents. |

## 2. Rule ↔ Mechanical Enforcement Map

| Invariant / Rule | Mechanical Enforcement | Failure Behavior |
| :--- | :--- | :--- |
| Hard Read-Only Prop Firm (zero `/api/Order/*`, `/api/Position/*`, `/hubs/user`, `placeOrder`...) | `safety_hook.py` (`PreToolUse`) + `arch_check.py` (`SEC-001`) + `verify_app.py smoke` + Codex `pass2` | Tool call `deny` or CI fail (`exit 1`) |
| Zero-Secret-in-Browser (no keys/tokens in `frontend/src/` or `.html`, redacted `/api/status`) | `arch_check.py` (`SEC-002`) + `verify_app.py smoke` + Codex `pass2` | Gate fail (`exit 1`) |
| Explicit Loopback / Tailscale Bind (no blind `0.0.0.0`) — `F01` | `arch_check.py` (`SEC-003`) + Codex `pass2` | Gate fail (`exit 1`) |
| Strict Layer Boundaries (`engine` pure, `storage` isolated, `topstep` transport-only, `drawings` reusable) | `arch_check.py` (`ARCH-001`..`ARCH-004`) | Gate fail (`exit 1`) |
| CME Globex Session Calendar (`America/New_York` 18:00 reset, no naive `utcnow()`) — `R05` | `arch_check.py` (`ARCH-005`) + Codex `pass1` | Gate fail (`exit 1`) |
| Keep Upstream Fork `../lightweight-charts/` Untouched | `safety_hook.py` (`deny` in Peer worktrees, `ask` for Coordinator) + `arch_check.py` (`ARCH-006`) + `verify_app.py doctor` | Hook `deny`/prompt + gate fail (`exit 1`) |
| Model Policy & Hook Guard (`MOD-001`, `NC-08`) | `harness/model-policy.json` + `.agents/hooks.json` + `safety_hook.py` + `arch_check.py` (`MOD-001`) + `verify_app.py doctor` | Hook `deny` / gate fail (`exit 1`) |
| No Unapproved Lint/Type Suppressions or Chart Raw Timers (`SLOP-001`, `SLOP-002`) | `arch_check.py` (`SLOP-001`, `SLOP-002`, expiring `EXEMPT(...)`) | Gate fail (`exit 1`) |
| Anti-Self-Confirming Tests (`assert True`, `x == x`, swallowed `except: pass`, unapproved `skip`) | `arch_check.py` (`TEST-001`) + Codex `pass1` | Gate fail (`exit 1`) |
| No Empty Test Suites / Swallowed Exit Codes (`build-verifier`) | `build_verifier.py` (fails on `0 collected` or missing phase target) | `FAILED` (`exit 1`) / `BLOCKED` (`exit 2`) |
| 2-Pass Independent Review on Exact SHA + Bugfix RED→GREEN + Immutable Receipt | `codex_room_review.py` + `receipt.py` | `BLOCKED` (`exit 2`) or `FAILED` (`exit 1`) |

## 3. Judgment Rules (Only What Machines Cannot Check)

1. **Equal-Colleague Debate (`Peer ↔ Lead`)**: Peer must raise `REOPEN_REQUEST` (with file:line/spec evidence) whenever a Task Packet assumption conflicts with the code or `implementation_plan.md` (`F01–F08`, `R01–R06`). Lead adjudicates explicitly; never pressure Peer into rubber-stamping.
2. **Test Sensitivity & Bug-Fix Discipline**: Every bug fix requires observed **RED on pre-fix state → GREEN on candidate** (no synthetic/import-error RED). See [test-required.md](./.references/test-required.md).
3. **Evidence Over Claims**: `"VERIFIED"` requires `build_verifier.py` exit `0`, 2-pass `codex_room_review.py` on the exact same `candidate_sha`, zero unresolved blocking findings, and `receipt.py` exit `0`.
4. **Update Feature Map & Statecharts**: Any PR adding/changing UI, routes, or state machines must update [docs/features/README.md](./docs/features/README.md) and [docs/statecharts.md](./docs/statecharts.md).
5. **Every Correction Feeds `/correct`**: After fixing a recurring agent mistake, turn it into a deterministic check in `arch_check.py` / lint / test and prune redundant prose (use `correct-loop` skill).

## 4. Key Workspace References

- Protocol & Gates: [docs/WORKSPACE_PROTOCOL.md](./docs/WORKSPACE_PROTOCOL.md)
- Statecharts: [docs/statecharts.md](./docs/statecharts.md)
- Feature Map: [docs/features/README.md](./docs/features/README.md)
- Harness Operations & Runbook: [harness/README.md](./harness/README.md)
- Non-Mechanizable Judgment Lessons: [harness/lessons.md](./harness/lessons.md)
- Master Plan & Checklist: [implementation_plan.md](./implementation_plan.md) · [task.md](./task.md)

## 5. Working language

- **Discuss with me in Vietnamese.** All conversation, explanations, questions and result reports to the user are in Vietnamese.
- **Write all plan amendments, task packets, evidence records, and handoffs in English.** This includes every file in the
  repository: source comments, doc comments, scripts, commit messages, user-facing strings, SETUP.md and this file.
