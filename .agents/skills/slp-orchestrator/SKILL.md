---
name: slp-orchestrator
description: >-
  Run the small-codebase S-L-P (Coordinator Gemini Neon, Peer Gemini 4 Hydrogen swarm coder/debater,
  Independent Reviewer 2-pass Codex Room) workflow for topstep-chart-app tasks and bug fixes.
---

# S-L-P Orchestrator (Coordinator = Gemini Neon)

Use this skill when executing tasks from `topstep-chart-app/task.md` or fixing bugs under `topstep-chart-app/`.

## 1. Topology
- **Coordinator (`gemini-4-neon-medium` CLI; App `Orchestrator` `Gemini 4 Neon Medium` + `Gemini 4 Hydrogen High` internal executor)**: Guard scope, Prop Firm Hard Read-Only (`SEC-001`), and `F01–F08` / `R01–R06`; author self-contained Task Packets; enforce `G0.5` (`brief_gate.py`); debate Peer findings as equal colleagues; verify commit reachability before `swarm(action="kill")`; run `build_verifier.py` and Independent Review; issue write-once receipts. App Internal Executor is strictly forbidden from editing Peer-owned product files.
- **Peer (`swarm` worker in `workspace="share"`)**:
  - Default Peer dispatch: `swarm(action="add", name="<role>", model="gemini-4-hydrogen-high", workspace="share", brief="...")`, communicating via `post_message` / swarm board.
  - Constrained low-risk Peer (`gemini-4-hydrogen-low`): allowed only when `brief_gate.py` (`G0.5`) classifies the packet as `zero-risk pure domain` (`Is Bugfix: No`, `Risk Tags: None`, pure domain paths only).
  - Legacy `peer-coder` subagent and `invoke_subagent(..., Model="flash")` / `flash_lite` tiers are discontinued and forbidden (`MOD-001`).
- **Independent Reviewer (`codex-room`)**: 2 independent read-only passes (`topstep-review-pass1`, `topstep-review-pass2`) via `codex_room_review.py` on `gpt-6.1-sol` `high` primary (or canary-qualified `agy` fallback per `harness/model-policy.json`) against the materialized `frozen-src` snapshot of the exact candidate SHA.

## 2. Execution Loop (`G0` → `G5`)
1. **G0 — Intake & Task Packet**:
   - Read the target phase/task in `task.md` and `implementation_plan.md`.
   - Record `base_sha` (`git -C topstep-chart-app rev-parse HEAD`).
   - Author a self-contained Task Packet using [task-packet-template.md](./references/task-packet-template.md) and write it to `topstep-chart-app/harness/runs/<task-id>/packet.md`.
2. **G0.5 — Brief Readiness Gate (`brief_gate.py`)**:
   - Run `python topstep-chart-app/harness/scripts/brief_gate.py --packet <packet> --repo-root topstep-chart-app --out <readiness.json>`.
   - Halt (`BLOCKED_BRIEF`) if exit code != `0` or if Peer model fails policy eligibility.
3. **G1 — Dispatch Peer (`swarm`, `workspace="share"`)**:
   - Spawn Peer via `swarm(action="add", model="gemini-4-hydrogen-high", workspace="share")` with [peer-system-prompt.md](./references/peer-system-prompt.md) contract and Task Packet path.
   - Wait via `swarm_wait` for board messages (never poll).
4. **Debate & Adjudicate**:
   - If Peer posts `REOPEN_REQUEST` or `DEPENDENCY_REQUEST`, inspect the cited `file:line` and plan section directly, then reply with `ACCEPT_AMENDMENT` (incrementing `Packet Version` and re-running `G0.5`), `REJECT_WITH_RATIONALE`, or `ESCALATE_TO_HUMAN`.
   - If Peer posts `BLOCKED`, verify whether missing dependency or unobservable bugfix RED; never push Peer to fake RED or bypass failing gates.
5. **G2 — Bug-Fix RED → GREEN Gate**:
   - For bug fixes, require Peer's `red_to_green` evidence showing observed non-zero RED failure caused by the real bug on `base_sha` before accepting the fix.
6. **Commit Reachability & Landing (Before `swarm(action="kill")`)**:
   - Verify `git cat-file -t <candidate_sha>` returns `commit` in the main repository, fast-forward/merge `<candidate_sha>` into the integration branch, and ONLY then call `swarm(action="kill")`.
7. **G3 — Hard Gate (`build-verifier`, `frozen-src`)**:
   - On `<candidate_sha>`, run:
     `python topstep-chart-app/harness/scripts/build_verifier.py --root topstep-chart-app --phase <N> --strict-clean --commit <candidate_sha> --out topstep-chart-app/harness/runs/<task-id>/verifier.json`
   - If `FAILED`, send failures back to Peer (`REJECT_WITH_RATIONALE`) to fix and commit a new SHA.
8. **G4 — 2-Pass Independent Review (`codex_room_review.py`)**:
   - Follow the `codex-room-review` skill on the exact same `<candidate_sha>`.
   - If any blocking finding (`critical`/`high`/`medium`) is fixed in code, Peer commits a new SHA and **both** `build_verifier.py` and `codex_room_review.py` re-run on the new SHA.
9. **G5 — Immutable Write-Once Receipt & Verdict**:
   - Run `python topstep-chart-app/harness/scripts/receipt.py ...`.
   - Update `task.md`, `docs/features/README.md`, and `docs/statecharts.md` ONLY when `receipt.py` exits `0` (`VERIFIED`).
