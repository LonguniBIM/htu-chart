# Feature Map — `topstep-chart-app`

> User-centric sitemap linking features, entry points, verification paths, and `F01–F08` / `R01–R06` invariants. Update this map in the same commit as any UI, route, or contract change.

| # | Feature Area | Phase | Key Audit Findings | Spec File |
| :--- | :--- | :--- | :--- | :--- |
| 01 | Market Data, Parquet Cache, Auth & Security | Phase 1 | `F01, F02, F04, F05, R03, R04, R05` | [01-market-data-security.md](./01-market-data-security.md) |
| 02 | Live SignalR Feed, WS Broadcast & Telegram Outbox | Phase 2 | `F01, F03, F07, R06` (`NC-12`, `NC-13`) | [02-live-signalr-telegram.md](./02-live-signalr-telegram.md) |
| 03 | Multi-Pane Chart Workspace & 5 Sync Toggles | Phase 3 (`VERIFIED`) | `F01, F04, F06, SEC-001, SEC-002, SLOP-002` | [03-chart-layout-sync.md](./03-chart-layout-sync.md) |
| 04 | Interactive Drawing Primitives & Command Service | Phase 4 | `F01, F06, R06, SEC-001, SEC-002, ARCH-004, SLOP-002`, unified `revision` OCC + Undo/Redo | [04-drawings-command-service.md](./04-drawings-command-service.md) |
| 05 | Bar Replay, Backtest Engine (`ICT FVG+Sweep`) & 24/7 Kit | Phase 5 | `F08, R02` | [05-replay-backtest-ops.md](./05-replay-backtest-ops.md) |
| 06 | Chart Agent Workspace, MCP & Isolated Indicator Sandbox | Phase 6 | `Module 7, R01` | [06-chart-agent-mcp.md](./06-chart-agent-mcp.md) |

## Cross-Cutting Invariants
- **Hard Read-Only Prop Firm**: Zero `/api/Order/*`, `/api/Position/*`, `/hubs/user`.
- **Upstream Fork Cleanliness**: `../lightweight-charts/` stays untouched; only consume `../lightweight-charts/dist/lightweight-charts.standalone.production.js`.
