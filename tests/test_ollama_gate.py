"""Tests for the cross-process Ollama GPU gate.

Pure policy/key-shape tests run anywhere. The async acquire/release/occupancy
tests use a tiny in-memory fake that mimics the exact Redis commands the gate
relies on (SET NX PX, EVAL of the release CAS, GET) — so the concurrency
semantics (bounded slots, crash-safe TTL, owner-fenced release, fail-open) are
verified without a live Redis.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.ollama_gate import (  # noqa: E402
    gate_enabled, capacity_for, ttl_ms, wait_s, slot_key, new_owner,
    acquire, release, occupancy,
)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_gate_enabled_env_parsing():
    assert gate_enabled({"VERA_OLLAMA_GATE": "1"}) is True
    assert gate_enabled({"VERA_OLLAMA_GATE": "true"}) is True
    assert gate_enabled({"VERA_OLLAMA_GATE": "on"}) is True
    assert gate_enabled({"VERA_OLLAMA_GATE": "0"}) is False
    assert gate_enabled({}) is False


def test_capacity_gpu_vs_cpu_defaults():
    assert capacity_for(True, {}) == 1        # GPU gated to 1 by default
    assert capacity_for(False, {}) == 0       # CPU ungated by default
    assert capacity_for(True, {"VERA_GPU_GATE_N": "2"}) == 2
    assert capacity_for(False, {"VERA_NODE_GATE_N": "4"}) == 4
    # never negative
    assert capacity_for(True, {"VERA_GPU_GATE_N": "-3"}) == 0


def test_ttl_and_wait_defaults():
    assert ttl_ms({}) == 1800 * 1000
    assert wait_s({}) == 600.0
    assert ttl_ms({"VERA_GATE_TTL_S": "5"}) == 5000


def test_slot_key_and_owner_shape():
    assert slot_key("gpu-250", 0) == "vera:ollama:gate:gpu-250:slot:0"
    a, b = new_owner(), new_owner()
    assert a != b and ":" in a


# ── fake Redis implementing exactly the commands the gate uses ────────────────

class FakeRedis:
    def __init__(self):
        self.store = {}          # key -> (value_bytes, expiry_epoch or None)

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
        # only the release CAS is ever eval'd
        cur = self._live(key)
        argb = arg.encode() if isinstance(arg, str) else arg
        if cur is not None and cur == argb:
            del self.store[key]
            return 1
        return 0

    def _expire_now(self, k):
        """test helper: force-expire a key to simulate a crashed holder."""
        if k in self.store:
            val, _ = self.store[k]
            self.store[k] = (val, time.time() - 1)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── async gate semantics ─────────────────────────────────────────────────────

def test_acquire_bounds_to_capacity():
    async def go():
        r = FakeRedis()
        l1 = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        assert l1 is not None
        # capacity 1 is now full → a second acquire with wait=0 fails open (None)
        l2 = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        assert l2 is None
        # release the first, then a new one can acquire
        assert await release(r, l1) is True
        l3 = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        assert l3 is not None
    _run(go())


def test_capacity_two_allows_two_not_three():
    async def go():
        r = FakeRedis()
        a = await acquire(r, "n", 2, ttl=10_000, wait=0)
        b = await acquire(r, "n", 2, ttl=10_000, wait=0)
        c = await acquire(r, "n", 2, ttl=10_000, wait=0)
        assert a and b and c is None
        assert a["slot"] != b["slot"]
    _run(go())


def test_release_is_owner_fenced():
    async def go():
        r = FakeRedis()
        lease = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        # someone else's release (wrong owner) must NOT free the slot
        forged = {"key": lease["key"], "owner": "someone-else"}
        assert await release(r, forged) is False
        # a second acquire still blocked because the real slot is still held
        assert await acquire(r, "gpu", 1, ttl=10_000, wait=0) is None
        # real owner releases fine
        assert await release(r, lease) is True
    _run(go())


def test_crashed_holder_ttl_frees_slot():
    async def go():
        r = FakeRedis()
        lease = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        # holder "crashes" — simulate its lease TTL expiring
        r._expire_now(lease["key"])
        # the slot is now reclaimable without anyone calling release
        l2 = await acquire(r, "gpu", 1, ttl=10_000, wait=0)
        assert l2 is not None
    _run(go())


def test_ungated_and_no_redis_fail_open():
    async def go():
        r = FakeRedis()
        assert await acquire(r, "cpu", 0, ttl=10_000, wait=0) is None   # capacity 0
        assert await acquire(None, "gpu", 1, ttl=10_000, wait=0) is None  # no redis
        assert await release(None, {"key": "x", "owner": "y"}) is False
    _run(go())


def test_occupancy_reports_held_and_free():
    async def go():
        r = FakeRedis()
        await acquire(r, "gpu", 2, ttl=10_000, wait=0)
        occ = await occupancy(r, "gpu", 2)
        assert occ["capacity"] == 2 and occ["held"] == 1 and occ["free"] == 1
        assert len(occ["owners"]) == 1
    _run(go())


def test_acquire_waits_then_succeeds_when_slot_frees():
    async def go():
        r = FakeRedis()
        held = await acquire(r, "gpu", 1, ttl=10_000, wait=0)

        async def _free_soon():
            await asyncio.sleep(0.15)
            await release(r, held)

        asyncio.ensure_future(_free_soon())
        t0 = time.time()
        got = await acquire(r, "gpu", 1, ttl=10_000, wait=2.0, poll=0.05)
        assert got is not None
        assert time.time() - t0 >= 0.1   # it actually waited for the free
    _run(go())
