# 24/7 Self-Hosted Home Host + Tailscale Setup Guide

`topstep-chart-app` is a **100% read-only** futures charting, Bar Replay, Python Backtest, and Telegram alerting terminal for **MES, MNQ, MYM, and MGC**.

Running the terminal on your own 24/7 home host (PC, Mini PC, or Raspberry Pi) over your residential IP and accessing it remotely through **Tailscale VPN** ensures:

- **CME Non-Professional & Prop Firm Compliance**: All TopstepX connections originate from your single residential IP with a single user, zero order-routing endpoints, and zero shared/cloud VPS IP addresses.
- **Zero Open Router Ports**: Never forward ports on your home router and never bind the server to a wildcard interface.

---

## 1. Generate Caller Authentication Credentials

The web terminal is protected by a single-user Argon2id password gate (`HttpOnly` + `SameSite=Strict` session cookies, `X-CSRF-Token`, and `Host`/`Origin` allowlists).

Generate an Argon2id hash locally using `backend/.venv`:

```bash
cd topstep-chart-app/backend
uv run python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('replace-with-strong-passphrase'))"
```

Generate a 32+ byte random session secret:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 2. Configure `.env` (Loopback + Tailscale Bind)

Copy `.env.example` to `.env` (never commit `.env`):

```bash
cp .env.example .env
```

Find your home host's Tailscale IPv4 address (for example `100.64.12.34`):

```bash
tailscale ip -4
```

### Option A: Tailscale Serve HTTPS (Recommended for Mobile/Remote Access)

Keep the backend bound strictly to loopback (`127.0.0.1`) and let Tailscale Serve terminate TLS for your tailnet:

```dotenv
APP_ENV=production
APP_BIND_HOST=127.0.0.1
APP_PORT=8000
APP_COOKIE_SECURE=true
APP_ALLOWED_HOSTS=127.0.0.1,localhost,my-mini-pc.tailnet-name.ts.net
APP_ALLOWED_ORIGINS=https://my-mini-pc.tailnet-name.ts.net
APP_PASSWORD_HASH=<argon2id-hash>
APP_SESSION_SECRET=<random-secret>
```

Enable Tailscale Serve in the background:

```bash
tailscale serve --bg http://127.0.0.1:8000
```

### Option B: Direct Tailscale Interface Bind (HTTP inside WireGuard Tunnel)

If you prefer connecting directly to `http://100.x.y.z:8000` inside your private Tailscale tunnel without Tailscale Serve:

```dotenv
APP_ENV=production
APP_BIND_HOST=100.64.12.34
APP_PORT=8000
APP_COOKIE_SECURE=false
APP_ALLOWED_HOSTS=100.64.12.34,127.0.0.1,localhost
APP_ALLOWED_ORIGINS=http://100.64.12.34:8000,http://127.0.0.1:8000
APP_PASSWORD_HASH=<argon2id-hash>
APP_SESSION_SECRET=<random-secret>
```

> **Security Rule (`SEC-003`)**: Never bind to an unspecified wildcard address. Always bind explicitly to `127.0.0.1` or your Tailscale `100.x.y.z` IP.
>
> **Docker `HEALTHCHECK` & `HostOriginMiddleware` Note**: Always include your `APP_BIND_HOST` literal (e.g., `100.64.12.34`, `127.0.0.1`, or `[::1]`) in `APP_ALLOWED_HOSTS` alongside any MagicDNS hostname (`my-mini-pc.tailnet-name.ts.net`), because the container `HEALTHCHECK` probes `http://${APP_BIND_HOST}:${APP_PORT}/api/health`.

---

## 3. Running 24/7 with Docker Compose

Build and start from `topstep-chart-app/` (the Compose build context automatically uses the parent workspace `..` so `lightweight-charts/` and `topstep-chart-app/` are both included, while `Dockerfile.dockerignore` keeps host `.venv`, `node_modules`, `.env*`, and `data/` out of build layers):

```bash
cd topstep-chart-app
docker compose up -d --build
```

`docker-compose.yml` mounts the persistent named volume `topstep_data:/app/data` so Parquet/SQLite storage automatically inherits container user `appuser` (`10001:10001`) ownership without host permission errors.

Inspect or back up `topstep_data`:

```bash
docker volume inspect topstep-chart-app_topstep_data
docker run --rm -v topstep-chart-app_topstep_data:/data -v "$PWD":/backup alpine tar czf /backup/topstep-data-backup.tgz -C /data .
```

If you prefer a host bind mount (`./data:/app/data`) on Linux, pre-create and chown it to UID/GID `10001:10001` before starting Compose:

```bash
mkdir -p ./data
sudo chown -R 10001:10001 ./data
```

Check health and logs:

```bash
docker compose ps
docker compose logs -f topstep-chart-app
```

---

## 4. Running Natively (Single-Worker `--workers 1`)

The backend maintains a single SignalR Market Hub connection, single-writer Parquet/SQLite stores, and a bounded `ProcessPoolExecutor(max_workers=1)` backtest worker. **Always run with `--workers 1`** (enforced by `python -m app.main`).

### Build Frontend Bundle

```bash
cd topstep-chart-app/frontend
pnpm install --frozen-lockfile
pnpm build
```

### Linux `systemd` Service Example (`/etc/systemd/system/topstep-chart.service`)

```ini
[Unit]
Description=TopstepX Read-Only Futures Terminal
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/workspace/topstep-chart-app/backend
EnvironmentFile=/opt/workspace/topstep-chart-app/.env
ExecStart=/opt/workspace/topstep-chart-app/backend/.venv/bin/python -m uvicorn app.main:app --host ${APP_BIND_HOST} --port ${APP_PORT} --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Windows Native Service (NSSM / Task Scheduler)

1. Point the working directory to `topstep-chart-app\backend`.
2. Run `.venv\Scripts\python.exe -m app.main` (which starts `uvicorn` with `workers=1` bound to `APP_BIND_HOST` and `APP_PORT`).
3. Ensure `.env` lives at `topstep-chart-app\.env`.

---

## 5. Operations, Key Rotation & Kill-Switch

- **TopstepX API Key Rotation**: Set `TOPSTEPX_KEY_UPDATED_AT=YYYY-MM-DD`, `KEY_ROTATION_DAYS=30`, and `KEY_ROTATION_ENFORCE=false` (or `true` to hard-block stale keys). The terminal warns automatically when rotation is due.
- **Backtest Kill-Switch (`R02`)**: Set `BACKTEST_ENABLED=false` in `.env` and restart to reject new `POST /api/backtest` submissions with HTTP `503` while keeping live charting and alerts running.
- **Telegram Weekend Test**: Configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and use `/api/alerts` rules for live ICT FVG / Sweep notifications.
