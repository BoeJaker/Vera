"""_offload_workers: ONE scan of vera:workers:* at a time (2026-10-03).

The user saw panels load slower and sometimes not at all. A py-spy profile of prod showed _offload_workers holding the
event loop for ~25% of 40 s: it runs on every capability call while a node-offload stage is on, refreshed every 3 s by
a SCAN of the whole keyspace 10 keys per round trip, and its timestamp only moved when a scan finished - so every call
during a multi-second scan started another one. Capability endpoints (/ui/panels, /health) answered in 3-6 s while
static pages answered in milliseconds.

The functions are pulled out of capability_orchestration.py by name and run against a fake Redis, so the test needs
neither the app nor a Redis.
"""
import ast
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(__file__), "..", "vera", "capability_orchestration.py")
WANT = {"_offload_workers_scan", "_offload_workers"}


def _load(redis):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    body = [n for n in tree.body if (isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in WANT)
            or (isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                getattr(t, "id", "") == "_OFFLOAD_W_TASK" for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)))]
    assert {getattr(n, "name", "") for n in body} >= WANT
    ns = {"asyncio": asyncio, "time": time, "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
          "REDIS": redis, "_OFFLOAD_CACHE": {"cfg_at": 0.0, "cfg": {}, "w_at": 0.0, "workers": []}}
    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns


class FakeRedis:
    def __init__(self, keys=3, delay=0.05, fail=False):
        self.scans = 0; self.count = None; self.keys = keys; self.delay = delay; self.fail = fail

    async def scan_iter(self, match, count=None):
        self.scans += 1; self.count = count
        for i in range(self.keys):
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("redis down")
            yield f"vera:workers:n{i}".encode()

    async def hmget(self, k, *fields):
        return [b"node-worker", b"idle", b"nlp"]


def test_many_callers_during_a_slow_scan_start_one_scan():
    r = FakeRedis(keys=4, delay=0.05)
    ns = _load(r)

    async def go():
        first = await asyncio.gather(*[ns["_offload_workers"]() for _ in range(50)])
        assert r.scans == 1, r.scans
        assert all(len(x) == 4 for x in first)          # the first callers waited for the first list
        ns["_OFFLOAD_CACHE"]["w_at"] = time.monotonic() - 10   # expire it; the list is known now
        later = await asyncio.gather(*[ns["_offload_workers"]() for _ in range(50)])
        assert all(len(x) == 4 for x in later)          # the stale list straight away, no waiting
        await ns["_OFFLOAD_W_TASK"]
        assert r.scans == 2, r.scans                    # and exactly one refresh behind it
    asyncio.run(go())


def test_a_scan_reads_many_keys_per_step():
    r = FakeRedis(keys=1, delay=0)
    ns = _load(r)
    asyncio.run(ns["_offload_workers"]())
    assert (r.count or 10) >= 100, r.count


def test_a_failed_scan_keeps_the_last_list():
    r = FakeRedis(keys=2, delay=0)
    ns = _load(r)

    async def go():
        assert len(await ns["_offload_workers"]()) == 2
        r.fail = True
        ns["_OFFLOAD_CACHE"]["w_at"] = time.monotonic() - 10
        await ns["_offload_workers"](); await ns["_OFFLOAD_W_TASK"]
        assert len(await ns["_offload_workers"]()) == 2
    asyncio.run(go())


def test_a_fresh_list_is_served_without_a_scan():
    r = FakeRedis(keys=1, delay=0)
    ns = _load(r)

    async def go():
        await ns["_offload_workers"]()
        for _ in range(100):
            await ns["_offload_workers"]()
        assert r.scans == 1
    asyncio.run(go())
