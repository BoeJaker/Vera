"""Gates start oldest pipeline first, across processes (4 Oct 2026, user: 'make sure the gate
queue is processing the oldest items first and not the newest added')."""
import asyncio
import time

import pytest

from vera.evolve import evolve_capabilities as evolve
from vera.evolve import gate_queue_core as Q

pytestmark = pytest.mark.critical


def test_ahead_skips_stale_and_stops_at_self():
    now = 1000.0
    line = [(b"old:a", 1.0), (b"dead:b", 2.0), (b"me:c", 3.0), (b"newer:d", 4.0)]
    hb = {"old:a": "995", "dead:b": "100", "me:c": "999", "newer:d": "999"}
    alive, stale = Q.ahead(line, hb, "me:c", now)
    assert alive == ["old:a"] and stale == ["dead:b"]
    assert Q.ahead(line, hb, "old:a", now) == ([], [])
    assert Q.score("2026-10-04T17:00:00+00:00", 5.0) == 1791133200.0
    assert Q.score("garbage", 5.0) == 5.0


class FakeRedis:
    def __init__(self):
        self.kv, self.z, self.h = {}, {}, {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value.encode() if isinstance(value, str) else value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def eval(self, _s, _n, key, token):
        if self.kv.get(key) == token.encode():
            del self.kv[key]
            return 1
        return 0

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zrange(self, key, a, b, withscores=False):
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1])[a:b + 1]
        return [(k.encode(), s) for k, s in items] if withscores else [k.encode() for k, _ in items]

    async def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def hmget(self, key, fields):
        return [self.h.get(key, {}).get(f) for f in fields]

    async def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)


def _patch(monkeypatch, redis, order, pool=None):
    async def unittest_run(branch, paths, markers, timeout, pipeline_id):
        order.append(branch)
        await asyncio.sleep(0.02)
        return {"ok": True, "summary": "PASS"}

    async def save(_rec):
        return None

    monkeypatch.setattr(evolve, "evolve_unittest_run", unittest_run)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_CRITICAL_TIER_POLL_S", 0.01)
    monkeypatch.setattr(evolve, "_critical_tier_lock", None)
    monkeypatch.setattr(evolve._orch, "TEST_POOL", pool, raising=False)


def test_the_older_gate_starts_first_even_when_it_arrives_second(monkeypatch):
    redis, order = FakeRedis(), []
    _patch(monkeypatch, redis, order)
    redis.kv[evolve._CRITICAL_TIER_LEASE_KEY] = b"other:fix/other"      # a gate running elsewhere
    newer = {"id": "new", "created_at": "2026-10-04T17:30:00+00:00"}
    older = {"id": "old", "created_at": "2026-10-04T16:00:00+00:00"}

    async def go():
        tn = asyncio.ensure_future(evolve._critical_tier_queued(newer, "fix/new"))
        await asyncio.sleep(0.03)
        to = asyncio.ensure_future(evolve._critical_tier_queued(older, "fix/old"))
        await asyncio.sleep(0.05)
        assert order == []
        del redis.kv[evolve._CRITICAL_TIER_LEASE_KEY]                   # the slot frees
        return await asyncio.gather(tn, to)

    asyncio.run(go())
    assert order == ["fix/old", "fix/new"]
    assert redis.z.get(Q.QUEUE_KEY, {}) == {} and redis.h.get(Q.HEARTBEAT_KEY, {}) == {}
    assert any(s["stage"] == "critical-queue" for s in newer["steps"])   # it waited, and said so


def test_the_pool_is_fed_oldest_first(monkeypatch):
    redis, order, fed = FakeRedis(), [], []

    async def pool(rec, branch, markers, timeout):
        fed.append(branch)
        await asyncio.sleep(0.01)
        return {"ok": True, "summary": "pooled"}

    _patch(monkeypatch, redis, order, pool=pool)
    # an older gate from another process is waiting, alive
    redis.z[Q.QUEUE_KEY] = {"blocker:fix/b": 1.0}
    redis.h[Q.HEARTBEAT_KEY] = {"blocker:fix/b": str(time.time() + 3600)}
    recs = [{"id": f"p{i}", "created_at": f"2026-10-04T1{i}:00:00+00:00"} for i in (7, 5, 6)]

    async def go():
        tasks = []
        for r in recs:                                    # arrive newest-first-ish
            tasks.append(asyncio.ensure_future(evolve._critical_tier_queued(r, "fix/" + r["id"])))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert fed == []
        redis.z[Q.QUEUE_KEY].pop("blocker:fix/b")        # the blocker starts
        return await asyncio.gather(*tasks)

    asyncio.run(go())
    assert fed == ["fix/p5", "fix/p6", "fix/p7"]


def test_a_dead_waiter_does_not_block_the_line(monkeypatch):
    redis, order = FakeRedis(), []
    _patch(monkeypatch, redis, order)
    redis.z[Q.QUEUE_KEY] = {"dead:fix/x": 1.0}
    redis.h[Q.HEARTBEAT_KEY] = {"dead:fix/x": "1.0"}                   # heartbeat long gone
    assert asyncio.run(evolve._critical_tier_queued({"id": "me", "created_at": "2026-10-04T18:00:00+00:00"},
                                                    "fix/me"))["ok"]
    assert order == ["fix/me"] and "dead:fix/x" not in redis.z[Q.QUEUE_KEY]
