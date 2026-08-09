"""Named capacity pools — one generalized lease primitive for scarce seats.

Generalizes the `ollama_gate` mechanism (crash-safe TTL-fenced leases on a
shared coordination Redis, owner-fenced release via Lua CAS, fail-OPEN) to
ARBITRARY named pools, so Claude subscription **seats** and any future bounded
resource are the same shape as Ollama **nodes**: a bounded pool of leases
(swarm §6.5.2). Writing a second scheduler for Claude seats would give two
half-correct schedulers with different failure modes — the hard parts
(crash-safety, cross-process visibility, fail-open) are already solved here.

Boundary with `ollama_gate`: the gate still OWNS the Ollama-node leases (this
module must not double-gate them). This module owns **seat** pools (Claude
subscriptions, etc.) and provides a unified read-only view; the Ollama side is
surfaced by reading the gate's occupancy, never re-leased here.

A pool id is `<kind>:<name>` — e.g. `claude:acct-a`, `claude:acct-b`. Seats are
REGISTERED (they map to an enrolled credential, §6.5.4), never assumed, so the
default registry is empty until a seat is enrolled.

Pure helpers (key shape, registry parse, cooling/seat-state policy) are separated
from the async Redis calls so the policy is unit-testable without a live Redis.
"""
import asyncio
import json
import os
import socket
import time
import uuid
from typing import Any, Dict, List, Optional

_HOST = socket.gethostname()

_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")


# ── pure policy / helpers (no I/O) ───────────────────────────────────────────
def pool_slot_key(pool: str, i: int) -> str:
    return f"vera:capacity:{pool}:slot:{i}"


def seats_key() -> str:
    """Redis hash of registered seat pools → JSON {slots, kind, label, ...}."""
    return "vera:capacity:seats"


def new_owner() -> str:
    return f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def ttl_ms(env: Optional[Dict[str, str]] = None) -> int:
    """Lease lifetime — must outlast the longest healthy run on the seat.
    Default 1 h (a Claude run can be long). Override VERA_CAPACITY_TTL_S."""
    env = os.environ if env is None else env
    return int(float(env.get("VERA_CAPACITY_TTL_S", "3600") or 3600) * 1000)


def wait_s(env: Optional[Dict[str, str]] = None) -> float:
    """How long a caller queues for a free seat before giving up (fail-open →
    caller decides: queue the item, don't spin). Default 0 = don't block; the
    board queues instead of holding a coroutine. Override VERA_CAPACITY_WAIT_S."""
    env = os.environ if env is None else env
    return float(env.get("VERA_CAPACITY_WAIT_S", "0") or 0)


def parse_pools(raw: Any) -> Dict[str, int]:
    """Parse a {pool: slots} mapping (dict or JSON string) → sane {str: int>=0}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = max(0, int(v))
        except Exception:
            continue
    return out


def is_cooling(seat: Dict[str, Any], now: Optional[float] = None) -> bool:
    """A seat that reported a usage limit is 'cooling' until its expected return
    (swarm §6.5.3 — reuses §4.3's declared-block idea). Routing skips it.
    `seat['cooling_until']` is an epoch seconds float; absent/past ⇒ available."""
    now = time.time() if now is None else now
    cu = seat.get("cooling_until")
    try:
        return bool(cu) and float(cu) > now
    except Exception:
        return False


def seat_state(seat: Dict[str, Any], now: Optional[float] = None) -> str:
    """available | cooling | expired | unauthed — the §6.5.5 UI state."""
    if not seat.get("enrolled", True) or seat.get("unauthed"):
        return "unauthed"
    exp = seat.get("expires_at")
    now = time.time() if now is None else now
    try:
        if exp and float(exp) <= now:
            return "expired"
    except Exception:
        pass
    return "cooling" if is_cooling(seat, now) else "available"


# ── async pool leases (needs a coordination Redis client) ────────────────────
async def acquire(r, pool: str, capacity: int, ttl: int, wait: float,
                  poll: float = 0.25, owner: Optional[str] = None
                  ) -> Optional[Dict[str, Any]]:
    """Claim one of `capacity` slots for `pool`. Lease dict on success, or None
    (proceed unslotted / queue the work) when uncapped / no Redis / errors /
    waited past `wait`. NEVER raises — a capacity gate can only ADD waiting."""
    if capacity <= 0 or r is None:
        return None
    owner = owner or new_owner()
    started = time.time()
    deadline = started + max(0.0, wait)
    while True:
        for i in range(capacity):
            k = pool_slot_key(pool, i)
            try:
                ok = await r.set(k, owner, nx=True, px=int(ttl))
            except Exception:
                return None  # coordination Redis down → fail-open
            if ok:
                return {"key": k, "owner": owner, "pool": pool, "slot": i,
                        "waited_s": round(time.time() - started, 2)}
        if time.time() >= deadline:
            return None
        await asyncio.sleep(poll)


async def release(r, lease: Optional[Dict[str, Any]]) -> bool:
    """Release a slot, owner-fenced (never steals a re-taken lease). Never raises."""
    if not lease or r is None:
        return False
    try:
        return bool(await r.eval(_RELEASE_LUA, 1, lease["key"], lease["owner"]))
    except Exception:
        return False


async def occupancy(r, pool: str, capacity: int) -> Dict[str, Any]:
    """How many of a pool's slots are held, and by whom. Read-only."""
    if capacity <= 0 or r is None:
        return {"pool": pool, "capacity": capacity, "held": 0, "free": capacity,
                "owners": []}
    owners: List[str] = []
    for i in range(capacity):
        try:
            v = await r.get(pool_slot_key(pool, i))
        except Exception:
            v = None
        if v is not None:
            owners.append(v.decode() if isinstance(v, (bytes, bytearray)) else str(v))
    return {"pool": pool, "capacity": capacity, "held": len(owners),
            "free": capacity - len(owners), "owners": owners}
