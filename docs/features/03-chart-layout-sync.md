# 03 — Multi-Pane Chart Workspace & 5 Sync Toggles (Phase 3)

## 1. Sub-features
- **Layout Picker (`1–8 panes`)**: All 21 canonical CSS Grid templates from `1-single` (`1x1`) up to `8-grid-2x4` (`8 panes`, `"4 Future"` default `2x2` grid for `MES/MNQ/MYM/MGC` at `5m`), slot-based hidden-pane preservation, and named layout CRUD (`Save`, `Save As`, `Rename`, `Delete`, `Set Default`) persisted in SQLite.
- **`SYNC IN LAYOUT` (5 Independent Toggles)**: `Symbol`, `Interval`, `Crosshair`, `Time`, `Date range` with synchronous `LayoutSyncController` active-source guards and `requestAnimationFrame` next-frame `setVisibleRange` echo suppression (`P1-001`, zero `setTimeout`/`setInterval`).
- **Stale Response Ownership Guard (`F06`)**: Per-pane `AbortController` + monotonic `requestGeneration` in `PaneFetchCoordinator` checking `(pane_id, contract_id, timeframe, mode, generation)` before mutating chart series, plus isolated `layoutResolveGenerationRef` / `retryGenerationRef` and cancelled-batch mode recovery in `App.tsx`.
- **OHLCV Legend & Local Engine**: Uses `../lightweight-charts/dist/` v5 Canvas isolated via React `useRef`, with in-memory CSRF (`SEC-002`) and `/ws/market` reconnect resync.

## 2. Entry Points
- `frontend/src/store/layoutStore.ts` (Zustand layout state, slot archive, CRUD actions with `sessionEpoch` + `draftRevision` + `AbortController` guards)
- `frontend/src/layout/templates.ts` (21 canonical 1–8 pane CSS Grid & SVG preview definitions)
- `frontend/src/layout/syncController.ts` (imperative `Crosshair`, `Time`, `Date range` coordinator)
- `frontend/src/layout/LayoutPicker.tsx`, `SyncControls.tsx`, `TopToolbar.tsx`, `WorkspaceGrid.tsx`
- `frontend/src/chart/ChartPane.tsx`, `frontend/src/chart/fetchGuard.ts` (Lightweight Charts v5 wrapper, rAF echo suppression, `F06` guard, `BarsQuality`)
- `frontend/src/api/client.ts`, `frontend/src/api/wsClient.ts`, `frontend/src/api/retryTimer.ts`, `frontend/src/App.tsx`
- `backend/app/api/routes_layout.py`, `backend/app/api/routes_status.py`

## 3. Driving & Verification
- `pnpm --dir frontend typecheck; pnpm --dir frontend lint; pnpm --dir frontend test; pnpm --dir frontend build` (99/99 Vitest tests across 8 files)
- `python harness/scripts/arch_check.py --root .` (`0` active violations: enforces `SEC-002`, `ARCH-006`, `SLOP-001`, and `SLOP-002` zero `setTimeout`/`setInterval` inside `frontend/src/chart/`)
- Runtime & live browser verification (`verify_app.py smoke` + Chromium at `http://127.0.0.1:5173/`): login gate, `"4 Future"` `2x2`, 1–8 pane switching (`1-single`, `2-cols`, `6-grid-3x2`, `8-grid-2x4`, `4-grid-2x2`), all 5 sync toggles, rapid `1m -> 5m -> 15m` switching (`F06`), `Save As` + `F5` reload persistence + delete cleanup, zero console errors.

## 4. Gotchas & Invariants
- Guard `Date range`, `Time`, and `Crosshair` sync loops with synchronous active-source flags AND `ChartPane` `requestAnimationFrame` next-frame suppression because Lightweight Charts v5 emits `subscribeVisibleTimeRangeChange` asynchronously on the next paint frame after `setVisibleRange`.
- Never mount chart canvas state in React `useState` per tick; keep series and bar maps imperative behind `useRef`, and keep retry timers outside `frontend/src/chart/` (`SLOP-002`).
