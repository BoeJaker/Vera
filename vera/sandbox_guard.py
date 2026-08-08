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
