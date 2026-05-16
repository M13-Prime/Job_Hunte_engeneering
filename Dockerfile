FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

# Install uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy lockfile + project metadata first to maximise layer cache.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev

COPY scripts/ ./scripts/
COPY config/ ./config/
COPY Makefile ./

ENV PATH="/app/.venv/bin:$PATH" \
    DB_PATH=/data/signals.db

VOLUME ["/data"]

# Default: print the help banner. docker-compose overrides this.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["make", "help"]
