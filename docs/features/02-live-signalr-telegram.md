# 02 — Live SignalR Feed, WS Broadcast & Telegram Outbox (Phase 2)

## 1. Sub-features
- **Single SignalR Market Hub (`F03`)**: Connects to `wss://rtc.topstepx.com/hubs/market` using state machine `DISCONNECTED -> RECOVERING -> LIVE` (and `DEGRADED`). Subscribes `MES`, `MNQ`, `MYM`, `MGC` active contracts.
- **Reconnect Reconciliation (`F03`)**: Persistent `last_closed_bar_time` watermark per `(live_mode, contract_id, tf)`, bounded tick queue during `RECOVERING`, overlapping REST backfill `[watermark - 2 bars, now]`, deterministic indicator rebuild, tags recovery signals `RECOVERED_HISTORICAL`.
- **Local WebSocket `/ws/market` (`R06`)**: Validates caller session cookie + `Origin` before `accept()`; per-client sender task + `asyncio.Queue(maxsize=64)`, open-bar coalescing, 5s send timeout, clean disconnect on slow mobile Tailscale clients.
- **Durable Alert Outbox & Telegram (`F07`)**: SQLite `alert_outbox` with `UNIQUE(strategy_id, strategy_version, live_mode, contract_id, timeframe, bar_time, direction)`, `expires_at` TTL (`ALERT_TTL_SECONDS=90`), bounded retry, weekend key rotation reminder.

## 2. Entry Points
- `backend/app/topstep/signalr_hub.py`
- `backend/app/services/live_feed_service.py`, `backend/app/services/ws_broadcaster.py`, `backend/app/services/alert_service.py`
- `backend/app/engine/bar_aggregator.py`, `backend/app/engine/alert_engine.py`
- `backend/app/storage/sqlite_store.py` (`feed_watermarks`, `alert_outbox`)
- `backend/app/notify/telegram.py`
- `backend/app/api/routes_market.py` (`WS /ws/market`), `backend/app/api/routes_alerts.py` (`GET /api/alerts/status`, `GET /api/alerts/history`, `POST /api/alerts/scanner`, `POST /api/alerts/test`), `backend/app/api/routes_status.py` (`GET /api/status` including `alerts` snapshot)

## 3. Driving & Verification
- Automated tests (`backend/tests/test_bar_aggregator.py`, `test_signalr_recovery_f03.py`, `test_ws_broadcaster_r06.py`, `test_alert_outbox_f07.py`, `test_security_f01.py`) simulating: SignalR drop during open candles + REST backfill reconciliation; slow WS client blocking without stalling healthy panes; process restart with `PENDING` outbox alerts before vs after `expires_at`; bounded transient retry & permanent 4xx `FAILED`; `F03` `RECOVERED_HISTORICAL`/`SUPPRESSED` suppression; secret redaction on `/bot<token>` and `chat_id`; CME weekend-only key rotation reminder.
- Telemetry fields in `/api/status`: `feed_state`, `last_event_age_sec`, `recovery_duration_ms`, `unresolved_gaps`, `queue_overflow_count`, and redacted `alerts` (`telegram_configured`, `scanner_enabled`, `alert_ttl_seconds`, `outbox` counts, `suppressed_signals`, `recovered_historical_skipped`, `last_error`).
- Verified commits: Phase 2 Part 1 (`84abff087ab3`) & Phase 2 Part 2 (`bb81eb9add7d`).

## 4. Gotchas & Invariants
- Never emit live Telegram entry alerts while in `RECOVERING` or `DEGRADED` (`F03`).
- Never retry expired entry alerts after `now >= expires_at` (`F07`).
- Never await slow Tailscale WebSocket writes inside the SignalR ingestion loop (`R06`).
