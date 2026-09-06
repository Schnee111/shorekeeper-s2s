# ==============================================================================
# Multi-stage Dockerfile for Shorekeeper S2S Agent (LiveKit + Gemini Live)
# Builder: python:3.11-slim-bookworm with uv package manager
# Target Architecture: x86_64 Linux with glibc (LiveKit C-extensions compatible)
# ==============================================================================

FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# Install uv for ultra-fast and locked dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.5.26 /uv /bin/uv

# System build dependencies for audio & C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY apps/agent/pyproject.toml apps/agent/uv.lock ./

# Install dependencies into dedicated virtualenv
RUN uv sync --frozen --no-dev --no-install-project

# ==============================================================================
# Production Runtime Stage
# ==============================================================================
FROM python:3.11-slim-bookworm AS runtime

# System audio runtime libraries required by LiveKit & WebRTC
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libasound2 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash shorekeeper

WORKDIR /app

# Copy virtualenv and application code
COPY --from=builder /build/.venv /app/.venv
COPY apps/agent/src /app/src

# Set environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER shorekeeper

ENTRYPOINT ["python", "src/agent_gemini_live.py", "start"]
