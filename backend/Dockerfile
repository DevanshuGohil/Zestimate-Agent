# syntax=docker/dockerfile:1.7
# --------------------------------------------------------------------------
# Build stage: install dependencies with uv into an isolated prefix
# --------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# Use /app so the venv's shebang paths match the runtime stage exactly
WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Copy dependency manifests first (cache layer)
COPY pyproject.toml uv.lock ./

# Install only production deps (no dev extras, skip the project itself)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy source and install the package itself (generates entry-point scripts)
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------------
# Runtime stage: minimal image, no build tools
# --------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Non-root user for principle of least privilege
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Copy the installed virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Put venv on PATH so entry-point scripts resolve without activation
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runtime env defaults (all can be overridden at `docker run` or in compose)
ENV API_HOST=0.0.0.0 \
    API_PORT=8000 \
    API_WORKERS=1 \
    LOG_LEVEL=INFO \
    CACHE_DIR=/app/data \
    CACHE_TTL_HOURS=24 \
    CACHE_FAILURE_TTL_HOURS=6

EXPOSE 8000

# Create data directory and hand it to the app user before switching
RUN mkdir -p /app/data && chown -R app:app /app/data

# Persist SQLite cache in a named volume so it survives container restarts
VOLUME ["/app/data"]

# Health check uses the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${API_PORT}/health')" || exit 1

USER app

CMD ["zestimate-api"]
