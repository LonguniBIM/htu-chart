# Task Packet: `<TASK_ID>`

- **Root Task ID**: `<ROOT_TASK_ID>`
- **Task ID**: `<TASK_ID>`
- **Packet Version**: `1`
- **Phase**: `<0..6>`
- **Audit Refs**: `<F01..F08 / R01..R06 / None>`
- **Base SHA**: `<40-char lowercase git SHA>`
- **Is Bugfix**: `<Yes | No>`
- **Risk Tags**: `<None | persistence, concurrency, websocket, security, auth, session_calendar>`
- **Budget USD**: `<positive float, e.g. 6.00>`

## 1. Outcome & Behavioral Contract

<DESCRIBE_EXACT_BEHAVIOR_AND_EDGE_CASES>

<!-- Required when Is Bugfix is Yes (remove if No): -->
### Pre-Fix RED Reproduction (Required if `Is Bugfix: Yes`)
<DESCRIBE_PRE_FIX_RED_TEST_AND_COMMAND>

<!-- Required when Risk Tags or Owned Paths touch persistence/concurrency/websocket (remove if unused): -->
### Atomicity / Rollback / Concurrency Contract (Required if `persistence` or `concurrency` / `websocket`)
<DESCRIBE_ATOMICITY_ROLLBACK_CONCURRENCY_INVARIANTS>

<!-- Required when Risk Tags or Owned Paths touch security/auth (remove if unused): -->
### Security / Read-Only Invariants (Required if `security` or `auth`)
<DESCRIBE_SECURITY_AND_READONLY_INVARIANTS>

<!-- Required when Risk Tags or Owned Paths touch session_calendar (remove if unused): -->
### Session Calendar Contract (Required if `session_calendar`)
<DESCRIBE_SESSION_CALENDAR_INVARIANTS>

## 2. Write Scope & Do-Not-Touch

- **Owned Paths (may create/edit)**:
  - `<path/to/new_file.py>` (new)
  - `<path/to/existing_file.py>` (modify)
- **Denied Paths**:
  <!-- Remove or narrow backend/** or frontend/** when owned; never remove ../lightweight-charts/**, .references/**, or .env*. -->
  - `backend/**`
  - `frontend/**`
  - `../lightweight-charts/**`
  - `.references/**`
  - `.env*`

## 3. Acceptance & Test Contract (`.references/test-required.md`)

List each test requirement using the value-first sensitivity statement:
- "When <scenario>, if <specific defect>, test fails at <assertion>, because contract requires <expected>."

## 4. Required Local Checks Before `CANDIDATE_READY`

- `python harness/scripts/arch_check.py --root .`
- `uv run --directory backend ruff check ...`
- `PYTHONPATH=. uv run --directory backend pytest ... -q`
- `python harness/scripts/build_verifier.py --root . --phase <0..6> --strict-clean --commit HEAD`
- Commit changes cleanly inside `topstep-chart-app/` and report `CANDIDATE_READY` with `base_sha`, `candidate_sha`, `changed_paths`, `commands_run`, `red_to_green` (if bugfix), and `remaining_risks`.
