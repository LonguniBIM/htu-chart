---
name: codex-room-review
description: >-
  Sync isolated Codex Room runtimes and run mandatory 2-pass read-only Lead Review
  on an immutable git commit SHA for topstep-chart-app.
---

# Codex Room 2-Pass Lead Review

Use this skill at Gate `G4` for every candidate commit before acceptance.

## 1. Ensure Isolated Runtimes Are Synced
Run:
```powershell
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all --verify-only
```
If missing or invalid, sync once (never modifies `~/.codex` directly):
```powershell
python topstep-chart-app/harness/scripts/codex_room_sync.py --role all
```

## 2. Execute 2 Independent Read-Only Passes on Exact Candidate SHA
Verify `git -C topstep-chart-app status --porcelain` is clean, then run:
```powershell
python topstep-chart-app/harness/scripts/codex_room_review.py `
  --commit <CANDIDATE_SHA> `
  --base <BASE_SHA> `
  --task-id <TASK_ID> `
  --task-packet <PACKET_PATH> `
  --fail-on-blocking
```

## 3. Rules & Blocking Behavior
- **Never** treat a single pass or same-context self-review as independent review.
- **Never** dismiss a finding raised in one pass by voting against the other pass.
- **If Codex CLI is unavailable or unauthenticated**: report `BLOCKED` honestly; never fake independent review.
- **If any code changes occur after review**: commit a new SHA and re-run **both** `build_verifier.py` and `codex_room_review.py` on the new SHA. Never carry forward a stale-SHA approval.
- To refute a false-positive blocking finding without editing code, record `{ "id": "...", "status": "REFUTED_WITH_EVIDENCE", "rationale": "...", "evidence_ref": "..." }` in `dispositions.json` and pass it to `receipt.py`.
