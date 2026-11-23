# 02 — Live SignalR Feed, WS Broadcast & Telegram Outbox (Phase 2)

## 1. Sub-features
- **Single SignalR Market Hub (`F03`)**: Connects to `wss://rtc.topstepx.com/hubs/market` using state machine `DISCONNECTED -> RECOVERING -> LIVE` (and `DEGRADED`). Subscribes `MES`, `MNQ`, `MYM`, `MGC` active contracts.
- **Reconnect Reconciliation (`F03`)**: Persistent `last_closed_bar_time` watermark per `(live_mode, contract_id, tf)`, bounded tick queue during `RECOVERING`, overlapping REST backfill `[watermark - 2 bars, now]`, deterministic indicator rebuild, tags recovery signals `RECOVERED_HISTORICAL`.
- **Local WebSocket `/ws/market` (`R06`)**: Validates caller session cookie + `Origin` before `accept()`; per-client sender task + `asyncio.Queue(maxsize=64)`, open-bar coalescing, 5s send timeout, clean disconnect on slow mobile Tailscale clients.
- **Durable Alert Outbox & Telegram (`F07`)**: SQLite `alert_outbox` with `UNIQUE(strategy_id, strategy_version, live_mode, contract_id, timeframe, bar_time, direction)`, `expires_at` TTL (`ALERT_TTL_SECONDS=90`), bounded retry, weekend key rotation reminder.

## 2. Entry Points
- `backend/app/topstep/signalr_hub.py`
- `backend/app/services/live_feed_service.py`, `backend/app/services/ws_broadcaster.py`
- `backend/app/engine/bar_aggregator.py`, `backend/app/engine/alert_engine.py`
- `backend/app/notify/telegram.py`
- `backend/app/api/routes_market.py` (`WS /ws/market`), `backend/app/api/routes_alerts.py`, `backend/app/api/routes_status.py`

## 3. Driving & Verification
- Automated tests simulating: SignalR drop during open candles + REST backfill reconciliation; slow WS client blocking without stalling healthy panes; process restart with `PENDING` outbox alerts before vs after `expires_at`.
- Telemetry fields in `/api/status`: `feed_state`, `last_event_age_sec`, `recovery_duration_ms`, `unresolved_gaps`, `queue_overflow_count`.

## 4. Gotchas & Invariants
- Never emit live Telegram entry alerts while in `RECOVERING` or `DEGRADED` (`F03`).
- Never retry expired entry alerts after `now >= expires_at` (`F07`).
- Never await slow Tailscale WebSocket writes inside the SignalR ingestion loop (`R06`).
