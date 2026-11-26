# Build context MUST be the parent workspace directory containing both
# `lightweight-charts/` and `topstep-chart-app/` (see docker-compose.yml).
# Example:
#   docker build -f topstep-chart-app/Dockerfile -t topstep-chart-app ..

FROM node:22-slim AS frontend-builder

RUN corepack enable

WORKDIR /build

COPY lightweight-charts/package.json /build/lightweight-charts/package.json
COPY lightweight-charts/dist/ /build/lightweight-charts/dist/
COPY topstep-chart-app/frontend/package.json topstep-chart-app/frontend/pnpm-lock.yaml /build/topstep-chart-app/frontend/

RUN pnpm --dir /build/topstep-chart-app/frontend install --frozen-lockfile

COPY topstep-chart-app/frontend/index.html topstep-chart-app/frontend/tsconfig.json topstep-chart-app/frontend/vite.config.ts topstep-chart-app/frontend/eslint.config.js /build/topstep-chart-app/frontend/
COPY topstep-chart-app/frontend/src/ /build/topstep-chart-app/frontend/src/

RUN pnpm --dir /build/topstep-chart-app/frontend build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_BIND_HOST=127.0.0.1 \
    APP_PORT=8000 \
    DATA_DIR=/app/data \
    FRONTEND_DIST_DIR=/app/frontend/dist

RUN pip install --no-cache-dir uv

WORKDIR /app/backend

COPY topstep-chart-app/backend/pyproject.toml topstep-chart-app/backend/uv.lock /app/backend/

RUN uv sync --frozen --no-dev --no-install-project

COPY topstep-chart-app/backend/app/ /app/backend/app/
COPY --from=frontend-builder /build/topstep-chart-app/frontend/dist /app/frontend/dist

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD [".venv/bin/python", "-c", "import os, urllib.request; host = os.environ.get('APP_BIND_HOST', '127.0.0.1').strip(); url_host = f'[{host}]' if ':' in host and not host.startswith('[') else host; port = os.environ.get('APP_PORT', '8000'); urllib.request.urlopen(f'http://{url_host}:{port}/api/health', timeout=3)"]

CMD [".venv/bin/python", "-m", "app.main"]
