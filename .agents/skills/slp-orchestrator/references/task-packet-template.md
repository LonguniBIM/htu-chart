# Task Packet: `<TASK_ID>`

- **Phase**: `<0..6>`
- **Audit Refs**: `<F01..F08 / R01..R06 / None>`
- **Base SHA**: `<40-char git SHA>`
- **Is Bugfix**: `<Yes | No>` (if `Yes`, capture `red_proof.json` before fixing)

## 1. Outcome & Behavioral Contract
Describe the exact observable behavior, invariants, and edge cases required.

## 2. Write Scope & Do-Not-Touch
- **Owned Paths (may create/edit)**:
  - `<path/...>`
- **Do Not Touch**:
  - `../lightweight-charts/**`, `.references/**`, `.env`, and any module outside Owned Paths.

## 3. Acceptance & Test Contract (`.references/test-required.md`)
List each test requirement using:
> "When [scenario], if [defect], test fails at [assertion], because contract requires [expected]."

## 4. Required Local Checks Before `CANDIDATE_READY`
- `python topstep-chart-app/harness/scripts/arch_check.py`
- `<ruff / pytest / uv / pnpm commands>`
- Commit changes in `topstep-chart-app/` and report `CANDIDATE_READY` with `candidate_sha`, `changed_paths`, `commands_run`, and `remaining_risks`.
