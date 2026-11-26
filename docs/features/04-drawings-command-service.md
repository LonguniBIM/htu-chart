# 04 — Interactive Drawing Primitives & Unified Command Service (Phase 4)

## 1. Sub-features
- **Lightweight Charts v5 `ISeriesPrimitive` Tools**: `Cursor` (select, anchor handle drag, whole-body drag with 5px deadzone), `Trendline`, `Ray`, `Horizontal Line` (price tag on right axis), `FVG Box` (Bullish/Bearish + 50% CE dashed line & formatted CE price label), `Order Block (OB)` (optional Extend Right + customizable label), `Long / Short Position Tool` (Entry/TP/SL right-axis tags, ticks, dollar risk/reward, R:R HUD, >= `1 * tickSize` directional clamping), `Measure Tool` (bars, duration, price delta, % change, ticks), `Magnet Mode` (nearest bar OHLC snap), `Undo / Redo`, `Delete / Clear`.
- **Unified `CommandService`**: Single mutation pipeline shared by Frontend UI and Phase 6 Agent MCP, enforcing `expected_revision` Optimistic Concurrency Control (`409 Conflict` `revision_conflict` with automatic client resync), `idempotency_key`, SQLite persistence, Undo/Redo stack (`actor: "user" | "agent"`), and `/ws/market` `drawings_update` broadcast.

## 2. Entry Points
- Backend:
  - `backend/app/api/routes_drawings.py` (`GET/POST/DELETE /api/drawings/{symbol}`, `POST /api/drawings/{symbol}/undo`, `POST /api/drawings/{symbol}/redo`)
  - `backend/app/services/command_service.py` (scope normalization, Pydantic `ConfigDict(extra="forbid")` validation, pixel-key rejection, OCC & idempotency orchestration)
  - `backend/app/storage/sqlite_store.py` (transactional SQLite persistence & Undo/Redo history)
- Frontend:
  - `frontend/src/drawings/` (`types.ts`, `geometry.ts`, `DrawingsOverlayPrimitive.ts`, `DrawingToolbar.tsx` — decoupled from `replay`/`backtest`/`agent` per `ARCH-004`, zero `setTimeout`/`setInterval` per `SLOP-002`)
  - `frontend/src/store/drawingStore.ts` (per-scope serialized mutation queue, `mutationEpoch` + `sessionEpoch`, `409` auto-resync, WS `drawings_update` monotonic merge)
  - `frontend/src/chart/ChartPane.tsx` (plot-area axis isolation, pointer placement/drag, synchronous mid-drag cancellation, `syncController.onTimeClick` suppression during drawing actions)
  - `frontend/src/App.tsx` (`.terminal-body` + `DrawingToolbar`, WS routing, global shortcuts `Escape`/`Delete`/`Backspace`/`Ctrl+Z`/`Ctrl+Y`/`Ctrl+Shift+Z`)

## 3. Driving & Verification
- `arch_check.py` enforces `ARCH-004` (`frontend/src/drawings/` cannot import `/replay`, `/backtest`, or `/agent`), `SLOP-001`, `SLOP-002`, `SEC-001`, `SEC-002`.
- Backend tests (`backend/tests/test_drawings_command_service.py`): stale `expected_revision` returns `409` `revision_conflict`, duplicate `idempotency_key` deduplicates, Undo/Redo restores exact `(time, price)` coordinates, pixel keys rejected with `422`.
- Frontend tests (`geometry.test.ts`, `DrawingsOverlayPrimitive.test.ts`, `drawingStore.test.ts`, `DrawingToolbar.test.tsx`, `ChartPane.test.tsx`, `App.test.tsx`, `wsClient.test.ts`): between-bar logical interpolation round-trip, plot-area clipping & scale isolation, drag deadzone & equality guard, synchronous mid-drag cancellation on `Escape`/`Delete`/concurrent revision change, `409` resync, and `1-single -> 4-grid-2x2 -> 1-single` layout persistence.

## 4. Gotchas & Invariants
- **Domain-Only Coordinates**: Store every coordinate strictly in **`(time, price)`** domain space (`time` integer UTC unix seconds >= `946684800`, `price > 0`), NEVER pixel coordinates. Between-bar and future-margin coordinates use `coordinateToLogical` / `logicalToCoordinate` linear interpolation so anchors survive zoom, pan, timeframe switches, and F5 reloads.
- **Plot-Area Axis Isolation**: Pointer hits/placements and canvas clipping are bounded to `[0, plotWidth) x [0, plotHeight)` via `timeScale().width()` / `timeScale().height()` so Right Price Scale and Bottom Time Scale remain unintercepted.
- **Synchronous Mid-Drag Safety**: Active drags capture `startMutationEpoch` and `originalDrawing.revision`. Any concurrent `Delete`/`Clear`/`Undo`/`Redo`, `Escape` deselection, tool switch, or WS revision change cancels the drag synchronously, restores `handleScroll`/`handleScale`, and blocks stale `updateDrawing` commits.
- **Wire Contract Discipline**: `POST /api/drawings/{symbol}` and `/undo`/`/redo` use `ConfigDict(extra="forbid")`; `client.ts` strips forbidden fields per action and sends `actor` (never `created_by`) on Undo/Redo with in-memory `X-CSRF-Token`.
