"""Freezing a container must not freeze the GPU with it.

Observed live 2026-08-30, and it cost a census goal. The harness logged
`waiting: gate held by ['43ed456d3225:1:b5b09007']`; 43ed456d3225 is the
container `vera-dev`, which docker reported as `Up 4 days (Paused)`. The loop
then burned its entire 25-minute wall cap and recorded
`planned=0 executed=0 inserted=0 cycles={}` - it never got a generation slot.

A paused container is SIGSTOPped: it cannot renew its lease, cannot release it,
and cannot use it. But the lease still looks LIVE - unexpired and owned - so
sweep_dead_local_leases correctly refuses to reclaim it, because it cannot verify
a peer's liveness and clearing a live peer slot would double-book the GPU. A
frozen owner is exactly the case that rule cannot distinguish.

The gate is capacity 1, so one paused holder starves prod, every sandbox and the
census at once. And the idle-reap AUTO-PAUSES idle containers, so
"acquire a lease -> go idle -> get paused mid-lease" is a normal path, not an
exotic one.

Pure: no Redis server, no app import - a tiny fake client stands in.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.ollama_gate import (          # noqa: E402
    is_lease_of_host, is_reapable_local_lease, release_leases_for_host, slot_key,
)


class FakeRedis:
    """Just enough: scan_iter, get, and the owner-fenced release eval."""

    def __init__(self, slots):
        self.slots = dict(slots)
        self.evals = 0

    async def scan_iter(self, match=None):
        for k in list(self.slots):
            yield k

    async def get(self, k):
        v = self.slots.get(k)
        return v.encode() if isinstance(v, str) else v

    async def eval(self, _lua, _n, key, owner, *a):
        self.evals += 1
        # Owner-fenced delete, exactly as the real _RELEASE_LUA does.
        if self.slots.get(key) == owner:
            del self.slots[key]
            return 1
        return 0


def _run(coro):
    return asyncio.run(coro)


HOST = "43ed456d3225"          # the paused container from the incident
PEER = "9f0011aabbcc"


# ── the predicate ──────────────────────────────────────────────────────────
def test_a_lease_is_matched_to_the_host_that_minted_it():
    assert is_lease_of_host(f"{HOST}:1:b5b09007", HOST) is True
    assert is_lease_of_host(f"{PEER}:1:b5b09007", HOST) is False


def test_blank_owner_or_host_matches_nothing():
    assert is_lease_of_host("", HOST) is False
    assert is_lease_of_host(f"{HOST}:1:x", "") is False
    assert is_lease_of_host(None, HOST) is False


def test_an_odd_owner_shape_does_not_match_by_accident():
    """parse_owner is tolerant of junk; that tolerance must not become a match."""
    assert is_lease_of_host("garbage", HOST) is False
    assert is_lease_of_host(f"{HOST}", HOST) is True      # host with no pid segment
    assert is_lease_of_host(f"{HOST}:notapid:x", HOST) is True


# ── the release ────────────────────────────────────────────────────────────
def test_the_paused_container_hands_its_slot_back():
    r = FakeRedis({slot_key("gpu-250", 0): f"{HOST}:1:b5b09007"})
    res = _run(release_leases_for_host(r, HOST))
    assert res["count"] == 1
    assert r.slots == {}, "the slot must be free for the next caller"


def test_only_that_container_s_leases_are_touched():
    """The gate is shared. Releasing a peer's live slot would double-book the GPU
    - the very thing sweep_dead_local_leases exists to avoid."""
    keys = {slot_key("gpu-250", 0): f"{HOST}:1:aa",
            slot_key("gpu-250", 1): f"{PEER}:7:bb",
            slot_key("cpu-246", 0): f"{PEER}:9:cc"}
    r = FakeRedis(keys)
    res = _run(release_leases_for_host(r, HOST))
    assert res["count"] == 1
    assert set(r.slots.values()) == {f"{PEER}:7:bb", f"{PEER}:9:cc"}


def test_releasing_is_owner_fenced():
    """If the slot changed hands between the scan and the delete, do not clobber
    the new owner."""
    r = FakeRedis({slot_key("gpu-250", 0): f"{HOST}:1:aa"})
    original_get = r.get

    async def racing_get(k):
        v = await original_get(k)
        r.slots[k] = f"{PEER}:2:bb"        # someone else takes it after we look
        return v
    r.get = racing_get
    res = _run(release_leases_for_host(r, HOST))
    assert res["count"] == 0
    assert r.slots[slot_key("gpu-250", 0)] == f"{PEER}:2:bb"


def test_nothing_held_is_a_no_op():
    r = FakeRedis({slot_key("gpu-250", 0): f"{PEER}:1:aa"})
    assert _run(release_leases_for_host(r, HOST))["count"] == 0
    assert len(r.slots) == 1


def test_no_redis_or_no_host_is_a_safe_no_op():
    assert _run(release_leases_for_host(None, HOST))["count"] == 0
    assert _run(release_leases_for_host(FakeRedis({}), ""))["count"] == 0


def test_a_redis_failure_never_raises():
    """Failing to release costs the old behaviour; failing to PAUSE would leave
    an idle container running. The pause must always win."""
    class Broken(FakeRedis):
        async def scan_iter(self, match=None):
            raise RuntimeError("redis down")
            yield  # pragma: no cover
    res = _run(release_leases_for_host(Broken({}), HOST))
    assert res["count"] == 0 and "error" in res


# ── the existing safety rule must be untouched ─────────────────────────────
def test_the_dead_local_pid_rule_still_refuses_peer_leases():
    """This fix adds a NARROWER question with its own justification (we are about
    to freeze the owner). It must not weaken the rule that a peer's lease is
    never cleared on a liveness guess."""
    assert is_reapable_local_lease(f"{PEER}:1:aa", HOST, lambda _p: False) is False
    assert is_reapable_local_lease(f"{HOST}:1:aa", HOST, lambda _p: True) is False
    assert is_reapable_local_lease(f"{HOST}:1:aa", HOST, lambda _p: False) is True
