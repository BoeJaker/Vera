"""A metrics write must not resurrect a worker that is gone.

The worker registry keys `vera:workers:<id>` are meant to be self-cleaning: the
registration writes the full record and sets a 120s TTL, and the heartbeat
refreshes both. A worker that dies stops refreshing and its key expires.

The metrics loop in workers.py breaks that:

    for wid in list(WORKER_REGISTRY.keys()):
        await r.hset(f"vera:workers:{wid}", mapping={cpu_pct, ram_*, disk_*})
        meta = WORKER_META.get(wid)
        if meta:                                  # SSH-provisioned entries only
            await r.expire(f"vera:workers:{wid}", 120)

The metrics `hset` is unconditional and sets no expiry, and Redis `hset` on a
missing key CREATES it. So once a worker's key expires, the next metrics tick
recreates it - metrics only, no id, no host, no pid, no started, no capabilities
- and with no TTL, which makes it permanent. The comment above that `expire`
claims entries "expire out cleanly instead of lingering with stale data forever";
that is true of the `meta` branch and defeated by the line above it.

OBSERVED 2026-09-01, with a single instance running:

    worker-c6f22631   ttl=117s      19 fields incl. id/host/pid/started   (live)
    worker-4f5f38d6   NO EXPIRY     13 fields, no identity, metrics only  (ghost)
    worker-b6c3cead   NO EXPIRY     13 fields, no identity, metrics only  (ghost)

They are indistinguishable from live workers in obs.workers, which fills the
missing fields with "unknown" and 0 - so the estate appears to have three
workers when it has one.

The rule: metrics DESCRIBE a worker, they do not assert one exists. Write them
only onto a key that is already there, and refresh the TTL whenever writing, so
a metrics tick can never outlive the registration it decorates.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Fields written by the registration - their absence means no one registered
#: this worker, or its registration expired and something recreated the key.
IDENTITY_FIELDS = ("id", "host", "pid", "started")

#: What the registration/heartbeat sets. -1 (no expiry) on a worker key means
#: nothing is keeping it honest.
NO_EXPIRY = -1


def may_write_metrics(key_exists: bool) -> bool:
    """Metrics describe a worker; they must not conjure one.

    A missing key means the worker's registration has expired - i.e. it is gone
    - so writing metrics would recreate it as an identity-less ghost.
    """
    return bool(key_exists)


def is_ghost(record: Optional[Mapping[str, Any]], ttl: int) -> bool:
    """A registry entry that no live worker is maintaining.

    Both halves are required. A record can legitimately lack identity fields for
    a moment mid-write, and a key can legitimately have no TTL only if something
    is about to set one - but a key with neither identity NOR an expiry is not
    coming back.
    """
    if record is None:
        return False
    has_identity = any(str((record or {}).get(f) or "").strip()
                       for f in IDENTITY_FIELDS)
    return (not has_identity) and int(ttl) == NO_EXPIRY


def classify(entries: Optional[Iterable[Mapping[str, Any]]]) -> Dict[str, Any]:
    """Split `[{id, record, ttl}, ...]` into live workers and ghosts."""
    live: List[str] = []
    ghosts: List[Dict[str, Any]] = []
    for e in (entries or []):
        if not isinstance(e, Mapping):
            continue
        wid = str(e.get("id") or "")
        if not wid:
            continue
        rec = e.get("record") if isinstance(e.get("record"), Mapping) else {}
        try:
            ttl = int(e.get("ttl", NO_EXPIRY))
        except (TypeError, ValueError):
            ttl = NO_EXPIRY
        if is_ghost(rec, ttl):
            ghosts.append({"id": wid, "fields": sorted(rec.keys())})
        else:
            live.append(wid)
    return {"live": sorted(live), "ghosts": ghosts,
            "ghost_ids": sorted(g["id"] for g in ghosts)}


def describe(plan: Mapping[str, Any]) -> str:
    ghosts = list((plan or {}).get("ghosts") or [])
    live = list((plan or {}).get("live") or [])
    if not ghosts:
        return f"{len(live)} worker(s), no ghosts"
    ids = ", ".join(g["id"] for g in ghosts)
    return (f"{len(live)} live worker(s); {len(ghosts)} GHOST(S) with no identity "
            f"and no expiry: {ids} - a metrics write recreated the key after the "
            f"registration expired")
