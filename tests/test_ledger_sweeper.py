"""Decisions of runs that never finished get an 'interrupted' outcome (FM-0.2a sweeper)."""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC  # noqa: E402

pytestmark = pytest.mark.critical


class FakeRedis:
    def __init__(self):
        self.h, self.stream = {}, []

    async def hsetnx(self, key, f, v):
        self.h.setdefault(key, {}).setdefault(f, v)

    async def hdel(self, key, f):
        self.h.get(key, {}).pop(f, None)

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hlen(self, key):
        return len(self.h.get(key, {}))

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        if stream == "vera:decide:ledger":
            self.stream.append(fields)


class FakeConn:
    def __init__(self, undecided):
        self.undecided = undecided

    async def execute(self, *a):
        return None

    async def fetch(self, sql, *args):
        return [{"decision_id": d} for d in self.undecided.get(args[0], [])]


class FakePool:
    def __init__(self, undecided):
        self.undecided = undecided

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return FakeConn(pool.undecided)

            async def __aexit__(self, *a):
                return False
        return _C()


@pytest.fixture
def ledger(monkeypatch):
    from vera.minds import ledger_capabilities as L
    redis = FakeRedis()
    d1, d2 = LC.new_decision_id(), LC.new_decision_id()
    monkeypatch.setattr(L._orch, "REDIS", redis, raising=False)
    monkeypatch.setattr(L._orch, "PG_POOL", FakePool({"old-run": [d1, d2]}), raising=False)
    monkeypatch.setattr(L, "_DDL_DONE", True)
    return L, redis, (d1, d2)


def test_only_runs_open_past_the_threshold_are_swept(ledger):
    L, redis, (d1, d2) = ledger
    asyncio.run(L.mark_run("old-run", True))
    redis.h[L.OPEN_RUNS]["old-run"] = "1000"
    redis.h[L.OPEN_RUNS]["live-run"] = "99000"
    res = asyncio.run(L.sweep_orphans(now=100000, older_than_s=21600))
    assert res == {"ok": True, "open": 2, "orphaned": 1, "interrupted_outcomes": 2}
    assert set(redis.h[L.OPEN_RUNS]) == {"live-run"}
    import json
    outs = [json.loads(f["row"]) for f in redis.stream]
    assert {o["decision_id"] for o in outs} == {d1, d2}
    assert all(o["label"] == "interrupted" and o["source"] == "sweeper.interrupted"
               and o["strength"] == "weak" and LC.check_outcome(o) == [] for o in outs)


def test_mark_open_then_closed(ledger):
    L, redis, _ = ledger
    asyncio.run(L.mark_run("r1", True))
    first = redis.h[L.OPEN_RUNS]["r1"]
    asyncio.run(L.mark_run("r1", True))                     # first decision's time is kept
    assert redis.h[L.OPEN_RUNS]["r1"] == first
    asyncio.run(L.mark_run("r1", False))
    assert "r1" not in redis.h[L.OPEN_RUNS]


def test_a_run_opens_on_its_first_decision_and_closes_at_the_end(monkeypatch):
    from vera.minds import loop_ledger
    marks = []

    async def fake_mark(run_id, open_):
        marks.append((run_id, open_))

    async def fake_append(row):
        return {"ok": True}
    monkeypatch.setattr(loop_ledger, "_append", lambda: fake_append)

    async def go():
        rl = loop_ledger.RunLedger("sess-9", marker=fake_mark)
        rl.controller(1, "continue", {})
        rl.controller(2, "continue", {})
        rl.finish({})
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(go())
    assert marks == [("sess-9", True), ("sess-9", False)]
