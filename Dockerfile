# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.1 AS uv

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 benji \
    && useradd --uid 10001 --gid benji --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin benji

USER benji

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.getenv('PORT', '8000'); urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://127.0.0.1:{port}/health', timeout=2)"

CMD ["sh", "-c", "exec uvicorn benji_api.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
