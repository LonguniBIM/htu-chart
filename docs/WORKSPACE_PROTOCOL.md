# Workspace Protocol — Small-Codebase S-L-P + Codex Room

> Shared operational contract for `topstep-chart-app`, synthesizing `pstack-codex-room.md`, `lauren-tan-harness.md`, and `test-required.md`.

---

## 1. Authority & Role Separation

| Actor | Model / Tool | Owns | Forbidden Actions |
| :--- | :--- | :--- | :--- |
| **Human** | User | Product intent, priorities, `.env` secrets, trade-offs, irreversible approvals. | — |
| **Coordinator** (Supervisor + Lead) | **Gemini Neon** (`gemini-4-neon-medium` CLI; App `Orchestrator` `Gemini 4 Neon Medium` + `Gemini 4 Hydrogen High` internal executor) | Scope & Prop Firm compliance guard; self-contained Task Packets; `G0.5` `brief_gate.py`; debating & adjudicating Peer signals; landing Peer commits before `swarm(action="kill")`; running `build_verifier.py`, `codex_room_review.py`, `receipt.py`; final `ACCEPT` / `REJECT`. | App Internal Executor editing Peer-owned product files; accepting unverified claims or stale-SHA reviews; modifying `../lightweight-charts/`. |
| **Peer** (Coder & Debater) | **`swarm(action="add", model="gemini-4-hydrogen-high", workspace="share")`** (`gemini-4-hydrogen-low` constrained to `G0.5` zero-risk pure domain packets; legacy `peer-coder` subagent and `flash` tiers are forbidden) | Technical execution of scoped Task Packet in isolated `share` worktree, writing value-first tests, producing RED→GREEN proof for bugfixes, challenging flawed specs via `post_message`, committing immutable candidate SHA. | Inventing unapproved requirements, modifying `../lightweight-charts/`, `.env*`, `.agents/hooks.json`, or `harness/runs/**`, bypassing failing checks, marking `VERIFIED` without evidence, spawning sub-agents or swarm helpers. |
| **Independent Reviewer** | **Codex CLI** (`topstep-review-pass1`, `topstep-review-pass2` on `gpt-6.1-sol` `high` primary; canary-qualified `agy` routes only per `harness/model-policy.json`) | Strictly read-only, 2-pass cross-model inspection on exact candidate commit SHA (`frozen-src`). | Editing files, bypassing `read-only` sandbox, enabling native multi-agent (`[features].multi_agent = false`), running excluded models. |

---

## 2. Self-Contained Task Packet Contract & `G0.5` Brief Gate

Every assignment from Coordinator to Peer must stand on its own (Peer starts fresh with no chat history) and pass `harness/scripts/brief_gate.py` (`G0.5`) before Peer dispatch:
1. **`Root Task ID`, `Task ID`, `Packet Version` & `Phase`**: Reference to `task.md` and `implementation_plan.md` findings (`F01–F08`, `R01–R06`).
2. **`Outcome` & `Contract`**: Exact observable behavior to build/fix and invariants to preserve.
3. **`Write Scope` & `Do-Not-Touch`**: Exact disjoint `Owned Paths` and `Denied Paths` (one writer per write scope at a time).
4. **`Base SHA`**: Current git commit SHA Peer builds from.
5. **`Bugfix? (Yes/No)` & `Risk Tags`**: If `Yes`, Peer MUST record pre-fix RED output before fixing.
6. **`Budget USD` & `Required Local Checks`**: Exact `arch_check.py`, `pytest`, `ruff`, `pnpm`, `verify_app.py`, and `build_verifier.py` checks.

---

## 3. Equal-Colleague Debate & Escalation Signals

Peer is an **equal engineering colleague**, not an obedient assistant. Peer MUST stop and message Coordinator (`post_message` on the swarm board) when specs/code conflict:

### Peer Signals
- **`REOPEN_REQUEST`**: The Task Packet premise, API assumption, or audit constraint is wrong or incomplete. Must include: `evidence` (exact `file:line` or command output), `consequence`, and `proposed_change`.
- **`DEPENDENCY_REQUEST`**: Task requires an interface or file outside assigned `Write Scope`.
- **`BLOCKED`**: Missing dependency, environment failure, or unable to observe legitimate pre-fix RED on a bugfix (`BLOCKED — cannot reproduce regression on pre-fix code`).
- **`CANDIDATE_READY`**: Task scope implemented, local gates passed, changes committed cleanly to git in the Peer `share` worktree. Must include: `base_sha`, `candidate_sha`, `changed_paths`, `commands_run` (with exit codes), `red_to_green` (if bugfix), and `remaining_risks`.

### Coordinator Adjudication Responses
- **`ACCEPT_AMENDMENT`**: Updates Task Packet scope/contract (`Packet Version + 1`), re-runs `brief_gate.py`, and logs judgment if non-obvious.
- **`REJECT_WITH_RATIONALE`**: Explains technical justification with code/plan evidence; instructs Peer to proceed as specified.
- **`ESCALATE_TO_HUMAN`**: Pauses task when decision alters user product intent, security boundary, or cost.

---

## 4. `swarm` `workspace="share"` Commit Reachability Before `kill`

