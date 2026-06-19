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

class VeraConfig:
    # ── Network / Hosts ────────────────────────────────────────────────────────
    # Backend services are on llm.int (internal network hostname)
    # The orchestrator itself binds to 0.0.0.0 so it's reachable from all hosts
    BACKEND_HOST  : str = os.getenv("BACKEND_HOST",  "llm.int")

    # ── FastAPI / Uvicorn ──────────────────────────────────────────────────────
    ORCHESTRATOR_HOST : str = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
    ORCHESTRATOR_PORT : int = int(os.getenv("ORCHESTRATOR_PORT", "8999"))

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