---
name: codex-room-review
description: >-
  Sync isolated Codex Room runtimes and run mandatory 2-pass read-only Independent Review
  on an immutable git commit SHA for topstep-chart-app.
---

# Codex Room 2-Pass Independent Review

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

## 2. Execute 2 Independent Read-Only Passes on Exact Candidate SHA (`frozen-src`)
Verify `git -C topstep-chart-app status --porcelain` is clean, then run:
```powershell
python topstep-chart-app/harness/scripts/codex_room_review.py `
  --commit <CANDIDATE_SHA> `
  --base <BASE_SHA> `
  --task-id <TASK_ID> `
  --task-packet <PACKET_PATH> `
  --fail-on-blocking
```

### Backend Priority & Policy-Governed Fallback (`--backend auto`, default)
- **`--backend auto` (default)** always prioritizes **Codex CLI** (`gpt-6.1-sol`, `high` reasoning effort) first.
- Fallback to **Antigravity CLI (`agy`)** is governed by `harness/model-policy.json`: check `roles.independent_reviewer_fallbacks.qualified_routes` for canary-qualified fallback routes (none are qualified until `reviewer_canary.py` promotes a candidate). Excluded models (`MOD-001`) are rejected fail-closed with exit `2` (`BLOCKED`).
- When a fallback route runs, `codex_room_review.py` verifies `git status --porcelain` and `frozen-src` tree hashes are unchanged before/after, and records `review_backend`, `review_model`, `fallback_used`, and `fallback_reason` in `consolidated-review.json` and `receipt.json`.
- Use `--backend codex` to require Codex CLI strictly, or `--backend agy --fallback-model <QUALIFIED_ROUTE>` when Codex CLI quota is exhausted and a canary-qualified fallback exists.

## 3. Rules & Blocking Behavior
- **Never** treat a single pass or same-context self-review as independent review.
- **Never** dismiss a finding raised in one pass by voting against the other pass.
- **If primary Codex CLI and canary-qualified fallback routes are unavailable or unauthenticated**: report `BLOCKED` honestly; never fake independent review or use excluded/unqualified models.
- **If any code changes occur after review**: commit a new SHA and re-run **both** `build_verifier.py` and `codex_room_review.py` on the new SHA. Never carry forward a stale-SHA approval.
- To refute a false-positive blocking finding without editing code, record `{ "id": "...", "status": "REFUTED_WITH_EVIDENCE", "rationale": "...", "evidence_ref": "..." }` in `dispositions.json` and pass it to `receipt.py`.
