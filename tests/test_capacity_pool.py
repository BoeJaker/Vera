"""Tests for the generalized capacity-pool lease primitive (vera/capacity_pool.py).

Pure policy (key shape, registry parse, seat-state) runs anywhere; the async
acquire/release/occupancy use a tiny in-memory fake Redis mimicking the exact
commands used (SET NX PX, GET, EVAL release CAS) — so the bounded-slot,
crash-safe-TTL, owner-fenced-release semantics are verified without a live Redis.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import capacity_pool as cp  # noqa: E402


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_pool_slot_key_and_owner():
    assert cp.pool_slot_key("claude:acct-a", 0) == "vera:capacity:claude:acct-a:slot:0"
    assert cp.new_owner() != cp.new_owner()


def test_ttl_and_wait_defaults():
    assert cp.ttl_ms({}) == 3600 * 1000
    assert cp.wait_s({}) == 0.0
    assert cp.ttl_ms({"VERA_CAPACITY_TTL_S": "5"}) == 5000
    assert cp.wait_s({"VERA_CAPACITY_WAIT_S": "30"}) == 30.0


def test_parse_pools():
    assert cp.parse_pools({"claude:a": 1, "claude:b": "2"}) == {"claude:a": 1, "claude:b": 2}
    assert cp.parse_pools('{"claude:a": 1}') == {"claude:a": 1}
    assert cp.parse_pools("") == {}
    assert cp.parse_pools("not json") == {}
    assert cp.parse_pools({"x": -5}) == {"x": 0}      # never negative


def test_seat_state_and_cooling():
    now = 1000.0
    assert cp.seat_state({}, now) == "available"
    assert cp.seat_state({"cooling_until": now + 100}, now) == "cooling"
    assert cp.seat_state({"cooling_until": now - 100}, now) == "available"  # cooled off
    assert cp.seat_state({"expires_at": now - 1}, now) == "expired"
    assert cp.seat_state({"unauthed": True}, now) == "unauthed"
    assert cp.is_cooling({"cooling_until": now + 5}, now) is True
    assert cp.is_cooling({}, now) is False


# ── fake Redis (same shape as test_ollama_gate) ──────────────────────────────
class FakeRedis:
    def __init__(self):
        self.store = {}

    def _live(self, k):
        v = self.store.get(k)
        if v is None:
            return None
        val, exp = v
        if exp is not None and time.time() >= exp:
            del self.store[k]
            return None
        return val

    async def set(self, k, v, nx=False, px=None):
        vb = v.encode() if isinstance(v, str) else v
        if nx and self._live(k) is not None:
            return None
        exp = (time.time() + px / 1000.0) if px else None
        self.store[k] = (vb, exp)
        return True

    async def get(self, k):
        return self._live(k)

    async def eval(self, script, numkeys, key, arg):
        cur = self._live(key)
        argb = arg.encode() if isinstance(arg, str) else arg
        if cur is not None and cur == argb:
            del self.store[key]
            return 1
        return 0

    def _expire_now(self, k):
        if k in self.store:
            val, _ = self.store[k]
            self.store[k] = (val, time.time() - 1)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── async lease semantics ────────────────────────────────────────────────────
def test_bounded_slots_one_seat():
    r = FakeRedis()

    async def go():
        a = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)
        assert a and a["pool"] == "claude:acct-a"
        # second acquire on a 1-slot pool → None (queue the work, don't block)
        b = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)
        assert b is None
        # release, then it's free again
        assert await cp.release(r, a) is True
        c = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)
        assert c is not None
    _run(go())


def test_uncapped_and_no_redis_fail_open():
    r = FakeRedis()
    assert _run(cp.acquire(r, "p", 0, 1000, 0)) is None   # 0 slots → unslotted
    assert _run(cp.acquire(None, "p", 1, 1000, 0)) is None  # no redis → unslotted


def test_release_is_owner_fenced():
    r = FakeRedis()

    async def go():
        a = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)
        # a different owner cannot release our slot
        assert await cp.release(r, {"key": a["key"], "owner": "someone-else"}) is False
        assert await cp.release(r, a) is True
    _run(go())


def test_crashed_holder_slot_auto_expires():
    r = FakeRedis()

    async def go():
        a = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)
        r._expire_now(a["key"])                     # holder "died"
        b = await cp.acquire(r, "claude:acct-a", 1, 10000, 0)  # lease auto-freed
        assert b is not None
    _run(go())


def test_occupancy():
    r = FakeRedis()

    async def go():
        await cp.acquire(r, "claude:acct-a", 2, 10000, 0)
        occ = await cp.occupancy(r, "claude:acct-a", 2)
        assert occ["capacity"] == 2 and occ["held"] == 1 and occ["free"] == 1
    _run(go())
