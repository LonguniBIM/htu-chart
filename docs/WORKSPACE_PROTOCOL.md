# Workspace Protocol — Small-Codebase S-L-P + Codex Room

> Shared operational contract for `topstep-chart-app`, synthesizing `pstack-codex-room.md`, `lauren-tan-harness.md`, and `test-required.md`.

---

## 1. Authority & Role Separation

| Actor | Model / Tool | Owns | Forbidden Actions |
| :--- | :--- | :--- | :--- |
| **Human** | User | Product intent, priorities, `.env` secrets, trade-offs, irreversible approvals. | — |
| **Supervisor + Lead** | **Gemini Neon** (Coordinator session) | Scope & Prop Firm compliance guard; Task Packets; debating & adjudicating Peer signals; running `build_verifier.py`, `codex_room_review.py`, `receipt.py`; final `ACCEPT` / `REJECT`. | Writing multi-file feature implementation inline when Peer should own it; accepting unverified claims or stale-SHA reviews; modifying `../lightweight-charts/`. |
| **Peer (Coder)** | **Lower Model (`flash`)** via `peer-coder` subagent or `swarm` worker | Technical execution of scoped Task Packet, writing value-first tests, producing RED→GREEN proof for bugfixes, challenging flawed specs, committing immutable candidate SHA. | Inventing unapproved requirements, modifying `../lightweight-charts/`, bypassing failing checks, marking `VERIFIED` without evidence, spawning sub-agents. |
| **Lead Review** | **Codex CLI** (`topstep-review-pass1`, `topstep-review-pass2`) | Strictly read-only, 2-pass cross-model inspection on exact candidate commit SHA. | Editing files, bypassing `read-only` sandbox, enabling native multi-agent (`[features].multi_agent = false`). |

---

## 2. Self-Contained Task Packet Contract

Every assignment from Lead (Neon) to Peer (`flash`) must stand on its own (Peer starts fresh with no chat history):
1. **`Task ID` & `Phase`**: Reference to `task.md` and `implementation_plan.md` findings (`F01–F08`, `R01–R06`).
2. **`Outcome` & `Contract`**: Exact observable behavior to build/fix and invariants to preserve.
3. **`Write Scope` & `Do-Not-Touch`**: Exact directory/file ownership (one writer per write scope at a time).
4. **`Base SHA`**: Current git commit SHA Peer builds from.
5. **`Bugfix? (Yes/No)`**: If `Yes`, Peer MUST record pre-fix RED output before fixing.
6. **`Required Verifier Commands`**: Exact `arch_check.py`, `pytest`, `ruff`, `pnpm`, or `verify_app.py` checks.

---

## 3. Equal-Colleague Debate & Escalation Signals

Peer is an **equal engineering colleague**, not an obedient assistant. Peer MUST stop and message Lead (`send_message`) when specs/code conflict:

### Peer Signals
- **`REOPEN_REQUEST`**: The Task Packet premise, API assumption, or audit constraint is wrong or incomplete. Must include: `evidence` (exact `file:line` or command output), `consequence`, and `proposed_change`.
- **`DEPENDENCY_REQUEST`**: Task requires an interface or file outside assigned `Write Scope`.
- **`BLOCKED`**: Missing dependency, environment failure, or unable to observe legitimate pre-fix RED on a bugfix (`BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi`).
- **`CANDIDATE_READY`**: Task scope implemented, local gates passed, changes committed cleanly to git. Must include: `base_sha`, `candidate_sha`, `changed_paths`, `commands_run` (with exit codes), `red_to_green` (if bugfix), and `remaining_risks`.

### Lead Adjudication Responses
- **`ACCEPT_AMENDMENT`**: Updates Task Packet scope/contract and logs judgment if non-obvious.
- **`REJECT_WITH_RATIONALE`**: Explains technical justification with code/plan evidence; instructs Peer to proceed as specified.
- **`ESCALATE_TO_HUMAN`**: Pauses task when decision alters user product intent, security boundary, or cost.

---

## 4. Verification & Test Quality Policy (`test-required.md`)

1. **Value-First Contract**: Before writing any test, state: *"When [scenario] occurs, if the product has [defect], this test fails at [assertion], because the contract requires [expected]."*
2. **Forbidden Patterns** (enforced by `arch_check.py TEST-001` & Codex Pass 1):
   - Comparing a value to itself (`assert True`, `assert x == x`).
   - Mocking the logic under test and asserting mock config.
   - Copying production math/formulas into the test helper to compute expected values.
   - Weak `not None` / "does not throw" assertions when concrete outputs are specified.
   - Swallowing exceptions (`except Exception: pass`) or unapproved `@pytest.mark.skip`/`xfail`.
3. **Bug-Fix RED → GREEN Gate**:
   - Reproduce failing behavior on pre-fix code with a targeted regression test -> observe non-zero exit due to the real bug (not import/syntax/fixture error) -> capture `red_proof.json` -> apply fix -> observe GREEN on candidate SHA.
   - If pre-fix RED cannot be observed -> **`BLOCKED`** (never fake RED by editing expected values or breaking setup).
4. **2-Pass Independent Lead Review (Codex Room)**:
   - Run `python harness/scripts/codex_room_review.py --commit <candidate_sha> --task-id <id> --task-packet <file>`.
   - Pass 1 (`topstep-review-pass1`) audits correctness, concurrency, statecharts, and test sensitivity.
   - Pass 2 (`topstep-review-pass2`) audits security, Prop Firm Read-Only, `F01–F08`, and `R01–R06`.
   - Consolidation forbids majority-vote dismissal: **any blocking finding (`critical`, `high`, `medium`) from either pass keeps status `OPEN`**.
   - **Code fix after review invalidates prior review approvals**: Peer commits a new SHA and Lead re-runs both passes on the new SHA.
5. **No Gate Weakening**: Never delete tests, weaken assertions, add unapproved skips, or mask exit codes to achieve `GREEN`.

---

## 5. Gate Lifecycle (`G0` → `G5`)

```text
G0 (Intake)      -> Lead writes Task Packet, confirms scope & Base SHA
G1 (Dispatch)    -> Peer (flash) receives Task Packet; may debate via REOPEN_REQUEST
G2 (RED Proof)   -> [Bugfix only] Peer proves real RED on pre-fix SHA
G3 (Hard Gate)   -> build_verifier.py runs arch_check + ruff + pytest + tsc/vite on candidate SHA
G4 (Lead Review) -> codex_room_review.py runs 2 independent passes on identical candidate SHA
G5 (Acceptance)  -> Lead adjudicates findings + generates immutable receipt.py verdict (VERIFIED/FAILED/BLOCKED)
```

Only **`VERIFIED`** from `receipt.py` allows Lead to mark a task complete in `task.md`.