Because `swarm(action="kill")` permanently deletes the Peer's `share` worktree, Coordinator MUST:
1. Verify `git cat-file -t <candidate_sha>` returns `commit` in the main repository (fetching from the Peer worktree branch if needed).
2. Fast-forward or merge `<candidate_sha>` onto the target integration branch (`main`) and run `G3`/`G4`/`G5` on the immutable commit SHA.
3. Call `swarm(action="kill")` **only after** commit reachability and landing are verified.

---

## 5. Verification & Test Quality Policy (`test-required.md`)

1. **Value-First Contract**: Before writing any test, state: *"When [scenario] occurs, if the product has [defect], this test fails at [assertion], because the contract requires [expected]."*
2. **Forbidden Patterns** (enforced by `arch_check.py TEST-001` & Pass 1):
   - Comparing a value to itself (`assert True`, `assert x == x`).
   - Mocking the logic under test and asserting mock config.
   - Copying production math/formulas into the test helper to compute expected values.
   - Weak `not None` / "does not throw" assertions when concrete outputs are specified.
   - Swallowing exceptions (`except Exception: pass`) or unapproved `@pytest.mark.skip`/`xfail`.
3. **Bug-Fix RED → GREEN Gate**:
   - Reproduce failing behavior on pre-fix code (`base_sha`) with a targeted regression test -> observe non-zero exit due to the real bug (not import/syntax/fixture error) -> record exact RED evidence -> apply fix -> observe GREEN on `candidate_sha`.
   - If pre-fix RED cannot be observed -> **`BLOCKED`** (never fake RED by editing expected values or breaking setup).
4. **2-Pass Independent Review (`codex_room_review.py`)**:
   - Run `python harness/scripts/codex_room_review.py --commit <candidate_sha> --task-id <id> --task-packet <file> --fail-on-blocking`.
   - Pass 1 (`topstep-review-pass1`) audits correctness, concurrency, statecharts, and test sensitivity on `frozen-src`.
   - Pass 2 (`topstep-review-pass2`) audits security, Prop Firm Read-Only, `F01–F08`, and `R01–R06` on `frozen-src`.
   - Consolidation forbids majority-vote dismissal: **any blocking finding (`critical`, `high`, `medium`) from either pass keeps status `OPEN`**.
   - **Code fix after review invalidates prior review approvals**: Peer commits a new SHA and Coordinator re-runs both `build_verifier.py` and `codex_room_review.py` on the new SHA.
5. **No Gate Weakening & Model Governance (`MOD-001`)**: Never delete tests, weaken assertions, add unapproved skips, mask exit codes, or use models excluded by `harness/model-policy.json`.

---

## 6. Gate Lifecycle (`G0` → `G5`)

```text
G0   (Intake)      -> Coordinator writes Task Packet, confirms scope & Base SHA
G0.5 (Brief Gate)  -> brief_gate.py validates Packet, model eligibility & write-once readiness.json
G1   (Dispatch)    -> Peer (swarm workspace="share", gemini-4-hydrogen-high) executes; may debate
G2   (RED Proof)   -> [Bugfix only] Peer proves real RED on pre-fix base_sha before fixing
G3   (Hard Gate)   -> build_verifier.py --commit <sha> runs gates inside frozen-src snapshot
G4   (Review)      -> codex_room_review.py runs 2 independent passes on identical frozen-src SHA (capped by ledger.json + attempts/: max_g4_rounds_per_root_task=6, max_g4_rounds_per_packet_version=3, max_g4_invocations_per_candidate_sha=2)
G5   (Acceptance)  -> Coordinator adjudicates findings + generates write-once receipt.py (VERIFIED/FAILED/BLOCKED; verifies ledger.json + attempts/ ceilings and transitions ledger to VERIFIED)
```

`G4 ↔ ledger.json ↔ G5` Hard Round-Cap Invariants (`R03`):
- `harness/model-policy.json` (`circuit_breakers`) and `task_runner.py` enforce `max_g4_rounds_per_root_task = 6` (`PAUSED_BUDGET`), `max_g4_rounds_per_packet_version = 3` (`NEEDS_REPLAN`, reset on `record_replan` while preserving `g4_rounds_total`), and `max_g4_invocations_per_candidate_sha = 2`.
- `codex_room_review.py` enforces OS-locked canonical `harness/runs/<task_id>/g4-tracker.json` + `attempts/` caps and `ledger.json` caps with atomic in-flight slot reservations (`in_flight` / `active_g4_reservations`) **before** creating `out_dir`, `.review-attempt.lock`, or `frozen/`, or spawning any CLI subprocess (preventing custom `--out-dir` and concurrency bypasses). Post-run, it bills all non-reused `pass{1,2}*.raw.jsonl` logs (including archived transient-retry and failed-provider logs), transitions `ledger.json`, and exits `2` if `PAUSED_BUDGET` triggers.
- `receipt.py` (`G5`) evaluates `g4-tracker.json`, `attempts/`, and `ledger.json` atomically under `_ledger_lock` before writing `receipt.json`, blocking (`exit 2`, `conclusion="BLOCKED"`) whenever `ledger.json` is halted (`PAUSED_BUDGET`, `BLOCKED*`), skipped `G4`, or `g4_rounds_total` / `g4-tracker.json` / `attempts/` exceeds policy caps.

Only **`VERIFIED`** from `receipt.py` allows Coordinator to mark a task complete in `task.md`.
