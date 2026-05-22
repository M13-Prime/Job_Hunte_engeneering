# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Builder stage — install uv + sync deps into /app/.venv
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy lockfile + project metadata first so layer cache survives source edits.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime stage — slim image with only the venv + project source
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DB_PATH=/data/signals.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd  --uid 1000 --gid app --shell /bin/bash --create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/    ./src/
COPY --chown=app:app scripts/ ./scripts/
COPY --chown=app:app config/  ./config/
COPY --chown=app:app Makefile pyproject.toml README.md ./

# /data is a mounted volume (SQLite file, scraping caches, snapshots, etc.).
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)" \
    || exit 0  # only meaningful for the dashboard service; scheduler ignores it.

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "signal_tracker.main"]
