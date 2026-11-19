# Phase 3 Ready TypeScript & ESLint Gate Profile (`topstep-chart-app/frontend/`)

Copy these constraints into `frontend/tsconfig.json` and `frontend/eslint.config.js` when scaffolding Phase 3:

## 1. `tsconfig.json` (`compilerOptions`)
- `"strict": true`
- `"noUncheckedIndexedAccess": true`
- `"exactOptionalPropertyTypes": true`
- `"noImplicitOverride": true`
- `"noFallthroughCasesInSwitch": true`
- `"noUnusedLocals": true`
- `"noUnusedParameters": true`

## 2. ESLint Enforcement Rules (`eslint.config.js`)
- Ban `@ts-ignore` / `@ts-expect-error` / `eslint-disable` without `EXEMPT(...)` (also checked by `arch_check.py SLOP-001`).
- Ban `no-restricted-imports` so `src/drawings/**` cannot import `../replay`, `../backtest`, or `../agent` (`ARCH-004`).
- Ban `no-restricted-syntax` for `fetch(...)` calls inside `src/chart/**` or `src/store/**` missing an `AbortSignal` (`F06` stale response protection).
- Ban `localStorage` access for credentials/tokens (`SEC-002`).
- Ban raw `setTimeout` / `setInterval` inside `src/chart/` and `src/drawings/` (`SLOP-002`).

## 3. Required `package.json` Scripts
```json
{
  "scripts": {
    "typecheck": "tsc --noEmit",
    "lint": "eslint . --max-warnings 0",
    "build": "vite build"
  }
}
```
`build_verifier.py` automatically detects and executes `typecheck`, `build`, `lint`, and `test`.
