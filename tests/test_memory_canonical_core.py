"""One canonical record per repeated text (owner 2026-10-04; 30% of the recall pool were copies, 2026-10-06)."""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.fabric import memory_canonical_core as C  # noqa: E402


def test_the_key_ignores_case_and_spacing_only():
    assert C.text_key("The current bash user is boejaker.") == C.text_key("  the current   BASH user is\nboejaker. ")
    assert C.text_key("a") != C.text_key("b") and C.text_key("   ") == ""


def test_tool_calls_transcripts_and_empty_records_take_no_part():
    assert C.eligible({"record_type": "message", "text": "hello there"})
    assert not C.eligible({"record_type": "cap_call", "text": "[http.get]"})
    assert not C.eligible({"record_type": "message", "text": "[Claude Code · x] working"})
    assert not C.eligible({"record_type": "message", "text": "  "})


def _row(i, at, k="k1", t="message", canon=None):
    return {"id": i, "created_at": at, "text_key": k, "record_type": t, "canonical_id": canon}


def test_the_oldest_copy_is_canonical_and_every_other_points_straight_at_it():
    rows = [_row("b", "2026-02"), _row("a", "2026-01"), _row("c", "2026-03", canon="b"),
            _row("d", "2026-04", canon="a"), _row("x", "2026-01", k="k2")]
    assert C.plan(rows) == {"b": "a", "c": "a"}          # d already points at a; x has no copy; c is not chained
    assert C.unmark(rows) == []


def test_types_are_folded_apart_and_a_rerun_changes_nothing():
    rows = [_row("a", "2026-01"), _row("b", "2026-02", t="observation")]
    assert C.plan(rows) == {}
    done = [_row("a", "2026-01"), _row("b", "2026-02", canon="a")]
    assert C.plan(done) == {} and C.unmark(done) == []


def test_a_canonical_that_left_the_live_set_hands_over_to_the_oldest_copy():
    rows = [_row("b", "2026-02", canon="a"), _row("c", "2026-03", canon="a")]   # a faded away
    assert C.unmark(rows) == ["b"] and C.plan(rows) == {"c": "b"}


def test_recall_returns_only_canonical_records():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "fabric", "memory.py"), encoding="utf-8").read()
    assert "AND NOT (metadata ? 'canonical_id')\n                    ORDER BY (metadata ? 'noise')" in src
    assert "WHERE {where} AND m.canonical_id IS NULL" in src
    assert "record.metadata[\"canonical_id\"] = canon\n                    worthy = False" in src


def _arun(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Conn:
    def __init__(self, rows):
        self.rows, self.keys, self.pointed, self.edges, self.cleared = rows, {}, {}, [], []

    async def fetch(self, sql, *a):
        if "AS canonical_id" in sql:
            return [r for r in self.rows if r["id"] > a[0]]
        if "'text_key', r.k" in sql:
            self.keys.update(zip(a[0], a[1]))
            return [{"id": i} for i in a[0]]
        if "'canonical_id', r.c" in sql:
            self.pointed.update(zip(a[0], a[1]))
            return [{"id": i} for i in a[0]]
        if "metadata - 'canonical_id'" in sql:
            self.cleared += list(a[0])
            return [{"id": i} for i in a[0]]
        return []

    async def execute(self, sql, *a):
        self.edges += list(zip(a[0], a[1]))
        return "INSERT 0 %d" % len(a[0])


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


def test_the_backfill_counts_first_then_folds(monkeypatch):
    try:
        H = importlib.import_module("Vera.vera.fabric.memory_hooks")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")
    rows = [{"id": "a", "record_type": "message", "created_at": "2026-01", "text": "The bash user is boejaker.",
             "text_key": None, "canonical_id": None},
            {"id": "b", "record_type": "message", "created_at": "2026-02", "text": "the bash user is  boejaker.",
             "text_key": None, "canonical_id": None},
            {"id": "c", "record_type": "message", "created_at": "2026-03", "text": "something else",
             "text_key": None, "canonical_id": None}]
    conn, kv = _Conn(rows), {}

    async def pool():
        return _Pool(conn)

    class _R:
        async def get(self, k):
            return kv.get(k)

        async def set(self, k, v):
            kv[k] = v
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_redis", lambda: _R())
    monkeypatch.setattr(H, "_memory", lambda: (None, None))
    monkeypatch.setattr(H, "_CANON_RUN_TASK", None)
    fn = getattr(H.memory_canonical_mark, "__wrapped__", H.memory_canonical_mark)
    dry = _arun(fn(trace_id="t"))
    assert dry["dry"] is True and dry["would_point"] == 1 and dry["would_key"] == 3 and conn.pointed == {}
    out = _arun(H._canon_apply())
    assert conn.pointed == {"b": "a"} and conn.edges == [("b", "a")] and len(conn.keys) == 3
    assert out["pointed"] == 1 and out["edges"] == 1 and json.loads(kv[H._CANON_RUN_KEY])["scanned"] == 3
