# Task List: TopstepX Futures Terminal (`topstep-chart-app`)

> **Status**: Phase 1 `VERIFIED` (`1918f9156d45`, `7d724bc79a76`); Phase 2 Part 1 (`F03`, `R06`: SignalR Market Hub, Reconnect Recovery, `BarAggregator`, Local WS `/ws/market`) **`VERIFIED`** on `84abff087ab3` (213 backend tests passed, 2-pass review `PASSED`, immutable receipt `VERIFIED`). Ready for Phase 2 Part 2 (`F07` Durable Telegram Outbox).

## Phase 1 — Backend FastAPI, Parquet Cache, Security & Contract Integrity (F01, F02, F04, F05, R03, R04, R05)
- [x] Initialize `topstep-chart-app/backend/` with `uv` (`pyproject.toml`, FastAPI, Polars, PyArrow, SQLite, httpx, argon2-cffi, pytest) and remove temporary `server.mjs`
- [x] **[F01]** Implement single-user caller auth (`security.py`): `HttpOnly; SameSite=Strict` session cookie, CSRF token for mutations, `AllowedHost` & `AllowedOrigin` validation, explicit loopback/Tailscale bind, dev-only credential route restricted to loopback
- [x] **[F02]** Implement strict Read-Only TopstepX auth + contract resolver (`auth.py`, `rest_client.py`): remove unrelated-symbol fallback, key storage by `(provider, live_mode, contract_id, timeframe)`, add `instruments.py` CME tick metadata
- [x] **[R03]** Add explicit `httpx` connect/read/write/pool timeouts + total operation deadline, bounded upstream concurrency (`asyncio.Semaphore`), and non-blocking startup (`DISCONNECTED` while background auth retries)
- [x] **[F04 & R04]** Implement paginated history backfill, coverage interval tracking, `is_closed` provisional-bar separation, and strict `validate_ohlcv_bar()` gate with `data_quality_issues` quarantine
- [x] **[F05]** Implement `ParquetStore` with per-dataset `asyncio.Lock` and Windows-safe unique temp files + atomic `os.replace`
- [x] **[R05]** Create shared `session_calendar.py` defining CME Globex session timezone (`America/New_York`), 18:00 ET daily/VWAP reset, bar-open UTC timestamps, and unified bucketing for resample & live aggregation
- [x] Implement Key Rotation Tracker (`KEY_ROTATION_DAYS`, `KEY_ROTATION_ENFORCE`) + log/exception secret redaction
- [x] Write & pass Phase 1 `pytest` suite (security, contract identity, timeouts, OHLCV validation, Parquet concurrency, pagination, CME session bucketing, zero order endpoints)

## Phase 2 — Single SignalR Real-Time Hub, Reconnect Recovery & Durable Telegram Outbox (F03, F07, R06)
- [x] **[F03]** Implement single SignalR Market Hub worker (`signalr_hub.py`) with `DISCONNECTED -> RECOVERING -> LIVE` state machine, per-contract closed-bar watermarks, bounded tick queue, overlapping REST reconciliation, and status telemetry
- [x] **[R06]** Implement real-time `bar_aggregator.py` + local WebSocket `/ws/market` (caller session + Origin check, per-client bounded queue, open-bar coalescing, send timeout, slow-consumer disconnect & resync)
- [ ] **[F07]** Implement SQLite transactional `alert_outbox` with `(strategy_id, strategy_version, live_mode, contract_id, timeframe, bar_time, direction)` unique key, `expires_at` staleness TTL, bounded retry, and Telegram dispatcher (`telegram.py`) + weekend key rotation reminder
- [ ] Write & pass Phase 2 `pytest` suite (reconnect outage simulation, bar aggregation, slow-client containment, outbox crash-recovery & TTL expiry)

