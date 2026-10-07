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
  • Legacy callers fail OPEN — if the gate is disabled, the node is
    ungated, or the coordination Redis is unreachable/errors, acquire returns
    None and the caller proceeds unslotted. The gate can only ever ADD waiting;
    it must never be able to BREAK generation. Explicit `required=True`
    acquisition instead raises GateAcquisitionError if no lease is obtained.
    This opt-in primitive does not enable sandbox coordination by itself.

Pure helpers (env/policy/key-shape) are separated from the async Redis calls so
the policy is unit-testable without a live Redis.
"""
import asyncio
import math
import os
import socket
import time
import uuid
from typing import Any, Dict, List, Optional

_HOST = socket.gethostname()

# Release only if we still own the slot — avoids deleting a slot that expired
# and was re-acquired by another process in the meantime.
_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")
# Refresh the TTL only if we still own the slot — the heartbeat's owner-fenced
# renew, so a lease that already expired and was re-taken by someone else is
# never resurrected under them.
_RENEW_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
              "then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end")


# ── pure policy/helpers (no I/O) ─────────────────────────────────────────────

def gate_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("VERA_OLLAMA_GATE", "")).strip().lower() in (
        "1", "true", "yes", "on")


def capacity_for(has_gpu: bool, env: Optional[Dict[str, str]] = None) -> int:
    """Max concurrent generations allowed across ALL processes for a node.
    GPU nodes default to 1 (a single 24GB card can't run two large models at
    once without thrashing). Non-GPU nodes default to 2 (user, 2026-09-28: "let
    2 llm/ollama jobs go through to the cpu nodes at once"): their Ollama runs
    two slots (ollama_node_core.CPU_NUM_PARALLEL), measured +20-35% total
    throughput, and a third generation queues here rather than splitting the
    cores three ways. Embeddings never take the gate, so a node generating
    still embeds. 0 = ungated. Override via VERA_GPU_GATE_N / VERA_NODE_GATE_N."""
    env = os.environ if env is None else env
    if has_gpu:
        return max(0, int(env.get("VERA_GPU_GATE_N", "1") or 1))
    return max(0, int(env.get("VERA_NODE_GATE_N", "2") or 0))


def ttl_ms(env: Optional[Dict[str, str]] = None) -> int:
    """Slot-lease lifetime. Must comfortably outlast a full generation so a
    long (but healthy) stream never loses its slot mid-flight. Default 30 min.

    NOTE: this is the NON-renewed fallback TTL. The gate now holds a lease with
    a SHORT renewable TTL (`lease_ttl_ms`) refreshed by a heartbeat while the
    generation runs, so an ORPHANED slot (holder cancelled/crashed, heartbeat
    stopped) self-heals within `lease_ttl_ms` instead of wedging the node for
    the full 30 min. Kept for callers that acquire without a heartbeat."""
    env = os.environ if env is None else env
    return int(float(env.get("VERA_GATE_TTL_S", "1800") or 1800) * 1000)


def lease_ttl_ms(env: Optional[Dict[str, str]] = None) -> int:
    """SHORT, RENEWABLE lease TTL used with a heartbeat. An orphaned slot (the
    holder was cancelled/crashed and its heartbeat stopped) expires within this
    window — so a wedged GPU self-heals in ~a minute instead of the 30-min hard
    TTL that repeatedly blocked the node (2026-08-18). A LIVE generation keeps
    its slot indefinitely because the heartbeat renews it well before expiry."""
    env = os.environ if env is None else env
    return int(float(env.get("VERA_GATE_LEASE_TTL_S", "90") or 90) * 1000)


def renew_interval_s(env: Optional[Dict[str, str]] = None) -> float:
    """How often the heartbeat refreshes a held lease. Comfortably shorter than
    `lease_ttl_ms` (default 30s vs 90s) so two missed beats still don't expire a
    live slot, while an orphan is gone within one lease TTL."""
    env = os.environ if env is None else env
    return float(env.get("VERA_GATE_RENEW_S", "30") or 30)


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

class GateAcquisitionError(RuntimeError):
    """A required lease was not acquired; callers must not dispatch inference."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"required inference lease unavailable: {reason}")


async def acquire(r, node: str, capacity: int, ttl: int, wait: float,
                  poll: float = 0.25, owner: Optional[str] = None, *,
                  required: bool = False
                  ) -> Optional[Dict[str, Any]]:
    """Try to claim one of `capacity` slots for `node`. Returns a lease dict on
    success, or None (proceed unslotted) when ungated / no Redis / Redis errors
    / waited longer than `wait`. Legacy calls fail open. With required=True,
    configuration, connectivity and queue failures raise GateAcquisitionError.
    Cancellation always propagates. Redis operations still require a transport
    timeout (or an outer deadline); `wait` bounds contention, not stalled I/O.
    """
    def unavailable(reason):
        if required:
            raise GateAcquisitionError(reason)
        return None

    if required and (not node or not math.isfinite(wait) or wait < 0
                     or not math.isfinite(poll) or poll <= 0
                     or not math.isfinite(ttl) or ttl < 1):
        raise GateAcquisitionError("invalid_policy")
    if capacity <= 0 or r is None:
        return unavailable("ungated_node" if capacity <= 0 else "coordination_unavailable")
    owner = owner or new_owner()
    started = time.monotonic()
    deadline = started + max(0.0, wait)
    while True:
        for i in range(capacity):
            k = slot_key(node, i)
            try:
                ok = await r.set(k, owner, nx=True, px=int(ttl))
            except Exception:
                # Transport exception text may contain credentials.
                return unavailable("coordination_error")
            if ok:
                return {"key": k, "owner": owner, "node": node, "slot": i,
                        "waited_s": round(time.monotonic() - started, 2)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return unavailable("queue_timeout")
        await asyncio.sleep(min(poll, remaining))


async def release(r, lease: Optional[Dict[str, Any]]) -> bool:
    """Release a slot, but only if we still own it (owner-fenced). Never raises."""
    if not lease or r is None:
        return False
    try:
        res = await r.eval(_RELEASE_LUA, 1, lease["key"], lease["owner"])
        return bool(res)
    except Exception:
        return False


async def renew(r, lease: Optional[Dict[str, Any]], ttl: int) -> bool:
    """Refresh a held slot's TTL to `ttl` ms, owner-fenced (only if we still own
    it). The heartbeat's primitive: a live generation calls this on an interval
    so its short lease never expires; when the holder is gone the calls stop and
    the slot expires on its own. Never raises."""
    if not lease or r is None:
        return False
    try:
        res = await r.eval(_RENEW_LUA, 1, lease["key"], lease["owner"], int(ttl))
        return bool(res)
    except Exception:
        return False


def parse_owner(owner: str):
    """Split an owner token 'host:pid:hash' -> (host, pid|None). Tolerant of odd
    shapes (returns (raw, None) when the pid segment isn't an int)."""
    parts = str(owner or "").split(":")
    if len(parts) < 2:
        return (str(owner or ""), None)
    try:
        return (parts[0], int(parts[1]))
    except (ValueError, TypeError):
        return (parts[0], None)


def is_reapable_local_lease(owner: str, this_host: str, pid_alive) -> bool:
    """Decide (purely) whether a leaked gate lease is SAFE to force-clear on startup.
    Safe ONLY when the lease was minted on THIS host AND its pid is no longer alive
    here — i.e. a crashed/restarted local process that never released its slot, which
    otherwise wedges the node for the full 30-min TTL. Leases from ANOTHER host are
    never touched (this node can't verify a peer's pid, and clearing a peer's live
    slot would double-book the GPU). Unknown shapes are left alone."""
    host, pid = parse_owner(owner)
    if not host or host != this_host or pid is None:
        return False
    return not pid_alive(pid)


def is_lease_of_host(owner: str, host: str) -> bool:
    """True when this lease was minted by `host`.

    Deliberately NOT a variant of is_reapable_local_lease: that one asks "did a
    dead process on MY host leak this", and its refusal to touch another host's
    lease is a safety rule that must stay intact. This asks a different, narrower
    question with a different justification — see release_leases_for_host.
    """
    h, _pid = parse_owner(owner)
    return bool(h) and bool(host) and h == host


async def release_leases_for_host(r, host: str) -> Dict[str, Any]:
    """Clear every gate slot owned by `host`. Caller must KNOW that host cannot
    still be working.

    The one legitimate use is immediately before `docker pause`. A paused
    container is SIGSTOPped: it cannot renew its lease, cannot release it, and
    cannot use it — but the lease still looks live (unexpired, owned), so
    sweep_dead_local_leases correctly refuses to reclaim it, since it cannot
    verify a peer's liveness and clearing a live peer slot would double-book the
    GPU. A frozen owner is precisely the case that rule cannot distinguish.

    Observed 2026-08-30: the container `vera-dev`, paused, held the capacity-1
    GPU slot; a census loop waited its entire 25-minute cap and recorded
    planned=0 executed=0 — it never got a slot at all.

    Pausing is what makes this safe. We are not guessing whether the owner is
    alive: we are about to freeze it, so it definitionally does no further work
    with the slot. And the idle-reap only pauses containers it has judged IDLE,
    so a lease still held at that point is already leaked. Never raises.
    """
    cleared: List[Dict[str, str]] = []
    if r is None or not host:
        return {"cleared": [], "count": 0}
    try:
        keys = []
        async for k in r.scan_iter(match="vera:ollama:gate:*:slot:*"):
            keys.append(k.decode() if isinstance(k, (bytes, bytearray)) else str(k))
    except Exception:
        return {"cleared": [], "count": 0, "error": "scan_iter unavailable"}
    for k in keys:
        try:
            v = await r.get(k)
        except Exception:
            continue
        if v is None:
            continue
        owner = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if is_lease_of_host(owner, host):
            try:
                if await r.eval(_RELEASE_LUA, 1, k, owner):   # owner-fenced del
                    cleared.append({"key": k, "owner": owner})
            except Exception:
                pass
    return {"cleared": cleared, "count": len(cleared)}


def _pid_alive(pid: int) -> bool:
    """True if a local pid exists. os.kill(pid, 0): no error = alive; ProcessLookupError
    = dead; PermissionError = alive (owned by another user). Any other error -> assume
    alive so the sweep never clears a lease it isn't sure is dead."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


async def sweep_dead_local_leases(r) -> Dict[str, Any]:
    """Startup sweep: clear gate slots leaked by a dead LOCAL process. A holder that
    dies mid-generation (crash / kill / restart) leaves its TTL-fenced lease held for
    the full TTL (default 30 min), wedging the node until it expires — the exact thing
    that blocked the GPU on 2026-08-17. This clears such orphans on boot, cluster-safe
    (this host's own dead-pid leases only) and owner-fenced (CAS del). Never raises."""
    cleared = []
    if r is None:
        return {"cleared": [], "count": 0}
    try:
        keys = []
        async for k in r.scan_iter(match="vera:ollama:gate:*:slot:*"):
            keys.append(k.decode() if isinstance(k, (bytes, bytearray)) else str(k))
    except Exception:
        return {"cleared": [], "count": 0, "error": "scan_iter unavailable"}
    for k in keys:
        try:
            v = await r.get(k)
        except Exception:
            continue
        if v is None:
            continue
        owner = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if is_reapable_local_lease(owner, _HOST, _pid_alive):
            try:
                if await r.eval(_RELEASE_LUA, 1, k, owner):   # owner-fenced del
                    cleared.append({"key": k, "owner": owner})
            except Exception:
                pass
    return {"cleared": cleared, "count": len(cleared)}


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
