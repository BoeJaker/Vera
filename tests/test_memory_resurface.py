"""Faded memories surface again on a strong match (owner, 2026-10-05: 'not get locked out forever').

HybridMemoryStore._with_resurfaced against a fake Postgres backend (the gate container imports the app).
"""
import asyncio
import importlib

import pytest


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mem():
    try:
        return importlib.import_module("Vera.vera.fabric.memory")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")


class _PG:
    def __init__(self, M, faded):
        self.M, self.faded, self.calls = M, faded, []

    async def search(self, query, limit=10, filters=None, embedding=None):
        self.calls.append(dict(filters or {}))
        return [(self.M.MemoryRecord(id=i, text="t " + i, importance=0.5), s) for i, s in self.faded]


def _store(M, faded, any_faded=True):
    st = M.HybridMemoryStore()
    st._backends = {"postgres": _PG(M, faded)}

    async def yes():
        return any_faded
    st._any_faded = yes
    return st


def test_a_strong_faded_match_joins_the_postgres_hits_marked_resurfaced():
    M = _mem()
    st = _store(M, [("old-strong", 0.3), ("old-weak", 0.05)])
    live = [(M.MemoryRecord(id="live", text="t", importance=0.5), 0.2)]
    out = _arun(st._with_resurfaced("census calendar", 5, {"exclude_record_types": ["cap_call"]},
                                    ["postgres", "neo4j"], [live, []]))
    ids = [r.id for r, _s in out[0]]
    assert ids == ["live", "old-strong"] and out[0][1][0].metadata.get("resurfaced") is True
    assert st._backends["postgres"].calls[0]["faded_only"] is True
    assert st._backends["postgres"].calls[0]["exclude_record_types"] == ["cap_call"]   # the recall exclusions still hold


def test_nothing_faded_means_no_extra_query():
    M = _mem()
    st = _store(M, [("old-strong", 0.9)], any_faded=False)
    res = [[], []]
    assert _arun(st._with_resurfaced("q", 5, {}, ["postgres", "neo4j"], res)) is res
    assert st._backends["postgres"].calls == []


def test_postgres_searches_faded_rows_only_when_asked():
    M = _mem()
    src = open(M.__file__, encoding="utf-8").read()
    assert '''conditions = (["archived = TRUE", "metadata ? 'faded_at'"] if (filters or {}).get("faded_only")''' in src
    assert "results = await self._with_resurfaced(query, limit, filters, list(tasks.keys()), results)" in src
    assert "fast_all = await self._with_resurfaced(query, limit, filters, fast_names, fast_all)" in src
