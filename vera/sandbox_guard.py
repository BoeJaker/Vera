"""Dev-sandbox write guard — stop testing noise leaking into prod's stores.

Dev sandbox containers share prod's Postgres, Chroma and Neo4j (only Redis and
the SQLite fabric.db are isolated per sandbox). So a dev loop that writes graph
nodes, vectors or task rows is mutating PROD. This guard closes that leak:
when the process is a dev sandbox (VERA_IS_DEV_SANDBOX=1), writes to those
SHARED prod stores are suppressed while READS pass through — dev still sees real
prod context, but can't corrupt it.

Strictly inert in prod: `write_blocked()` returns False whenever
VERA_IS_DEV_SANDBOX is unset, so every guarded call behaves exactly as before on
the real instance. The guard can only ever suppress a write in a sandbox; it can
never change prod behaviour.

Pure (env-only, no I/O) so the policy is unit-testable. Cypher classification is
here too so callers can pass a statement through `is_write_cypher()` and only
block the mutating ones (MATCH…RETURN reads still pass through for read-through).
"""
import os
import re
from typing import Optional, Dict

# Cypher clauses that MUTATE the graph. A statement containing any of these
# (as a word) is a write; everything else (MATCH/RETURN/CALL{read}/WITH) reads.
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH\s+DELETE|SET|REMOVE|DROP|"
    r"CREATE\s+INDEX|CREATE\s+CONSTRAINT|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE)


def is_dev_sandbox(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("VERA_IS_DEV_SANDBOX", "")).strip().lower() in (
        "1", "true", "yes", "on")


def write_blocked(env: Optional[Dict[str, str]] = None) -> bool:
    """True when writes to prod-SHARED stores must be suppressed: this process
    is a dev sandbox AND the guard is not explicitly disabled. Prod (no
    VERA_IS_DEV_SANDBOX) → always False (strict no-op). Escape hatch:
    VERA_SANDBOX_WRITE_GUARD=0 lets a dev deliberately write (e.g. seeding an
    isolated mirror) without turning off sandbox mode itself."""
    env = os.environ if env is None else env
    if not is_dev_sandbox(env):
        return False
    return str(env.get("VERA_SANDBOX_WRITE_GUARD", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def is_write_cypher(cypher: str) -> bool:
    """True if a Cypher statement mutates the graph. Used to let read queries
    pass through the guard while blocking writes on shared Neo4j."""
    return bool(_WRITE_CLAUSE.search(cypher or ""))


# ── READ-THROUGH: a dev sandbox reads prod's estate one way ──────────────────
# A sandbox's Redis and SQLite are its own, so every estate reading that lives
# there — health, workers, the event ring, the gate, the routing stats, the
# topology — is empty in a sandbox, and a dashboard drawn inside one shows
# nothing. A sandbox may instead read those from prod: a read-only capability
# (registered with an HTTP GET route), in one of the estate groups, is answered
# by prod's /mcp/call — reachable from a sandbox container as
# host.docker.internal:8999 (the gate broker uses the same door). Nothing goes
# the other way: only GET-routed capabilities qualify, a name with a writing
# word never does, and prod is never told anything but the read. Strictly inert
# outside a sandbox.
DEFAULT_UPSTREAM_READ_URL = "https://host.docker.internal:8999/mcp/call"
READ_THROUGH_GROUPS = (
    "obs", "sysmon", "topology", "ollama", "cluster", "models", "bench", "estate",
    "nodes", "activity", "health", "workers", "gpu", "census", "mesh", "backup",
    "perf", "jobs", "background", "catalog", "netmap", "proxmox", "docker",
    "autoenroll", "netsec", "provision", "evolve", "sandbox", "memory", "fabric",
    "markets", "ide", "openclaw", "vfs", "netmon", "identity", "dream", "loops", "research", "providers", "vllm", "ha", "n8n", "cal", "sched", "worldview", "agent", "project", "workshop", "system", "tg", "netscan", "print", "llm")
# a reading that is about THIS process, not the estate: stays local
READ_THROUGH_LOCAL = ("evolve.sandbox.status", "obs.diagnostics", "obs.modules", "obs.pending",
                      "llm.formats", "sandbox.session.list",
                      "evolve.sandbox.snapshot")   # a snapshot is taken, not read (topology.snapshot is a reading)
_WRITE_WORDS = frozenset((
    "set", "save", "delete", "remove", "write", "put", "post", "run", "start", "stop",
    "spawn", "restart", "up", "down", "prune", "reap", "apply", "enqueue", "cancel",
    "exec", "pull", "install", "activate", "pause", "resume", "approve", "attach",
    "detach", "migrate", "repair", "reconstruct", "pin", "ensure", "clear",
    "lease", "acquire", "release", "renew", "generate", "resolve", "add",
    "create", "update", "edit", "send", "kill", "restore", "compare", "loop", "passive",
    "matrix", "review", "promote", "adopt", "begin", "test", "begin", "sync", "control",
    "yield", "mark", "detect", "optimize", "autoopt", "workplan", "fs", "code", "log_status",
    "logs", "metrics", "diff", "registry", "preflight", "worktree"))


def upstream_read_url(env: Optional[Dict[str, str]] = None) -> str:
    """Where a sandbox reads prod's estate from, or '' when it must not: not a
    sandbox, or VERA_UPSTREAM_READ_URL set to 0/off. Unset means the docker
    host's own /mcp/call."""
    env = os.environ if env is None else env
    if not is_dev_sandbox(env):
        return ""
    v = env.get("VERA_UPSTREAM_READ_URL")
    if v is None:
        return DEFAULT_UPSTREAM_READ_URL
    v = str(v).strip()
    if v.lower() in ("", "0", "off", "false", "no"):
        return ""
    return v


def read_through_groups(env: Optional[Dict[str, str]] = None) -> tuple:
    env = os.environ if env is None else env
    raw = str(env.get("VERA_UPSTREAM_READ_GROUPS", "") or "").strip()
    if not raw:
        return READ_THROUGH_GROUPS
    return tuple(g.strip().lower() for g in raw.split(",") if g.strip())


# the reading names: a capability ending in one of these is a read whatever its route (its arguments name what to read)
READ_WORDS = frozenset(("status", "stats", "health", "snapshot", "summary", "list", "get", "history", "topology", "results",
                        "nodes", "installed", "info", "config", "overview", "usage", "scan", "report", "metrics", "recent", "top", "ps"))


def read_through_allowed(name: str, http_method: Optional[str],
                         env: Optional[Dict[str, str]] = None) -> bool:
    """True when a sandbox may answer this capability from prod: a GET route
    (or a reading name on any route), an estate group, no writing word in the
    name, not a reading about the sandbox itself."""
    if not upstream_read_url(env):
        return False
    n = str(name or "").strip().lower()
    # a GET route reads; so does a capability routed POST (or with no route) whose last name is a reading word - vfs.status,
    # netmon.snapshot, docker.stack.status, perf.scan are reads however they are routed
    last = n.replace("_", ".").split(".")[-1] if n else ""
    if str(http_method or "").upper() != "GET" and last not in READ_WORDS:
        return False
    if not n or n in READ_THROUGH_LOCAL or n.startswith(("sys.", "ui.", "widget.", "session.")):
        return False
    parts = [p for p in n.replace("_", ".").split(".") if p]
    if not parts or parts[0] not in read_through_groups(env):
        return False
    if any(p in _WRITE_WORDS for p in parts[1:]):
        return False
    return True
