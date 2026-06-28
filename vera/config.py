"""
vera_config.py  —  Central configuration for the Vera platform
==============================================================

All environment variable reads and defaults live here.
Every other module imports from here rather than reading os.getenv directly.

Usage:
    from Vera.vera.vera_config import cfg

Override any value via environment variable before starting:
    export REDIS_URL=redis://myhost:6379
    export POSTGRES_URL=postgresql://admin:admin@myhost:5432/llm
"""

import os
from pathlib import Path


def _load_dotenv_files() -> None:
    """Load KEY=VALUE pairs from the repo-root .env (no external deps).

    docker compose reads .env automatically, but *native* launches
    (./build.sh run, make run, or `python -m Vera.vera.capability_orchestration`)
    do not — so without this, .env overrides like TLS_ENABLED were silently
    ignored when running outside Docker. Variables already present in the
    environment are NEVER overridden, so an explicit `TLS_ENABLED=1 ./build.sh
    run` or compose-injected values still take precedence.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",  # repo root: Vera/.env
        Path.cwd() / ".env",
    ]
    seen: set = set()
    for path in candidates:
        try:
            rp = path.resolve()
        except OSError:
            continue
        if rp in seen or not rp.is_file():
            continue
        seen.add(rp)
        try:
            lines = rp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or key in os.environ:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val


_load_dotenv_files()


class VeraConfig:
    # ── Network / Hosts ────────────────────────────────────────────────────────
    # Single internal-domain variable. Host-derived defaults across the codebase
    # (SearXNG, research data sources, the Google OAuth redirect base, etc.) build
    # on this, and the orchestrator injects it into served HTML as
    # window.__VERA_DOMAIN__ / window.__VERA_BASE__ so browser panels resolve the
    # backend from config too. Set BACKEND_HOST once to retarget the whole stack.
    # The orchestrator itself binds to 0.0.0.0 so it's reachable from all hosts.
    BACKEND_HOST  : str = os.getenv("BACKEND_HOST",  "llm.int")

    # ── FastAPI / Uvicorn ──────────────────────────────────────────────────────
    ORCHESTRATOR_HOST : str = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
    ORCHESTRATOR_PORT : int = int(os.getenv("ORCHESTRATOR_PORT", "8999"))

    # ── TLS / HTTPS ────────────────────────────────────────────────────────────
    # Browsers only expose "secure context" features — the Web Serial API
    # (navigator.serial) and getUserMedia (webcam/mic) — over HTTPS or to
    # localhost. Set TLS_ENABLED=1 to serve the orchestrator over HTTPS so those
    # work when Vera is opened from another machine on the LAN (by IP/hostname).
    # HTTPS is served on the SAME ORCHESTRATOR_PORT — plain http:// clients then
    # need to switch to https://. If no cert/key is supplied, a self-signed pair
    # is generated at the default paths on first start (browsers show a one-time
    # "not trusted" warning that must be accepted — it is still a secure context).
    TLS_ENABLED  : bool = os.getenv("TLS_ENABLED", "0") == "1"
    # expanduser so a `TLS_CERTFILE=~/...` value (env or .env) resolves correctly.
    TLS_CERTFILE : str = os.path.expanduser(os.getenv(
        "TLS_CERTFILE",
        os.path.join(os.path.expanduser("~"), ".vera", "tls", "cert.pem")))
    TLS_KEYFILE  : str = os.path.expanduser(os.getenv(
        "TLS_KEYFILE",
        os.path.join(os.path.expanduser("~"), ".vera", "tls", "key.pem")))

    # ── Google OAuth (calendar) ────────────────────────────────────────────────
    # Redirect/callback base for the Google OAuth flow used by the calendar
    # module. The orchestrator serves /cal/google/oauth_callback at this base;
    # after the user approves, Google redirects the browser straight back here and
    # the refresh token is captured automatically (no code-pasting).
    #
    # Defaults to the address you actually reach Vera on (BACKEND_HOST:port) so it
    # works from any machine on the LAN — this needs a Google "Web application"
    # OAuth client with this exact <base>/cal/google/oauth_callback URI registered
    # in Google Cloud. (TLS is strongly recommended; Google only allows http for
    # localhost.) If instead you always run the browser on the same host as Vera,
    # set this to a loopback address — http://localhost:<port> — and use a
    # "Desktop app" client, which accepts any loopback port without registration.
    GOOGLE_OAUTH_REDIRECT_BASE : str = os.getenv(
        "GOOGLE_OAUTH_REDIRECT_BASE",
        f"{'https' if TLS_ENABLED else 'http'}://{BACKEND_HOST}:{ORCHESTRATOR_PORT}")

    # Optional shared Google OAuth client. Set these once and every account can
    # connect (calendar, mail, contacts …) against the same client without
    # pasting client_id/secret per-account — the "one key for everything" path.
    # Accounts may still override with their own client for split/isolated use.
    # The generic callback is served at <redirect_base>/accounts/oauth/callback;
    # register that exact URI on the Google "Web application" client.
    GOOGLE_OAUTH_CLIENT_ID     : str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    GOOGLE_OAUTH_CLIENT_SECRET : str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL         : str = os.getenv("REDIS_URL", "redis://localhost:6379")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    # Note: admin:admin credentials, database = llm
    POSTGRES_URL      : str = os.getenv(
        "POSTGRES_URL",
        "postgresql://admin:admin@localhost:5433/postgres"
    )

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    CHROMA_HOST       : str = os.getenv("CHROMA_HOST", "localhost")
    CHROMA_PORT       : int = int(os.getenv("CHROMA_PORT", "8008"))

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    NEO4J_URI         : str = os.getenv("NEO4J_URI",  "bolt://localhost:7687")
    NEO4J_USER        : str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASS        : str = os.getenv("NEO4J_PASS", "neo4j")

    # ── Ollama cluster ────────────────────────────────────────────────────────
    # gpu-250: main GPU node (V100, 16GB VRAM)
    # cpu-246, cpu-247: CPU inference nodes
    OLLAMA_GPU_URL    : str = os.getenv("OLLAMA_GPU_URL",  "http://192.168.0.250:11435")
    OLLAMA_CPU_A_URL  : str = os.getenv("OLLAMA_CPU_A_URL","http://192.168.0.246:11435")
    OLLAMA_CPU_B_URL  : str = os.getenv("OLLAMA_CPU_B_URL","http://192.168.0.247:11435")
    OLLAMA_MODEL      : str = os.getenv("OLLAMA_MODEL",    "jaahas/qwen3.5-uncensored")
    # Context window for source-review / planning generations. qwen3.5 supports
    # up to 32k natively; raise via env if your model build allows more. Larger
    # values cost more VRAM/RAM and are slower (esp. on CPU nodes).
    OLLAMA_NUM_CTX    : int = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
    # Keep the model resident between requests so a review run doesn't pay the
    # cold-load cost on every file. Passed straight to Ollama's keep_alive.
    OLLAMA_KEEP_ALIVE : str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
    OLLAMA_EMBED_URL  : str = os.getenv("OLLAMA_EMBED_URL","http://192.168.0.246:11435")
    OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL","nomic-embed-text")

    # ── GPU inference server (Whisper / TTS / SD) ─────────────────────────────
    GPU_INFER_URL     : str = os.getenv("GPU_INFER_URL", "http://192.168.0.250:8765")

    # ── IDE / Projects ────────────────────────────────────────────────────────
    # Root directory where IDE workspaces/projects are stored.
    # Created automatically on first use if it does not exist.
    VERA_PROJECT_ROOT : str = os.getenv(
        "VERA_PROJECT_ROOT",
        os.path.join(os.path.expanduser("~"), "vera_projects")
    )

    # ── Git / Gitea integration ───────────────────────────────────────────────
    # Local clone root: project git repos cloned with mode "local"/"both" land in
    # <GIT_CLONE_ROOT>/<project-slug>/<repo-name>. Created on first use.
    GIT_CLONE_ROOT    : str = os.getenv(
        "GIT_CLONE_ROOT",
        os.path.join(os.path.expanduser("~"), "vera_repos")
    )
    # Local Gitea/Forgejo instance used for mode "gitea"/"both" mirrors.
    # GITEA_TOKEN needs repo + (org) create scope. GITEA_OWNER is the user/org
    # that migrated repos are created under (defaults to the token's user).
    GITEA_BASE_URL    : str = os.getenv("GITEA_BASE_URL", "").rstrip("/")
    GITEA_TOKEN       : str = os.getenv("GITEA_TOKEN", "")
    GITEA_OWNER       : str = os.getenv("GITEA_OWNER", "")

    # ── Memory / DAG ─────────────────────────────────────────────────────────
    MAX_CAPS_IN_PROMPT: int  = int(os.getenv("MAX_CAPS_IN_PROMPT", "25"))
    EMBED_CAPS_ON_START: bool = os.getenv("EMBED_CAPS_ON_START", "1") == "1"

    # ── Syslog ────────────────────────────────────────────────────────────────
    SYSLOG_MAXLEN     : int  = int(os.getenv("SYSLOG_MAXLEN",      "5000"))
    SYSLOG_MONITOR_INT: int  = int(os.getenv("SYSLOG_MONITOR_INT", "300"))
    SYSLOG_MONITOR    : bool = os.getenv("SYSLOG_MONITOR",         "1") == "1"

    # ── Module loading ────────────────────────────────────────────────────────
    # Paths relative to the orchestrator file's directory
    MODULE_FILES: list = [
        "vera_capabilities.py",
        "vera_skills.py",
        "vera_memory.py",
        "vera_dag_store.py",
        "vera_agents.py",
        "vera_cluster.py",
        "vera_syslog.py",
        "vera_memory_hooks.py",
    ]

    # ── Ollama worker registry ─────────────────────────────────────────────────
    # Each entry: name → {url, has_gpu, priority}
    # priority: lower = preferred for heavy workloads
    OLLAMA_INSTANCES: dict = {
        "gpu-250": {
            "url":      os.getenv("OLLAMA_GPU_URL",   "http://192.168.0.250:11435"),
            "has_gpu":  True,
            "priority": 0,
        },
        "cpu-246": {
            "url":      os.getenv("OLLAMA_CPU_A_URL", "http://192.168.0.246:11435"),
            "has_gpu":  False,
            "priority": 1,
        },
        "cpu-247": {
            "url":      os.getenv("OLLAMA_CPU_B_URL", "http://192.168.0.247:11435"),
            "has_gpu":  False,
            "priority": 2,
        },
    }

    def __repr__(self):
        return (
            f"VeraConfig(redis={self.REDIS_URL!r}, "
            f"pg={self.POSTGRES_URL!r}, "
            f"chroma={self.CHROMA_HOST}:{self.CHROMA_PORT}, "
            f"neo4j={self.NEO4J_URI!r}, "
            f"orchestrator={self.ORCHESTRATOR_HOST}:{self.ORCHESTRATOR_PORT})"
        )


# Singleton — import this everywhere
cfg = VeraConfig()