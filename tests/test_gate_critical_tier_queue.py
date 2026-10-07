"""Gates run the critical tier one at a time.

Two tiers side by side were each killed at the 900 s limit (2026-09-29) while
the same tier alone took 335-551 s. _critical_tier_queued holds a Redis lease
(plus an in-process lock) around evolve.unittest.run; a gate that has to wait
says so in its steps, and the wait is not part of its 900 s.
"""
import asyncio

import pytest

from vera.evolve import evolve_capabilities as evolve


pytestmark = pytest.mark.critical


class FakeRedis:
    def __init__(self):
        self.kv = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value.encode() if isinstance(value, str) else value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def eval(self, _script, _nkeys, key, token):
        if self.kv.get(key) == token.encode():
            del self.kv[key]
            return 1
        return 0


def _patch(monkeypatch, redis, running, peak, timeouts):
    async def unittest_run(branch, paths, markers, timeout, pipeline_id):
        running.append(branch)
        peak.append(len(running))
        timeouts.append(timeout)
        await asyncio.sleep(0.05)
        running.remove(branch)
        return {"ok": True, "summary": f"{branch} PASS"}

    async def save(_rec):
        return None

    monkeypatch.setattr(evolve, "evolve_unittest_run", unittest_run)
    monkeypatch.setattr(evolve, "_save_pipeline", save)
    monkeypatch.setattr(evolve, "_redis", lambda: redis)
    monkeypatch.setattr(evolve, "_CRITICAL_TIER_POLL_S", 0.01)
    monkeypatch.setattr(evolve, "_critical_tier_lock", None)


def test_two_gates_never_run_the_tier_together(monkeypatch):
    redis, running, peak, timeouts = FakeRedis(), [], [], []
    _patch(monkeypatch, redis, running, peak, timeouts)
    a, b = {"id": "p-a"}, {"id": "p-b"}

    async def both():
        return await asyncio.gather(evolve._critical_tier_queued(a, "feat/a"),
                                    evolve._critical_tier_queued(b, "feat/b"))

    ra, rb = asyncio.run(both())
    assert ra["ok"] and rb["ok"]
    assert max(peak) == 1
    assert timeouts == [evolve._CRITICAL_TIER_TIMEOUT_S] * 2
    assert redis.kv == {}, "the lease must be released after each run"
    waited = [r for r in (a, b) if any(s["stage"] == "critical-queue" for s in r.get("steps", []))]
    assert len(waited) == 1, "exactly one gate waited, and its steps say so"


def test_a_lease_held_elsewhere_makes_the_gate_wait_and_name_the_holder(monkeypatch):
    redis, running, peak, timeouts = FakeRedis(), [], [], []
    _patch(monkeypatch, redis, running, peak, timeouts)
    redis.kv[evolve._CRITICAL_TIER_LEASE_KEY] = b"p-other:feat/other"
    rec = {"id": "p-me"}

    async def run():
        task = asyncio.ensure_future(evolve._critical_tier_queued(rec, "feat/me"))
        await asyncio.sleep(0.05)
        assert not task.done() and not running, "must not start while another holds the lease"
        del redis.kv[evolve._CRITICAL_TIER_LEASE_KEY]
        return await task

    assert asyncio.run(run())["ok"]
    details = " ".join(s["detail"] for s in rec["steps"])
    assert "p-other:feat/other" in details
    assert "gate slot free after" in details


def test_no_redis_still_runs_the_tier(monkeypatch):
    running, peak, timeouts = [], [], []
    _patch(monkeypatch, None, running, peak, timeouts)
    assert asyncio.run(evolve._critical_tier_queued({"id": "p"}, "feat/x"))["ok"]


def test_the_lease_outlives_one_run():
    assert evolve._CRITICAL_TIER_LEASE_S > evolve._CRITICAL_TIER_TIMEOUT_S
