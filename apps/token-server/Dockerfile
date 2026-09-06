# ==============================================================================
# Multi-stage Dockerfile for Shorekeeper S2S Token Server (aiohttp port 8083)
# ==============================================================================

FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:0.5.26 /uv /bin/uv

COPY apps/token-server/pyproject.toml apps/token-server/uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.11-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash shorekeeper

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv
COPY apps/token-server/token_server.py /app/token_server.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SHOREKEEPER_TOKEN_PORT=8083 \
    SHOREKEEPER_AGENT_NAME=shorekeeper

USER shorekeeper

EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8083/health || exit 1

ENTRYPOINT ["python", "token_server.py"]
