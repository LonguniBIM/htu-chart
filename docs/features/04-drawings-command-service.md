# 04 — Interactive Drawing Primitives & Unified Command Service (Phase 4)

## 1. Sub-features
- **Lightweight Charts v5 `ISeriesPrimitive` Tools**: `Cursor` (select/drag anchors), `Trendline`, `Ray`, `Horizontal Line` (price label), `FVG Box` (Bullish/Bearish + 50% CE line), `Order Block (OB)` (optional Extend Right), `Long / Short Position Tool` (Entry/SL/TP ticks, dollar risk, R:R), `Measure Tool` (bars, duration, price & ticks), `Magnet Mode` (OHLC snap), `Delete / Clear`.
- **Unified `CommandService`**: Single mutation pipeline shared by Frontend UI and Phase 6 Agent MCP, enforcing `expected_revision` Optimistic Concurrency Control (`409 Conflict` on stale revision), `idempotency_key`, SQLite persistence, Undo/Redo stack, and WebSocket broadcast.

## 2. Entry Points
- `frontend/src/drawings/` (primitives decoupled from replay/backtest/agent per `ARCH-004`)
- `backend/app/api/routes_layout.py` & drawing command routes (`GET/POST/DELETE /api/drawings/{symbol}`)
- `backend/app/storage/sqlite_store.py`

## 3. Driving & Verification
- `arch_check.py` enforces `ARCH-004` (`drawings/` cannot import `replay`, `backtest`, or `agent`) and `SLOP-002`.
- Automated tests: stale `expected_revision` returns `409 Conflict`; duplicate `idempotency_key` is idempotent; Undo/Redo restores exact `(time, price)` coordinates.
- Browser check: draw FVG Box & Long/Short tool, zoom/pan/switch layout `1 -> 4 -> 1` and F5 reload -> coordinates remain exact.

## 4. Gotchas & Invariants
- Store every coordinate strictly in **`(time, price)`** domain space, NEVER pixel coordinates.
- Attach `revision` and `created_by` (`"user" | "agent"`) to every persisted object.
