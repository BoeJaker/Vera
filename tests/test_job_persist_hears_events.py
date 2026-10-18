"""S3 / D15-2: job persistence hears the events, within bounds.

The listener subscribed to vera:events - the stream key - while emit_event publishes on the vera:events:live
channel, so job history, the Ollama log and the Postgres archive were never written. Now it hears them, records
worker tasks and Ollama requests, records capability calls only for opted-in groups (each would otherwise be a Redis
hash and an archive row), keeps the full prompt out of the permanent archive, and trims the Ollama log oldest first.
Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.workers.job_persistance as jp  # noqa: E402  (fails loudly, never skips)


class FakePubSub:
    def __init__(self, events):
        self.events, self.channels = events, []

    async def subscribe(self, *ch):
        self.channels += list(ch)

    async def listen(self):
        yield {"type": "subscribe", "data": 1}
        for e in self.events:
            yield {"type": "message", "data": json.dumps(e)}


class FakeRedis:
    def __init__(self, events=()):
        self.h, self.z, self.ps = {}, {}, FakePubSub(list(events))

    def pubsub(self):
        return self.ps

    async def hgetall(self, k):
        return {kk.encode(): (v.encode() if isinstance(v, str) else v) for kk, v in self.h.get(k, {}).items()}

    async def hset(self, k, f=None, v=None, mapping=None):
        h = self.h.setdefault(k, {})
        if mapping:
            h.update(mapping)
        else:
            h[f] = v

    async def hget(self, k, f):
        v = self.h.get(k, {}).get(f)
        return v.encode() if isinstance(v, str) else v

    async def expire(self, *a):
        return True

    async def zadd(self, k, m):
        self.z.setdefault(k, {}).update(m)

    async def hincrby(self, k, f, n):
        self.h.setdefault(k, {})[f] = int(self.h.get(k, {}).get(f, 0)) + n

    async def hlen(self, k):
        return len(self.h.get(k, {}))

    async def hdel(self, k, *fs):
        for f in fs:
            self.h.get(k, {}).pop(f.decode() if isinstance(f, bytes) else f, None)

    async def zremrangebyscore(self, *a):
        return 0

    async def zcard(self, k):
        return len(self.z.get(k, {}))


class FakePool:
    def __init__(self):
        self.rows = []
        pool = self

        class _C:
            async def execute(self, sql, *args):
                pool.rows.append(args)

        class _Ctx:
            async def __aenter__(self):
                return _C()

            async def __aexit__(self, *a):
                return False
        self._ctx = _Ctx

    def acquire(self):
        return self._ctx()


@pytest.fixture
def world(monkeypatch):
    def make(events, groups=()):
        r, pool = FakeRedis(events), FakePool()
        monkeypatch.setattr(jp._orch, "REDIS", r)
        monkeypatch.setattr(jp._orch, "PG_POOL", pool)
        monkeypatch.setattr(jp, "ARCHIVE_TO_PG", True)
        monkeypatch.setattr(jp, "CAP_GROUPS", set(groups))
        asyncio.run(jp._event_listener())
        return r, pool
    return make


EVENTS = [{"type": "worker.start", "task": "t1", "capability": "docker.run", "worker": "w1"},
          {"type": "worker.done", "task": "t1", "worker": "w1"},
          {"type": "cap.call", "name": "obs.events", "group": "obs", "trace_id": "c1"},
          {"type": "cap.ok", "name": "obs.events", "group": "obs", "trace_id": "c1"}]


def test_it_listens_on_the_channel_events_are_published_on(world):
    r, _ = world([])
    assert r.ps.channels == ["vera:events:live"]


def test_worker_tasks_are_recorded_and_cap_calls_are_not_by_default(world):
    r, pool = world(EVENTS)
    assert r.h["vera:jobs:t1"]["status"] == "done" and r.h["vera:jobs:t1"]["capability"] == "docker.run"
    assert "vera:jobs:c1" not in r.h                                    # no hash per capability call
    assert [a[0] for a in pool.rows] == ["t1"]                          # one archive row: the worker task


def test_an_opted_in_group_records_its_cap_calls(world):
    r, pool = world(EVENTS, groups=("obs",))
    assert r.h["vera:jobs:c1"]["status"] == "done"
    assert sorted(a[0] for a in pool.rows) == ["c1", "t1"]


def test_the_archive_never_holds_the_full_prompt(world):
    r, pool = world([{"type": "ollama.request", "req_id": "o1", "model": "m", "prompt_preview": "short",
                      "prompt_full": "SECRET whole prompt"},
                     {"type": "ollama.request_done", "req_id": "o1", "elapsed_s": 1.0}])
    assert r.h["vera:jobs:o1"]["prompt_full"] == "SECRET whole prompt"     # the 7-day record keeps it for the panel
    archived = [json.loads(a[1]) for a in pool.rows if a[0] == "o1"]
    assert archived and "prompt_full" not in archived[0] and archived[0]["prompt_preview"] == "short"


def test_the_ollama_log_trims_oldest_first():
    entries = {f"r{i}".encode(): json.dumps({"ts": float(i)}).encode() for i in range(10)}
    entries[b"junk"] = b"{not json"
    out = jp._oldest_ollama_keys(entries, 3)
    assert set(out[:2]) == {b"junk", b"r0"} and out[2] == b"r1"     # junk ties with ts 0 (counts as oldest)
    assert jp._oldest_ollama_keys(entries, 0) == []
