"""Retrieval timing envelopes (four-minds FM-7.0).

The point is the outcome per leg: an embed that timed out (so Chroma never ran) must not
look like a Chroma search that found nothing.
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory, retrieval_timing_core as rtc  # noqa: E402

pytestmark = pytest.mark.critical


def test_leg_outcome():
    assert rtc.leg_outcome([1]) == "ok"
    assert rtc.leg_outcome([]) == "empty"
    assert rtc.leg_outcome(asyncio.TimeoutError()) == "timeout"
    assert rtc.leg_outcome(TimeoutError()) == "timeout"
    assert rtc.leg_outcome(RuntimeError("x")) == "error"


def test_envelope_and_window_percentiles():
    clock = iter([0.0, 0.1, 0.3, 0.3, 0.3]).__next__
    env = rtc.Envelope("p", clock=clock)
    env.stage("embed", env.mark(), "timeout")
    env.stage("chroma", 0.0, "skipped:no-vector")
    d = env.to_dict()
    assert d["type"] == "retrieval.timing" and d["stages"][0] == {"stage": "embed", "ms": 100.0, "outcome": "timeout"}
    assert env.worth_an_event()
    w = rtc.Window(size=3)
    for ms in (10, 20, 30, 40):
        w.add({"path": "p", "total_ms": ms, "stages": [{"stage": "s", "ms": ms, "outcome": "ok"}]})
    s = w.summary()["p"]
    assert s["calls"] == 3 and s["total"]["p50"] == 30 and s["stages"]["s"]["outcomes"] == {"ok": 3}
    assert w.summary("other") == {}


def test_timed_reports_exceptions_and_reraises_cancel():
    async def boom():
        raise RuntimeError("x")
    r, ms = asyncio.run(rtc.timed(boom()))
    assert isinstance(r, RuntimeError) and ms >= 0

    async def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rtc.timed(cancelled()))


class _B:
    def __init__(self, hits, delay=0.0, exc=None):
        self.hits, self.delay, self.exc = hits, delay, exc

    async def search(self, query, limit=10, filters=None, embedding=None):
        await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return [(memory.MemoryRecord(id=i, text="t " + i, importance=0.5), s) for i, s in self.hits]


class _Chroma(_B):
    """Like ChromaBackend: no query vector, no search."""

    async def search(self, query, limit=10, filters=None, embedding=None):
        if not embedding:
            return []
        return await super().search(query, limit, filters, embedding)


def _store(**b):
    st = memory.HybridMemoryStore()
    st._backends = dict(b)
    return st


def _last(path):
    return list(memory.RETRIEVAL_TIMING._by_path[path])[-1]


def _outcomes(env):
    return {s["stage"]: s["outcome"] for s in env["stages"]}


def test_search_records_embed_timeout_apart_from_empty(monkeypatch):
    async def no_vec(q):
        return None, "timeout"
    monkeypatch.setattr(memory, "embed_text_outcome", no_vec)
    out = asyncio.run(_store(neo4j=_B([("k", 4.0)]), postgres=_B([]), chroma=_Chroma([("v", 0.5)]))
                      .search("q", limit=3))
    assert [r["record"]["id"] for r in out] == ["k"]
    o = _outcomes(_last("hybrid.search"))
    assert o == {"embed": "timeout", "neo4j": "ok", "postgres": "empty",
                 "chroma": "skipped:no-vector", "merge": "ok"}


def test_search_records_backend_errors(monkeypatch):
    async def vec(q):
        return [0.1], "ok"
    monkeypatch.setattr(memory, "embed_text_outcome", vec)
    asyncio.run(_store(neo4j=_B([], exc=RuntimeError("down")), chroma=_Chroma([])).search("q"))
    o = _outcomes(_last("hybrid.search"))
    assert o["neo4j"] == "error" and o["chroma"] == "empty" and o["embed"] == "ok"


def test_fast_and_slow_tiers_each_get_an_envelope(monkeypatch):
    async def vec(q):
        return None, "skipped:cooldown"
    monkeypatch.setattr(memory, "embed_text_outcome", vec)
    st = _store(neo4j=_B([("k", 4.0)]), postgres=_B([], delay=0.5), chroma=_Chroma([("v", 0.5)]))

    async def go():
        fast, slow = await st.search_fast_and_slow("q", limit=3, fast_timeout=0.2)
        return fast, await slow
    asyncio.run(go())
    assert _outcomes(_last("hybrid.fast")) == {"postgres": "timeout", "neo4j": "timeout", "merge": "empty"}
    assert _outcomes(_last("hybrid.slow")) == {"embed": "skipped:cooldown", "chroma": "skipped:no-vector",
                                               "merge": "empty"}


def test_search_fast_envelope():
    asyncio.run(_store(neo4j=_B([("k", 4.0)]), postgres=_B([])).search_fast("q", limit=3))
    assert _outcomes(_last("hybrid.fast_only")) == {"postgres": "empty", "neo4j": "ok", "merge": "ok"}


def test_embed_text_keeps_its_contract(monkeypatch):
    async def out(q):
        return [0.2], "ok"
    monkeypatch.setattr(memory, "embed_text_outcome", out)
    assert asyncio.run(memory.embed_text("x")) == [0.2]
