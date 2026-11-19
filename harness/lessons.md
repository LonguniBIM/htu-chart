# Judgment Lessons & `/correct` Ledger

> Only record **judgment rules that cannot yet be enforced mechanically**.
> Whenever a recurring mistake *can* be caught by code/API design, `arch_check.py`, `ruff`, `eslint`, or `pytest`, promote it to a deterministic check via the `correct-loop` skill and remove redundant prose.

| Date | Failure / Correction | Promoted Check (or Why Judgment-Only) | Owner |
| :--- | :--- | :--- | :--- |
| 2026-11-20 | Pre-existing LF/CRLF stat noise in sibling `../lightweight-charts/pnpm-lock.yaml` tripped naive `git status --porcelain` check while content diff was empty. | Promoted into `arch_check.py` (`ARCH-006`) & `verify_app.py doctor` using `diff --name-only` + `diff --cached --name-only` + `ls-files --others --exclude-standard` so real edits fail cleanly without touching upstream fork. | Lead (Neon) |
