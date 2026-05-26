# ═══════════════════════════════════════════════════════════════════════════════
#  Vera Orchestrator — Dockerfile
# ═══════════════════════════════════════════════════════════════════════════════
#  Multi-stage build: deps layer cached separately from source code.
#
#  Build:   docker build -t vera-orchestrator .
#  Run:     docker run -p 8999:8999 --env-file .env vera-orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Dependencies ────────────────────────────────────────────────────
FROM python:3.11-slim AS deps

WORKDIR /app

# System deps for asyncpg, neo4j, and other compiled packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime-only system libs (libpq for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code
# Assumes project lives under Vera/Orchestration/ or flat in the build context
COPY . /app/Vera/Orchestration/

# Make the package importable
RUN touch /app/Vera/__init__.py /app/Vera/Orchestration/__init__.py

# Create project data directory
RUN mkdir -p /data/projects

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8999

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -sf http://localhost:8999/docs || exit 1

CMD ["uvicorn", "Vera.Orchestration.capability_orchestration:APP", \
     "--host", "0.0.0.0", "--port", "8999", "--workers", "1"]