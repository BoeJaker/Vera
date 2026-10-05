"""The hybrid memory store reports one 0..1 score (RF-05).

Live 2026-10-04: Neo4j answered 1.4-7.5 (Lucene), Chroma 0.40-0.58 (cosine), and the
merge kept the largest raw number, so every keyword hit passed every 0..1 threshold.
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory, memory_score_core as msc  # noqa: E402

pytestmark = pytest.mark.critical


def test_normalise_maps_each_backend_onto_0_1():
    assert msc.normalise("neo4j", 2.0) == pytest.approx(0.5)
    assert 0.78 < msc.normalise("neo4j", 7.5) < 0.8
    assert msc.normalise("chroma", 0.45) == pytest.approx(0.45)
    assert msc.normalise("postgres", 1.3) == 1.0
    for bad in (None, "x", -1, float("nan"), 0):
        assert msc.normalise("neo4j", bad) == 0.0


def test_combine_rewards_agreement_and_stays_in_range():
    s = msc.combine({"neo4j": [("a", 4.3), ("b", 1.45), ("a", 1.0)],
                     "chroma": [("a", 0.45), ("c", 0.58)]})
    assert s["a"] > max(msc.normalise("neo4j", 4.3), 0.45)      # found by both
    assert s["c"] == pytest.approx(0.58)
    assert all(0.0 <= v <= 1.0 for v in s.values())
    assert s["a"] == pytest.approx(1 - (1 - 4.3 / 6.3) * (1 - 0.45))   # best hit per backend once


class _B:
    def __init__(self, hits):
        self.hits = hits
        self.limits = []

    async def search(self, query, limit=10, filters=None, embedding=None):
        self.limits.append(limit)
        return [(memory.MemoryRecord(id=i, text="text " + i, importance=0.5), s) for i, s in self.hits]


def _store(**backends):
    st = memory.HybridMemoryStore()
    st._backends = dict(backends)
    return st


def _run(coro):
    return asyncio.run(coro)


def test_hybrid_search_reports_0_1_and_keeps_keyword_hits_first(monkeypatch):
    async def _embed(q):
        return [0.1] * 4, "ok"
    monkeypatch.setattr(memory, "embed_text_outcome", _embed)
    neo, chroma = _B([("k1", 7.4), ("k2", 4.3)]), _B([("v1", 0.46), ("v2", 0.44)])
    out = _run(_store(neo4j=neo, chroma=chroma).search("census minds", limit=4))
    ids = [r["record"]["id"] for r in out]
    assert ids[:2] == ["k1", "k2"]
    assert all(0.0 <= r["score"] <= 1.0 for r in out)
    assert neo.limits == [4 * memory._CANDIDATES_PER_HIT]


def test_single_backend_and_empty_query_keep_raw_scores(monkeypatch):
    neo = _B([("k1", 7.4)])
    out = _run(_store(neo4j=neo, chroma=_B([])).search("q", limit=3, backends=["neo4j"]))
    assert out[0]["score"] == pytest.approx(7.4)
    out = _run(_store(neo4j=_B([("k1", 0.9)]), postgres=_B([("k1", 0.9)])).search_fast("", limit=3))
    assert out[0]["score"] == pytest.approx(0.9)


def test_fast_and_slow_tiers_share_the_scale(monkeypatch):
    async def _embed(q):
        return [0.1] * 4, "ok"
    monkeypatch.setattr(memory, "embed_text_outcome", _embed)
    st = _store(neo4j=_B([("k1", 4.3)]), postgres=_B([]), chroma=_B([("k1", 0.5), ("v1", 0.55)]))

    async def go():
        fast, slow = await st.search_fast_and_slow("q", limit=5)
        return fast, await slow
    fast, slow = _run(go())
    f = {r["record"]["id"]: r["score"] for r in fast}
    s = {r["record"]["id"]: r["score"] for r in slow}
    assert f["k1"] == pytest.approx(4.3 / 6.3, abs=1e-3)
    assert s["k1"] == pytest.approx(1 - (1 - 4.3 / 6.3) * 0.5, abs=1e-3)
    assert s["v1"] == pytest.approx(0.55)
    assert all(0.0 <= v <= 1.0 for v in list(f.values()) + list(s.values()))
