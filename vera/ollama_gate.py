"""Cross-process Ollama concurrency gate — the "one big queue".

Every Vera process (prod + each dev sandbox container) talks to ONE physical
Ollama cluster. `pick_instance()` load-balances WITHIN a process and a local
asyncio.Semaphore serialises WITHIN a process — but nothing coordinates ACROSS
processes, so N Vera instances independently flood the same GPU node and fight
for VRAM. This module is a bounded, crash-safe semaphore per node, kept on a
SHARED coordination Redis DB that prod and every dev container both reach. A
caller must hold a slot before it generates and releases it after.

Design guarantees:
  • Crash-safe — slots are TTL-fenced leases (SET NX PX). A holder that dies
    (container killed mid-generation) has its slot auto-expire, so the queue
    never wedges on a dead lease.
  • Owner-fenced release — release only deletes a slot the caller still owns
    (Lua CAS), so a lease that already expired and was re-taken by someone else
    is never stolen back.
  • Fail-OPEN, never fail-closed — if the gate is disabled, the node is
    ungated, or the coordination Redis is unreachable/errors, acquire returns
    None and the caller proceeds unslotted. The gate can only ever ADD waiting;
    it must never be able to BREAK generation.

Pure helpers (env/policy/key-shape) are separated from the async Redis calls so
the policy is unit-testable without a live Redis.
"""
import asyncio
import os
import socket
import time
import uuid
from typing import Any, Dict, Optional

_HOST = socket.gethostname()

# Release only if we still own the slot — avoids deleting a slot that expired
# and was re-acquired by another process in the meantime.
_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")


# ── pure policy/helpers (no I/O) ─────────────────────────────────────────────

def gate_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("VERA_OLLAMA_GATE", "")).strip().lower() in (
        "1", "true", "yes", "on")


def capacity_for(has_gpu: bool, env: Optional[Dict[str, str]] = None) -> int:
    """Max concurrent generations allowed across ALL processes for a node.
    GPU nodes default to 1 (a single 24GB card can't run two large models at
    once without thrashing). Non-GPU nodes default to 0 = ungated (CPU boxes
    are the pressure-relief valve; serialising them would only create queues
    where there is spare capacity). Override via VERA_GPU_GATE_N / VERA_NODE_GATE_N."""
    env = os.environ if env is None else env
    if has_gpu:
        return max(0, int(env.get("VERA_GPU_GATE_N", "1") or 1))
    return max(0, int(env.get("VERA_NODE_GATE_N", "0") or 0))


def ttl_ms(env: Optional[Dict[str, str]] = None) -> int:
    """Slot-lease lifetime. Must comfortably outlast a full generation so a
    long (but healthy) stream never loses its slot mid-flight. Default 30 min."""
    env = os.environ if env is None else env
    return int(float(env.get("VERA_GATE_TTL_S", "1800") or 1800) * 1000)


def wait_s(env: Optional[Dict[str, str]] = None) -> float:
    """How long a caller queues for a free slot before proceeding UNSLOTTED
    (fail-open). Long enough that queueing behind one big job is normal; finite
    so a wedged node can't strand every caller forever. Default 10 min."""
    env = os.environ if env is None else env
    return float(env.get("VERA_GATE_WAIT_S", "600") or 600)


def slot_key(node: str, i: int) -> str:
    return f"vera:ollama:gate:{node}:slot:{i}"


def new_owner() -> str:
    return f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# ── async gate (needs a coordination Redis client) ───────────────────────────

async def acquire(r, node: str, capacity: int, ttl: int, wait: float,
                  poll: float = 0.25, owner: Optional[str] = None
                  ) -> Optional[Dict[str, Any]]:
    """Try to claim one of `capacity` slots for `node`. Returns a lease dict on
    success, or None (proceed unslotted) when ungated / no Redis / Redis errors
    / waited longer than `wait`. NEVER raises — the gate can only add waiting."""
    if capacity <= 0 or r is None:
        return None
    owner = owner or new_owner()
    deadline = time.time() + max(0.0, wait)
    while True:
        for i in range(capacity):
            k = slot_key(node, i)
            try:
                ok = await r.set(k, owner, nx=True, px=int(ttl))
            except Exception:
                return None  # coordination Redis down/errored → fail-open
            if ok:
                return {"key": k, "owner": owner, "node": node, "slot": i,
                        "waited_s": round(time.time() - (deadline - wait), 2)}
        if time.time() >= deadline:
            return None  # queued long enough → proceed unslotted
        await asyncio.sleep(poll)


async def release(r, lease: Optional[Dict[str, Any]]) -> bool:
    """Release a slot, but only if we still own it (owner-fenced). Never raises."""
    if not lease or r is None:
        return False
    try:
        res = await r.eval(_RELEASE_LUA, 1, lease["key"], lease["owner"])
        return bool(res)
    except Exception:
        return False


async def occupancy(r, node: str, capacity: int) -> Dict[str, Any]:
    """Observability: how many of a node's slots are currently held, and by
    whom. Read-only; safe to call from a status cap."""
    if capacity <= 0 or r is None:
        return {"node": node, "capacity": capacity, "held": 0, "owners": []}
    owners = []
    for i in range(capacity):
        try:
            v = await r.get(slot_key(node, i))
        except Exception:
            v = None
        if v is not None:
            owners.append(v.decode() if isinstance(v, (bytes, bytearray)) else str(v))
    return {"node": node, "capacity": capacity, "held": len(owners),
            "free": capacity - len(owners), "owners": owners}
