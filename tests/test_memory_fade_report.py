"""The fading job's dry run (memory.fade.report; context-and-ui CU-M.4, owner Q2: soft-archive, dry run first).

Nothing is changed: the report says what would fade, by class, and why. Runs the real capability against a fake pool.
"""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import pytest


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _hooks():
    try:
        return importlib.import_module("Vera.vera.fabric.memory_hooks")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")


NOW = datetime.now(timezone.utc)
ROWS = [
    {"id": "a-fresh", "record_type": "message", "source_type": "", "category": "", "importance": 0.5,
     "created_at": NOW - timedelta(days=1), "head": "hello"},
    {"id": "b-old", "record_type": "message", "source_type": "", "category": "", "importance": 0.5,
     "created_at": NOW - timedelta(days=400), "head": "old chat"},
    {"id": "c-tool", "record_type": "cap_call", "source_type": "", "category": "", "importance": 0.4,
     "created_at": NOW - timedelta(days=30), "head": "[board.items]"},
    {"id": "d-used", "record_type": "message", "source_type": "", "category": "", "importance": 0.5,
     "created_at": NOW - timedelta(days=400), "head": "the plan"},
]


class _Conn:
    def __init__(self):
        self.writes = []

    async def fetchval(self, sql, *a):
        return True

    async def fetch(self, sql, *a):
        if "FROM vera_memory_usage" in sql:
            return [{"record_id": "d-used", "injected_n": 2, "used_n": 2, "last_used": NOW - timedelta(days=1)}]
        if "id > $1" in sql:
            last, size = a
            return [r for r in ROWS if r["id"] > last][:size]
        if "ANY(" in sql:
            return [{"id": i, "t": "text of " + i} for i in a[0]]
        return []

    async def execute(self, *a):
        self.writes.append(a)


class _Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        c = self.c

        class _Ctx:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return _Ctx()


def test_the_dry_run_counts_what_would_fade_and_changes_nothing(monkeypatch):
    H = _hooks()
    conn = _Conn()

    async def pool():
        return _Pool(conn)
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_redis", lambda: None)
    out = _arun(H._fade_compute(5, 500))
    assert out["dry_run"] is True and out["total"] == 4
    conv, tool = out["by_class"]["conversation"], out["by_class"]["tool"]
    assert conv["total"] == 3 and conv["fading"] == 1                  # only the old, unused chat line
    assert [s["id"] for s in conv["samples"]] == ["b-old"] and conv["samples"][0]["text"] == "text of b-old"
    assert tool["fading"] == 1                                          # a month-old tool call is long past 3 days
    assert out["fading"] == 2 and out["usage_rows"] == 1
    assert conn.writes == []                                            # a dry run writes nothing to the store


def test_the_report_names_the_policy_in_force(monkeypatch):
    H = _hooks()

    async def pool():
        return _Pool(_Conn())
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_redis", lambda: None)
    assert _arun(H._fade_compute())["policy"] == H._strength_core.POLICY_VERSION


def test_the_capability_answers_at_once_and_runs_one_scan_in_the_background(monkeypatch):
    # the whole-store scan took past the 60 s a request is given on prod (2026-10-05): it runs as a task instead
    H = _hooks()
    started = []

    async def slow_compute(samples=5, batch=5000):
        started.append(1)
        await asyncio.sleep(0.3)
        return {}
    monkeypatch.setattr(H, "_fade_compute", slow_compute)
    monkeypatch.setattr(H, "_redis", lambda: None)
    monkeypatch.setattr(H, "_FADE_TASK", None)
    fn = getattr(H.memory_fade_report, "__wrapped__", H.memory_fade_report)

    async def go():
        a = await fn()
        b = await fn(refresh=True)          # one already running: no second scan
        await asyncio.sleep(0.4)
        return a, b
    a, b = _arun(go())
    assert a == {"report": None, "computing": True} and b["computing"] is True
    assert started == [1]
