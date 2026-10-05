"""Hourly four-minds baseline snapshots survive restarts (FM-0.4)."""
import asyncio
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical


class _Win:
    def __init__(self, s):
        self.s = s

    def summary(self):
        return self.s


class _Conn:
    def __init__(self, store):
        self.store = store

    async def execute(self, sql, *args):
        if sql.startswith("INSERT"):
            self.store.append((args[0], json.loads(args[1])))

    async def fetch(self, sql, *args):
        return [{"question_id": "loop.step_verify", "origin": "census", "decisions": 3, "with_outcome": 1}]


class _Pool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return _Conn(pool.store)

            async def __aexit__(self, *a):
                return False
        return _C()


def test_snapshot_stores_each_window_and_the_decision_counts(monkeypatch):
    from vera.minds import baseline_capabilities as B
    store = []
    monkeypatch.setattr(B._orch, "PG_POOL", _Pool(store), raising=False)
    monkeypatch.setattr(B, "_DDL_DONE", True)
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory",
                        types.SimpleNamespace(RETRIEVAL_TIMING=_Win({"hybrid.fast": {"calls": 2}})))
    monkeypatch.setitem(sys.modules, "Vera.vera.agents.agents",
                        types.SimpleNamespace(CHAT_TURNS=_Win({"vera|plain": {"calls": 5}})))
    res = asyncio.run(B.snapshot_once())
    assert res == {"ok": True, "stored": ["retrieval", "chat", "decisions"]}
    kinds = {k: d for k, d in store}
    assert kinds["retrieval"] == {"hybrid.fast": {"calls": 2}}
    assert kinds["decisions"]["rows"][0]["origin"] == "census"


def test_empty_windows_are_not_stored(monkeypatch):
    from vera.minds import baseline_capabilities as B
    store = []
    monkeypatch.setattr(B._orch, "PG_POOL", _Pool(store), raising=False)
    monkeypatch.setattr(B, "_DDL_DONE", True)
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory", types.SimpleNamespace(RETRIEVAL_TIMING=_Win({})))
    monkeypatch.setitem(sys.modules, "Vera.vera.agents.agents", types.SimpleNamespace())
    res = asyncio.run(B.snapshot_once())
    assert res["stored"] == ["decisions"]


def test_no_postgres_no_error(monkeypatch):
    from vera.minds import baseline_capabilities as B
    monkeypatch.setattr(B._orch, "PG_POOL", None, raising=False)
    assert asyncio.run(B.snapshot_once())["ok"] is False


def test_registered_in_the_module_list():
    src = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert '"minds/baseline_capabilities.py"' in src
