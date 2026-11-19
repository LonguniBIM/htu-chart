---
name: correct-loop
description: >-
  Lauren Tan's /correct workflow: turn every agent mistake or review finding into
  an architecture fix, static arch_check/lint rule with root-cause fix message,
  and proving test, then prune redundant prose from AGENTS.md.
---

# Correct Loop (`/correct`)

Use this skill whenever an agent repeats a mistake or a Codex Room review finding highlights a systemic failure mode.

## Steps (Strict Inverted Pyramid Order)
1. **Architecture / API First**: Ask if the API or module boundary can make the mistake unrepresentable (e.g., typed errors, isolated module).
2. **Static Check with Actionable Fix Message**:
   - Add or tighten a rule in `topstep-chart-app/harness/scripts/arch_check.py` (or `ruff`/`eslint` config).
   - Every rule MUST emit a concrete `fix` message explaining the root-cause correction (never vague warnings).
3. **Prove the Check Catches the Defect**:
   - Add a positive + negative fixture test in `topstep-chart-app/harness/tests/` proving the new check fails on the bad pattern and passes on the clean fix.
4. **Run Gate & Update `AGENTS.md` Table**:
   - Run `pytest topstep-chart-app/harness/tests -v` and `python topstep-chart-app/harness/scripts/arch_check.py`.
   - Add a 1-row mapping in `topstep-chart-app/AGENTS.md` §2 (`Rule ↔ Mechanical Enforcement Map`) and **delete** any prose bullet now enforced mechanically.
5. **Judgment Fallback Only**:
   - Only if the failure mode genuinely requires human/LLM judgment and cannot be checked statically or dynamically, log a concise row in `topstep-chart-app/harness/lessons.md`.
