---
name: slp-orchestrator
description: >-
  Run the small-codebase S-L-P (Supervisor+Lead Gemini Neon, Peer Flash coder/debater,
  Codex Room 2-pass reviewer) workflow for topstep-chart-app tasks and bug fixes.
---

# S-L-P Orchestrator (Gemini Neon = Supervisor + Lead)

Use this skill when executing tasks from `topstep-chart-app/task.md` or fixing bugs under `topstep-chart-app/`.

## 1. Topology
- **Supervisor + Lead (You — Gemini Neon)**: Guard scope, Prop Firm Hard Read-Only (`SEC-001`), and `F01–F08` / `R01–R06`; write self-contained Task Packets; debate Peer findings as equal colleagues; run hard gates and Codex Room Review; accept or reject candidates via immutable receipts.
- **Peer (Lower Model — `flash`)**:
  - Default single-writer: `define_subagent` `peer-coder` (`EnableWriteTools: true`) using system prompt in [peer-system-prompt.md](./references/peer-system-prompt.md), then `invoke_subagent(..., Model: "flash")` and debate via `send_message`.
  - Parallel disjoint write scopes only: `swarm{action: "add", model: "gemini-3.8-flash-high", workspace: "share"}`.
- **Lead Review**: 2 independent read-only Codex CLI passes via `codex_room_review.py` on the exact candidate commit SHA.

## 2. Execution Loop (`G0` → `G5`)
1. **G0 — Intake & Task Packet**:
   - Read the target phase/task in `task.md` and `implementation_plan.md`.
   - Record `base_sha` (`git -C topstep-chart-app rev-parse HEAD`).
   - Author a self-contained Task Packet using [task-packet-template.md](./references/task-packet-template.md) and write it to `topstep-chart-app/harness/runs/<task-id>-packet.md`.
2. **G1 — Dispatch Peer (`flash`)**:
   - Launch `peer-coder` (`Model: "flash"`) with the Task Packet.
   - Wait for `send_message` replies (do not poll).
3. **Debate & Adjudicate**:
   - If Peer sends `REOPEN_REQUEST` or `DEPENDENCY_REQUEST`, inspect the cited `file:line` and plan section directly, then reply with `ACCEPT_AMENDMENT` (updating the Task Packet), `REJECT_WITH_RATIONALE` (with evidence), or `ESCALATE_TO_HUMAN`.
   - If Peer sends `BLOCKED`, verify whether missing dependency or unobservable bugfix RED; never push Peer to fake RED or bypass failing gates.
4. **G2 — Bug-Fix RED → GREEN Gate**:
   - For bug fixes, require Peer's `red_proof.json` showing observed non-zero RED failure caused by the real bug on pre-fix code before accepting the fix.
5. **G3 — Hard Gate (`build-verifier`)**:
   - On Peer's `CANDIDATE_READY` (`candidate_sha`), run:
     `python topstep-chart-app/harness/scripts/build_verifier.py --phase <N> --strict-clean --out topstep-chart-app/harness/runs/<task-id>/verifier.json`
   - If `FAILED`, send failures back to Peer via `send_message` (`REJECT_WITH_RATIONALE`) to fix and commit a new SHA.
6. **G4 — 2-Pass Codex Room Review**:
   - Follow the `codex-room-review` skill on the exact same `candidate_sha`.
   - If any blocking finding (`critical`/`high`/`medium`) is fixed in code, Peer commits a new SHA and **both** `build_verifier.py` and `codex_room_review.py` re-run on the new SHA.
7. **G5 — Immutable Receipt & Verdict**:
   - Run `python topstep-chart-app/harness/scripts/receipt.py ...`.
   - Update `task.md`, `docs/features/README.md`, and `docs/statecharts.md` ONLY when `receipt.py` exits `0` (`VERIFIED`).
