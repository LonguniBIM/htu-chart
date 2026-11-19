# 05 — Bar Replay, Backtest Engine & 24/7 Self-Hosted Kit (Phase 5)

## 1. Sub-features
- **Isolated Backtest Worker (`R02`)**: `POST /api/backtest` validates date/bar caps, enqueues into `ProcessPoolExecutor(max_workers=1)` (`queue_depth <= 3`), returns `202 Accepted` + `job_id`, supports `DELETE /api/backtest/{job_id}` cancellation & worker respawn, plus `BACKTEST_ENABLED` kill-switch.
- **Deterministic Causality & `ICT FVG + Sweep` (`F08`)**: Signals at `bar[i]` close fill no earlier than `bar[i+1].open`; zones carry `detected_at_time`; conservative both-hit SL/TP (SL first + `ambiguous_fill=true`); exact CME `tickSize`/`tickValue` P&L (`MES $1.25`, `MNQ $0.50`, `MYM $0.50`, `MGC $1.00`); outputs `markers`, `trade_lines`, `zones`, `indicators`, `summary`.
- **Bar Replay UI (`F08`)**: Click-to-start, `Play/Pause`, `Step Forward (+1)`, `Step Back (-1)` via immutable action log + checkpoints, `0.5x..10x` speeds, simulated Long/Short Market/SL/TP orders + Live Replay Stats.
- **24/7 Home Host Kit**: `Dockerfile`, `docker-compose.yml`, `.env.example`, Tailscale residential deployment runbook.

## 2. Entry Points
- `backend/app/engine/backtester.py`, `backend/app/engine/strategy_base.py`, `backend/app/engine/strategies/ict_fvg_sweep.py`, `backend/app/engine/indicators.py`
- `backend/app/api/routes_backtest.py`
- `frontend/src/replay/`, `frontend/src/backtest/`

## 3. Driving & Verification
- `arch_check.py`: enforces pure `engine/` layer (`ARCH-001`) and CME calendar (`ARCH-005`).
- Pytest suite: causality (zero future look-ahead), conservative both-hit fill count, checkpoint rewind accuracy, P&L arithmetic for all 4 contracts, worker timeout/cancellation without stalling async event loop.

## 4. Gotchas & Invariants
- Never compute indicator/strategy fills using `bar[i].close` from future information (`F08`).
- Never block FastAPI event loop with synchronous Parquet backtests (`R02`).
