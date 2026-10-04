"""context.recall within a budget (vera/fabric/recall_budget_core.py), and its wiring.

Measured on prod 2026-10-05: one chat context fetch spent 7.1-7.3 s of ~9 s in context.recall, which waited for its
slowest source with no limit (cold related_qa 5.6 s, entity crawl 3.7 s; warm 0.03-0.25 s).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.fabric import recall_budget_core as rb  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


async def _after(s, v):
    await asyncio.sleep(s)
    return v


async def _boom():
    raise RuntimeError("worldview down")


def test_no_budget_waits_for_every_source():
    async def go():
        return await rb.gather_until({"a": _after(0.01, 1), "b": _after(0.05, 2)}, 0)
    done, ms, pending = asyncio.run(go())
    assert done == {"a": 1, "b": 2} and pending == {} and set(ms) == {"a", "b"}


def test_a_budget_answers_with_what_finished_and_leaves_the_rest_running():
    async def go():
        done, ms, pending = await rb.gather_until({"fast": _after(0.01, "f"), "slow": _after(0.3, "s")}, 0.1)
        assert not pending["slow"].done() and not pending["slow"].cancelled()   # left running, not cancelled
        late = await pending["slow"]
        return done, ms, list(pending), late
    done, ms, pend, late = asyncio.run(go())
    assert done == {"fast": "f"} and "fast" in ms and ms["fast"] >= 0
    assert pend == ["slow"] and late[0] == "s"


def test_a_failed_source_is_its_exception_not_an_error():
    async def go():
        return await rb.gather_until({"wv": _boom(), "qa": _after(0.01, "q")}, 0.5)
    done, ms, pending = asyncio.run(go())
    assert isinstance(done["wv"], RuntimeError) and done["qa"] == "q" and not pending


def test_a_late_answer_is_kept_for_the_next_call():
    got = []

    async def go():
        _d, _m, pending = await rb.gather_until({"qa": _after(0.1, ("rows", "edges"))}, 0.01)
        rb.keep_when_done(pending["qa"], got.append)
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert got == [("rows", "edges")]


def test_a_late_failure_is_not_kept():
    got = []

    async def go():
        async def slow_boom():
            await asyncio.sleep(0.05)
            raise RuntimeError("x")
        _d, _m, pending = await rb.gather_until({"qa": slow_boom()}, 0.01)
        rb.keep_when_done(pending["qa"], got.append)
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert got == []


def test_nothing_to_run_is_nothing():
    assert asyncio.run(rb.gather_until({}, 1.0)) == ({}, {}, {})


# -- wiring --------------------------------------------------------------------------------------------------------
def test_recall_takes_a_budget_and_keeps_each_source_for_the_next_call():
    s = _src("vera/fabric/context.py")
    assert "    budget_ms:      int  = 0," in s
    assert "done, took, pending = await _rbc.gather_until(coros, budget_s)" in s
    assert "_rbc.keep_when_done(task, lambda res, _k=_src_key(name): _recall_cache_put(_k, {\"v\": res}))" in s
    assert "hit = _recall_cache_get(_src_key(name))" in s
    # an answer missing a late source is never cached as the whole answer
    assert "    if not pending:" in s and '"timings": timings, "late": sorted(pending)' in s


def test_the_chat_preview_passes_the_budget_and_a_fetch_you_ask_for_waits():
    s = _src("vera/chat/chat_panel.html")
    assert "budget_ms:force?0:1500," in s


# -- the capability itself, with fake sources (the gate container imports the app as Vera.vera) -----------------
def _ctx_module():
    import importlib
    try:
        return importlib.import_module("Vera.vera.fabric.context")
    except Exception:
        import pytest
        pytest.skip("the app package is not importable here (run in the gate container)")


def test_recall_answers_within_its_budget_and_the_late_source_comes_next_call(monkeypatch):
    C = _ctx_module()
    C._RECALL_CACHE.clear()

    async def ent(seed_ids, query, top_k, ds):
        return [{"id": "e1", "text": "entity hit", "score": 0.9, "source": "entity"}], []

    async def wv(query, top_k, ds, ro, steps):
        raise RuntimeError("worldview down")

    async def qa(query, session_id, agent_name):
        await asyncio.sleep(0.4)
        return [{"id": "q1", "text": "a past answer", "score": 0.8, "source": "qa"}], [], [{"q": "x"}]

    monkeypatch.setattr(C, "_recall_entity_crawl", ent)
    monkeypatch.setattr(C, "_recall_worldview", wv)
    monkeypatch.setattr(C, "_recall_related_qa", qa)
    fn = getattr(C.cap_context_recall, "__wrapped__", C.cap_context_recall)

    async def go():
        first = await fn(query="budget probe", budget_ms=100)
        await asyncio.sleep(0.5)                       # the late source finishes in the background
        second = await fn(query="budget probe", budget_ms=100)
        third = await fn(query="budget probe", budget_ms=100)
        return first, second, third
    first, second, third = asyncio.run(go())
    assert [r["id"] for r in first["results"]] == ["e1"]
    assert first["late"] == ["related_qa"] and first["timings"]["related_qa"] == "late"
    assert isinstance(first["timings"]["entities"], float)
    assert {r["id"] for r in second["results"]} == {"e1", "q1"} and second["late"] == []
    assert second["timings"]["related_qa"] == "cached" and second["pairs"] == [{"q": "x"}]
    assert third["cached"] is True                     # complete now, so the whole answer is cached


def test_recall_without_a_budget_waits_for_every_source(monkeypatch):
    C = _ctx_module()
    C._RECALL_CACHE.clear()

    async def ent(seed_ids, query, top_k, ds):
        return [], []

    async def wv(query, top_k, ds, ro, steps):
        return [], []

    async def qa(query, session_id, agent_name):
        await asyncio.sleep(0.2)
        return [{"id": "q1", "text": "a past answer", "score": 0.8, "source": "qa"}], [], []

    monkeypatch.setattr(C, "_recall_entity_crawl", ent)
    monkeypatch.setattr(C, "_recall_worldview", wv)
    monkeypatch.setattr(C, "_recall_related_qa", qa)
    fn = getattr(C.cap_context_recall, "__wrapped__", C.cap_context_recall)
    out = asyncio.run(fn(query="no budget probe"))
    assert [r["id"] for r in out["results"]] == ["q1"] and out["late"] == []
