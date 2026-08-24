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
        git \
    && (apt-get install -y --no-install-recommends wkhtmltopdf \
        || echo "wkhtmltopdf not in this suite — skipping (pandoc PDF fallback)") \
    && rm -rf /var/lib/apt/lists/*

# Dev containers bind-mount the repo/worktree so you can commit from inside (e.g.
# the VS Code terminal). Files are owned by the host repo user but git runs as
# root here, so git's dubious-ownership guard would otherwise block every command
# — these are our own trusted sandboxes, so mark all mounted repos safe.
RUN git config --system --add safe.directory '*'

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Dev/test tooling (pytest) baked in so dev sandboxes + the ephemeral test runner
# (evolve.unittest.run) don't pip-install at runtime. Best-effort: a PyPI hiccup at
# build time must not kill the image (the runner falls back to an in-container install).
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt \
    || echo "dev tools not installed at build — evolve.unittest.run will pip-install at run"

# Operator browser extra (Playwright + Chromium). PARITY: prod runs natively from a
# host venv that already has playwright + browsers, so operator.run works there — but
# this image did not, so every browser-verification step inside a dev sandbox died with
# "Playwright is not installed in this environment". The loop would correctly plan
# operator.run (the only cap that can prove a page BEHAVES) and then be unable to run it,
# which also made that path untestable in a sandbox. `--with-deps` pulls the system libs
# headless chromium needs (libnss3, libatk, …); without them the browser cannot launch
# even when the wheel is present. Chromium only — firefox/webkit are not used here and
# would add several hundred MB more.
# Best-effort, exactly like the dev tooling above: a PyPI/CDN hiccup at build time must
# not kill the image, and operator.run already reports the missing extra clearly.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements-operator.txt .
RUN (pip install --no-cache-dir -r requirements-operator.txt \
     && playwright install --with-deps chromium) \
    || echo "operator browser extra not installed at build — operator.run will say so"

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