# 01 — Market Data, Parquet Cache, Auth & Security (Phase 1)

## 1. Sub-features
- **Caller Auth Boundary (`F01`)**: Single-user Argon2id app login, `HttpOnly; SameSite=Strict` session cookie, `X-CSRF-Token` on mutations, `AllowedHost` & `AllowedOrigin` validation, explicit `APP_BIND_HOST=127.0.0.1` or Tailscale IP.
- **Read-Only TopstepX Client & Contract Resolver (`F02, R03`)**: `/api/Auth/loginKey` with auto-refresh `<23h`, strict `symbolId`/root match (raises `ContractNotFoundError` / `AmbiguousContractError` instead of unrelated fallback), `httpx.AsyncClient` connect/read/write/pool timeouts + 25s total deadline + `asyncio.Semaphore` + non-blocking `DISCONNECTED` startup.
- **Parquet Store & CME Calendar (`F04, F05, R04, R05`)**: Partitioned by `(provider, live_mode, contract_id, timeframe)`, paginated backfill with `coverage` intervals, `is_closed` provisional-bar separation, `validate_ohlcv_bar()` quarantine to SQLite `data_quality_issues`, per-dataset `asyncio.Lock` + Windows-safe unique temp file `os.replace`, shared `session_calendar.py` (`18:00 America/New_York` reset).
- **Key Rotation & Log Redaction**: `KEY_ROTATION_DAYS=30`, `KEY_ROTATION_ENFORCE=false` (default soft reminder), redaction of `Authorization`, `access_token`, `apiKey`, `token`.

## 2. Entry Points
- `backend/app/main.py` (`FastAPI` lifespan, single worker `--workers 1`, sanitized `RequestValidationError` handler, global log redaction)
- `backend/app/security.py`, `backend/app/config.py` (`LoginGuard`, `SessionManager`, `HostOriginMiddleware`, `SecretRedactionFilter`, `validate_ws_handshake`)
- `backend/app/topstep/errors.py`, `backend/app/topstep/auth.py`, `backend/app/topstep/rest_client.py`
- `backend/app/storage/parquet_store.py`, `backend/app/storage/sqlite_store.py` *(Phase 1 Part 2)*
- `backend/app/engine/instruments.py` (`InstrumentSpec`, `StorageDatasetKey`), `backend/app/engine/session_calendar.py` *(Phase 1 Part 2)*
- `backend/app/api/routes_status.py` (`GET /api/health`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/status`, `POST /api/connect`, `POST /api/disconnect`), `backend/app/api/routes_market.py` (`GET /api/contracts`)

## 3. Driving & Verification
- Static gates: `python harness/scripts/arch_check.py` (`SEC-001`, `SEC-003`, `ARCH-001..003`, `ARCH-005`).
- Unit tests: `uv run --directory backend pytest tests/` (security boundary, pagination, OHLCV quarantine, concurrent Parquet lock, CME Globex DST boundaries).
- Runtime smoke: `python harness/scripts/verify_app.py launch` -> `smoke` -> `cleanup`.

## 4. Gotchas & Invariants
- Never bind `0.0.0.0` blindly (`F01`).
- Never mix `live=true` and `live=false` or quarterly contract expiries in one Parquet file (`F02`).
- Windows `os.replace` fails if the temp file handle is still open (`F05`).
- Single-process `--workers 1` is mandatory for in-process `asyncio.Lock` consistency (`F05`).
