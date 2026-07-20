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

# Runtime-only system libs (libpq for asyncpg). pandoc + wkhtmltopdf power the
# render.export document capabilities (DOCX/PDF/HTML/ODT/PPTX/… via vera/render).
# wkhtmltopdf was REMOVED from Debian bookworm (python:3.11-slim's base) — a
# hard `apt-get install wkhtmltopdf` exits 100 and kills the whole build, so it
# is best-effort: when unavailable, PDF export falls back to pandoc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        pandoc \
    && (apt-get install -y --no-install-recommends wkhtmltopdf \
        || echo "wkhtmltopdf not in this suite — skipping (pandoc PDF fallback)") \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code. The build context is the REPO ROOT, whose `vera/`
# package directory must land at /app/Vera/vera so that
# `python -m Vera.vera.capability_orchestration` (with PYTHONPATH=/app)
# resolves. Copying to /app/Vera/vera/ (the old path) nested the package one
# level too deep (/app/Vera/vera/vera/…) and the container could never start.
COPY . /app/Vera/

# Make the package importable
RUN touch /app/Vera/__init__.py /app/Vera/vera/__init__.py

# Create project data directory
RUN mkdir -p /data/projects

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8999

# Try HTTPS first (TLS_ENABLED=1, self-signed → -k), fall back to plain HTTP.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -skf https://localhost:8999/docs || curl -sf http://localhost:8999/docs || exit 1

# Launch via the module entrypoint (not the uvicorn CLI) so TLS_ENABLED is
# honoured: it auto-generates a self-signed cert and serves HTTPS when set.
CMD ["python", "-m", "Vera.vera.capability_orchestration"]