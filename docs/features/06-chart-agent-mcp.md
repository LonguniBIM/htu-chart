# 06 — Chart Agent Workspace, MCP & Isolated Sandbox (Phase 6)

## 1. Sub-features
- **Shared `ChartContextService` (§7.1)**: Exposes active workspace/pane, `contract_id`, `root_symbol`, `timeframe`, `visible_time_range`, `replay_cursor_time`, `selected_object_ids`, OHLCV bars, and revisioned drawing objects.
- **MCP Server (`backend/app/mcp/server.py`, §7.2)**: `get_chart_context`, `get_bars`, `list_chart_objects`, `get_selected_objects`, `create_chart_objects`, `update_chart_objects`, `delete_chart_objects`, `capture_chart`, `validate_indicator`, `run_indicator_preview`, `apply_indicator`, `remove_indicator`.
- **Two-Tier Safe Indicator Execution (`R01`)**:
  - **Tier 1 (Default Everywhere)**: Declarative **Rule Spec JSON** evaluated by trusted `indicators.py` (zero arbitrary code execution).
  - **Tier 2 (Opt-in `AGENT_CUSTOM_CODE_ENABLED=false`)**: Ephemeral OS-isolated container sandbox (`--network=none --read-only --cap-drop=ALL`, `--memory=256m`, `--cpus=0.5`, `--pids-limit=32`, no `.env`/SQLite/Parquet mounts, OHLCV via stdin JSON); fails closed if sandbox runtime unavailable.
- **Frontend Agent / Copilot Bridge**: Shares `CommandService` Undo/Redo and optimistic revision concurrency.

## 2. Entry Points
- `backend/app/agent/`, `backend/app/mcp/server.py`
- `frontend/src/agent/`

## 3. Driving & Verification
- Pytest + integration checks:
  - Agent and UI observe identical `object_id` and `revision`; stale `expected_revision` fails `409`.
  - Replay mode `get_bars` / `list_chart_objects` / `run_indicator_preview` strictly hide bars & zones with `time > replay_cursor_time` or `detected_at_time > replay_cursor_time` (`F08`).
  - Arbitrary code sandbox rejects network/FS/`.env` reads and fails closed on hosts without container sandbox (`R01`).
  - Zero MCP tools expose real order placement (`SEC-001`).

## 4. Gotchas & Invariants
- Visual screenshots (`capture_chart`) are supplementary only; structured `(time, price)` JSON is always source of truth.
- Never allow Python AST filtering alone to act as a security boundary (`R01`).
