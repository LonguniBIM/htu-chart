# 03 — Multi-Pane Chart Workspace & 5 Sync Toggles (Phase 3)

## 1. Sub-features
- **Layout Picker (`1–8 panes`)**: Templates from `1x1` up to `8 panes` (`4 Future` default `2x2` grid for `MES/MNQ/MYM/MGC`), plus named layout CRUD persisted in SQLite.
- **`SYNC IN LAYOUT` (5 Independent Toggles)**: `Symbol`, `Interval`, `Crosshair`, `Time`, `Date range` (with re-entrancy guards on `subscribeVisibleTimeRangeChange` / crosshair propagation).
- **Stale Response Ownership Guard (`F06`)**: Per-pane `AbortController` + monotonic `requestGeneration` checking `(pane_id, contract_id, timeframe, mode, generation)` before mutating chart series.
- **OHLCV Legend & Local Engine**: Uses `../lightweight-charts/dist/` v5 Canvas isolated via React `useRef`.

## 2. Entry Points
- `frontend/src/store/` (Zustand layout, sync toggles, pane state)
- `frontend/src/layout/` (Layout Picker dropdown, grid container, Sync Controller)
- `frontend/src/chart/` (Lightweight Charts v5 wrapper, OHLCV legend, resize observer)
- `backend/app/api/routes_layout.py`

## 3. Driving & Verification
- `pnpm --dir frontend typecheck && pnpm --dir frontend build`
- `arch_check.py`: forbids `TOPSTEPX_*` secrets (`SEC-002`) and raw `setTimeout`/`setInterval` inside `frontend/src/chart/` (`SLOP-002`).
- Manual browser verification: rapid `1m -> 5m -> 15m` switching with throttled network (`F06`), all 5 sync toggles across 2x2 and 8-pane layouts, and reload persistence.

## 4. Gotchas & Invariants
- Guard `Date range` and `Crosshair` sync loops with active-source flags to prevent infinite event ping-pong.
- Never mount chart canvas state in React `useState` per tick; keep series imperative behind `useRef`.
