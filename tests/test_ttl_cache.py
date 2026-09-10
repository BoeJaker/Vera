"""A polled endpoint that costs 30 s must not be recomputed per poll, and
concurrent pollers must share one computation (vera/evolve/ttl_cache.py).

Measured 2026-09-10: /evolve/authors took 20-35 s per call and was polled
every 4-20 s, so calls overlapped, filled the browser's connection pool, and
the census table appeared 36 s after the click with no single request slow.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve.ttl_cache import TTLCache  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_within_ttl_the_value_is_reused():
    clock = [100.0]
    c = TTLCache(60, clock=lambda: clock[0])
    calls = []

    async def compute():
        calls.append(1)
        return {"n": len(calls)}

    assert run(c.get("k", compute)) == {"n": 1}
    clock[0] += 30
    assert run(c.get("k", compute)) == {"n": 1}
    assert len(calls) == 1
    clock[0] += 31
    assert run(c.get("k", compute)) == {"n": 2}
    assert c.stats()["hits"] == 1 and c.stats()["misses"] == 2


def test_concurrent_callers_share_one_computation():
    c = TTLCache(60)
    started = []

    async def compute():
        started.append(1)
        await asyncio.sleep(0.05)
        return "v"

    async def main():
        return await asyncio.gather(*(c.get("k", compute) for _ in range(8)))

    assert run(main()) == ["v"] * 8
    assert len(started) == 1
    assert c.stats()["coalesced"] == 7


def test_fresh_bypasses_and_refills():
    c = TTLCache(60)
    n = [0]

    async def compute():
        n[0] += 1
        return n[0]

    assert run(c.get("k", compute)) == 1
    assert run(c.get("k", compute, fresh=True)) == 2
    assert run(c.get("k", compute)) == 2


def test_keys_are_independent_and_bounded():
    c = TTLCache(60, max_entries=2)

    async def mk(v):
        async def compute():
            return v
        return compute

    for k in ("a", "b", "c"):
        run(c.get(k, run(mk(k))))
    assert c.stats()["entries"] == 2
    assert c.peek("a") is None and c.peek("c") == "c"
    c.invalidate()
    assert c.stats()["entries"] == 0
