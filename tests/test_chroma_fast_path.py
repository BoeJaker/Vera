"""Chroma memory search without server-side filters (2026-10-05).

Any `where` made Chroma build an allowed-id set over the whole collection first (live:
10-12 ms unfiltered vs 2.4-3.7 s with archived=false alone, 212k vectors). The default
exclusions are applied after an over-fetch; a caller's own filters still go to the server.
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory  # noqa: E402

pytestmark = pytest.mark.critical


class FakeCollection:
    """Rows: (id, doc, meta, distance), nearest first. Records every query's kwargs."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def count(self):
        return len(self.rows)

    def query(self, **kw):
        self.calls.append(kw)
        rows = self.rows
        where = kw.get("where")
        if where:   # the server path: apply archived + $nin + $eq like Chroma would
            def ok(meta):
                clauses = where.get("$and", [where])
                for c in clauses:
                    (k, cond), = c.items()
                    if "$eq" in cond and meta.get(k) != cond["$eq"]:
                        return False
                    if "$nin" in cond and meta.get(k) in cond["$nin"]:
                        return False
                return True
            rows = [r for r in rows if ok(r[2])]
        rows = rows[:kw["n_results"]]
        return {"ids": [[r[0] for r in rows]], "documents": [[r[1] for r in rows]],
                "metadatas": [[r[2] for r in rows]], "distances": [[r[3] for r in rows]]}


def _row(i, rtype="message", archived=False, doc=None):
    return (f"r{i}", doc or f"text {i}", {"record_type": rtype, "archived": archived, "importance": 0.5},
            0.1 + i * 0.01)


def _backend(rows):
    b = memory.ChromaBackend()
    b._collection = FakeCollection(rows)
    b._count_cache = (1e18, len(rows))     # fresh: no count call
    return b


DEFAULT = {"exclude_record_types": ["cap_call", "dag_step"], "exclude_transcripts": True}


def test_default_exclusions_need_no_server_filter():
    rows = [_row(0, "cap_call"), _row(1), _row(2, archived=True), _row(3, doc="[Claude Code] x"), _row(4), _row(5)]
    b = _backend(rows)
    out = asyncio.run(b.search("q", limit=2, filters=dict(DEFAULT), embedding=[0.1]))
    assert [r.id for r, _ in out] == ["r1", "r4"]
    assert len(b._collection.calls) == 1 and "where" not in b._collection.calls[0]


def test_falls_back_to_the_filtered_query_when_too_few_survive():
    rows = [_row(i, "cap_call") for i in range(20)] + [_row(20 + i) for i in range(3)]
    b = _backend(rows)
    before = dict(memory.CHROMA_PATHS)
    out = asyncio.run(b.search("q", limit=3, filters=dict(DEFAULT), embedding=[0.1]))
    assert [r.id for r, _ in out] == ["r20", "r21", "r22"]
    assert len(b._collection.calls) == 2 and "where" in b._collection.calls[1]
    assert memory.CHROMA_PATHS["fallback"] == before["fallback"] + 1


def test_a_small_collection_is_answered_whole_without_fallback():
    rows = [_row(0, "cap_call"), _row(1)]
    b = _backend(rows)
    out = asyncio.run(b.search("q", limit=5, filters=dict(DEFAULT), embedding=[0.1]))
    assert [r.id for r, _ in out] == ["r1"] and len(b._collection.calls) == 1


def test_a_callers_own_filter_still_goes_to_the_server():
    rows = [_row(0), _row(1)]
    rows[1][2]["session_id"] = "s1"
    rows[0][2]["session_id"] = "s0"
    b = _backend(rows)
    out = asyncio.run(b.search("q", limit=5, filters={"session_id": "s1", **DEFAULT}, embedding=[0.1]))
    assert [r.id for r, _ in out] == ["r1"]
    assert "where" in b._collection.calls[0]


def test_no_vector_no_query():
    b = _backend([_row(0)])
    assert asyncio.run(b.search("q", limit=5, filters=dict(DEFAULT), embedding=None)) == []
    assert b._collection.calls == []
