"""DEEP-21 D21-12 (compute-roles, 12 Oct 2026): run records are patched under WATCH/MULTI, so an LPUSH
between the find and the set can no longer make the LSET overwrite the neighbouring record."""
import asyncio
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import redis_list_patch as P  # noqa: E402

pytestmark = pytest.mark.critical


class FakeRedis:
    """Just enough of redis.asyncio for WATCH/MULTI on one list; `on_watch` runs a racing LPUSH once."""

    def __init__(self, items, on_watch=None):
        self.items = [json.dumps(i) for i in items]
        self.version = 0
        self.on_watch = on_watch

    def pipeline(self, transaction=True):
        return FakePipe(self)


class FakePipe:
    def __init__(self, r):
        self.r, self.ops, self.watched = r, [], None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def watch(self, key):
        self.watched = self.r.version
        if self.r.on_watch:
            fn, self.r.on_watch = self.r.on_watch, None
            fn(self.r)

    async def lrange(self, key, a, b):
        return list(self.r.items[a:b + 1])

    async def unwatch(self):
        self.watched = None

    def multi(self):
        pass

    def lset(self, key, i, v):
        self.ops.append((i, v))

    async def execute(self):
        if self.watched != self.r.version:
            raise P.WatchError("changed")
        for i, v in self.ops:
            self.r.items[i] = v
        self.r.version += 1


def _push(r):                         # another writer LPUSHes after our WATCH, before our LRANGE
    r.items.insert(0, json.dumps({"id": "new"}))
    r.version += 1


def test_patch_survives_a_racing_lpush():
    r = FakeRedis([{"id": "a", "x": 1}, {"id": "b", "x": 2}], on_watch=_push)
    ok = asyncio.run(P.patch_list_item(r, "k", lambda d: d["id"] == "b", lambda d: dict(d, x=99)))
    vals = [json.loads(v) for v in r.items]
    assert ok and vals == [{"id": "new"}, {"id": "a", "x": 1}, {"id": "b", "x": 99}]


def test_no_match_writes_nothing():
    r = FakeRedis([{"id": "a"}])
    assert asyncio.run(P.patch_list_item(r, "k", lambda d: d["id"] == "z", lambda d: d)) is False
    assert json.loads(r.items[0]) == {"id": "a"}


def test_both_run_lists_use_it():
    for f in ("vera/evolve/schedule_capabilities.py", "vera/evolve/evolve_capabilities.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "patch_list_item(r, KEY_RUNS," in src, f
