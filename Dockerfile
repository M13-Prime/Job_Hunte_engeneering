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
# Note: not using --frozen so we can deploy a pyproject.toml change without
# round-tripping uv.lock through the GitHub API (push pipeline limitation).
RUN uv sync --no-dev
# Optional JobSpy collector (Indeed / LinkedIn / Google Jobs). Off by default:
#   INSTALL_JOBSPY=1 docker compose -f docker-compose.lite.yml up -d --build
ARG INSTALL_JOBSPY=0
RUN if [ "$INSTALL_JOBSPY" = "1" ]; then \
        uv pip install --python /app/.venv/bin/python python-jobspy; \
    fi

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

# Optional LinkedIn channel (agent-reach / mcporter). Off by default; adds
# Node.js (~150 MB), so only enable it if you set LINKEDIN_ENABLED=true:
#   INSTALL_LINKEDIN=1 docker compose -f docker-compose.lite.yml up -d --build
# The `linkedin` MCP server itself (Chromium via Patchright) runs outside the
# image: log in on a machine with a display, then mount ~/.linkedin-mcp and
# the mcporter config (see docs/LINKEDIN.md).
ARG INSTALL_LINKEDIN=0
RUN if [ "$INSTALL_LINKEDIN" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends nodejs npm \
        && npm install -g mcporter \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/    ./src/
COPY --chown=app:app scripts/ ./scripts/
COPY --chown=app:app config/  ./config/
COPY --chown=app:app Makefile pyproject.toml README.md ./

# /data is a mounted volume (SQLite file, scraping caches, snapshots, etc.).
# /app/data is also created and chown'd so legacy relative cache paths like
# "data/cache/rss" written from WORKDIR=/app still resolve to a writable dir.
RUN mkdir -p /data /app/data /home/app/.mcporter /home/app/.linkedin-mcp \
    && chown -R app:app /data /app/data /home/app/.mcporter /home/app/.linkedin-mcp
VOLUME ["/data"]

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)" \
    || exit 0  # only meaningful for the dashboard service; scheduler ignores it.

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "signal_tracker.main"]