## Phase 3 — Frontend React+TS+Vite, Layout Setup (1–8 Panes) & Sync in Layout (F06)
- [ ] Scaffold `topstep-chart-app/frontend/` (React + TypeScript + Vite + Zustand + local `lightweight-charts` v5)
- [ ] Build Layout Picker (`1–8 panes`, `4 Future` default 2×2) + Named Layout persistence in SQLite
- [ ] Implement all 5 `SYNC IN LAYOUT` toggles (`Symbol`, `Interval`, `Crosshair`, `Time`, `Date range`) with re-entrancy guards
- [ ] **[F06]** Implement `AbortController` + `(pane_id, contract_id, timeframe, mode, generation)` response ownership guard on all pane data fetches
- [ ] Verify `tsc` typecheck, Vite build, and manual browser testing of 1–8 panes & 5 sync toggles

## Phase 4 — Interactive Drawing Primitives & Unified Command Service
- [ ] Build Unified `CommandService` (backend + SQLite) with `revision` optimistic concurrency (`409` on stale revision), `idempotency_key`, Undo/Redo stack, and WebSocket broadcast
- [ ] Implement Lightweight Charts v5 `ISeriesPrimitive` drawing tools: Cursor, Trendline, Ray, HLine, **FVG Box (with 50% CE)**, **Order Block**, **Long/Short Position Tool**, Measure, Magnet
- [ ] Persist drawings in time–price coordinates (never pixels) per `(contract_id, pane)` and verify survival across zoom/layout switch/reload

## Phase 5 — Bar Replay, Python Backtest (`ICT FVG + Sweep`) & Self-Hosted 24/7 Kit (F08, R02)
- [ ] **[R02]** Isolate backtest execution in a bounded `ProcessPoolExecutor(max_workers=1)` with max queue depth, bar/date/memory/runtime caps, `202 Accepted` + `job_id`, cancellation, worker respawn, and `BACKTEST_ENABLED` kill-switch
- [ ] **[F08]** Implement deterministic `backtester.py` & `ict_fvg_sweep.py` with strict causality (`bar[i+1]` earliest fill), zone `detected_at_time`, conservative both-hit SL/TP policy (`ambiguous_fill` flag), CME `tickValue` P&L, `markers`, `trade_lines`, `zones`, and `indicators`
- [ ] **[F08]** Implement Bar Replay UI (`Play/Pause/Step Forward/Step Back/Speed`) with checkpoint rewind, simulated Long/Short orders, live P&L, and strict `replay_cursor_time` visibility filtering
- [ ] Add `Dockerfile`, `docker-compose.yml`, `.env.example`, and 24/7 Home Host + Tailscale runbook
- [ ] Write & pass Phase 5 `pytest` suite (causality, both-hit fills, rewind accuracy, P&L arithmetic, backtest isolation/cancellation under simulated live feed)

## Phase 6 — Chart Agent Workspace & MCP Integration (Module 7, R01)
- [ ] Build `ChartContextService` exposing shared workspace/pane/contract/timeframe/visible range/replay cursor/selected objects/OHLCV/drawings with stable IDs & revisions
- [ ] Implement MCP Server (`backend/app/mcp/server.py`) with schemas: `get_chart_context`, `get_bars`, `list_chart_objects`, `get_selected_objects`, `create_chart_objects`, `update_chart_objects`, `delete_chart_objects`, `capture_chart`, `validate_indicator`, `run_indicator_preview`, `apply_indicator`, `remove_indicator`
- [ ] **[R01]** Implement Tier 1 safe **declarative Rule Spec DSL** (default for Agent indicators, zero arbitrary code execution) + Tier 2 opt-in `AGENT_CUSTOM_CODE_ENABLED=false` **OS-enforced container sandbox** (`--network=none --read-only --cap-drop=ALL`, no host secret/data mounts, fail-closed on unsupported hosts)
- [ ] Integrate Frontend Agent/Copilot bridge (inspired by `CopilotKit` / `OpenBB` widget context sharing) with shared `CommandService` Undo/Redo
- [ ] Write & pass Phase 6 tests (shared revision sync, zoom/layout coordinate fidelity, replay non-leakage, OS sandbox filesystem/network denial & fail-closed behavior, zero live-order capability)
