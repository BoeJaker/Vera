"""WSU-41: web.research tells the chat how it stands while it runs - the same info.seek.step events info.seek sends
(stage, checked, the pages in hand and whether each is read), keyed by the query, which is the question the chat's
live desk item for the call holds."""
import asyncio

import pytest

try:
    import Vera.vera.capability_orchestration  # noqa: F401
    from Vera.vera.web import web_capabilities as W
except Exception:                                   # the app module is not importable here (as test_crawl_policy)
    W = None

pytestmark = pytest.mark.skipif(W is None, reason="app module not importable here")


def _run(monkeypatch, read_fails=()):
    events = []

    async def emit(ev):
        events.append(ev)

    async def search(query, limit, engine, discover):
        return {"engine_used": "searx", "results": [
            {"url": "https://a.example/1", "title": "A one", "domain": "a.example"},
            {"url": "https://b.example/2", "title": "B two", "domain": "b.example"},
            {"url": "https://c.example/3", "title": "C three", "domain": "c.example"},
        ]}

    async def reader(url, max_chars, mode):
        if url in read_fails:
            return {"ok": False}
        return {"ok": True, "markdown": "text of " + url, "title": "T " + url, "published": "2026-10-01"}

    async def fetch(url, timeout, max_chars, ingest_to_fabric):
        return {"error": "blocked"}

    monkeypatch.setattr(W, "emit_event", emit)
    monkeypatch.setattr(W, "cap_web_search", search)
    monkeypatch.setattr(W, "cap_web_fetch", fetch)
    monkeypatch.setitem(W._orch.CAPABILITY_REGISTRY, "reader.page", {"func": reader})
    out = asyncio.run(W.cap_web_research(query="latest on the thing", results=3, trace_id="tr-1"))
    return out, events


def test_stages_in_order_keyed_by_the_query(monkeypatch):
    out, events = _run(monkeypatch)
    steps = [e for e in events if e.get("type") == "info.seek.step"]
    assert len(out["sources"]) == 3
    assert all(e["question"] == "latest on the thing" and e["trace_id"] == "tr-1" for e in steps)   # DA-04a
    stages = [e["stage"] for e in steps]
    assert stages[0] == "searching the web"
    assert stages[1] == "reading the top 3 pages"
    assert stages[-1] == "done"
    assert [s for s in stages if s.startswith("read ")][-1] == "read 3 of 3 pages"
    # the pages are in hand from the search on; each says when it has been read
    assert [i["url"] for i in steps[1]["items"]] == ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    assert not any(i["read"] for i in steps[1]["items"])
    assert all(i["read"] for i in steps[-1]["items"])
    assert steps[-1]["items"][0]["published"] == "2026-10-01"
    assert any(c.startswith("search: 3 results") for c in steps[-1]["checked"])
    assert any(e.get("type") == "web.research.done" for e in events)   # the old event still goes


def test_a_page_that_cannot_be_read_is_shown_unread(monkeypatch):
    out, events = _run(monkeypatch, read_fails=("https://b.example/2",))
    last = [e for e in events if e.get("type") == "info.seek.step"][-1]
    assert [i["read"] for i in last["items"]] == [True, False, True]
    assert "read: 2 of 3 pages" in last["checked"]


def test_info_seek_steps_carry_the_trace_id_too(monkeypatch):
    """DA-04a: info.seek's own steps are keyed by the call's trace as well, not only by the question text."""
    events = []

    async def emit(ev):
        events.append(ev)

    async def call(name, timeout, **kw):
        return {}

    async def searches(queries, first_slot=0):
        return [{"results": [{"url": "https://a.example/1", "title": "A one", "domain": "a.example"}]} for _ in queries]

    async def rerank(q, hits):
        return hits

    async def read(items, n, chars):
        return [dict(i, text="text of " + i["url"]) for i in items[:n]]

    monkeypatch.setattr(W, "emit_event", emit)
    monkeypatch.setattr(W, "_seek_call", call)
    monkeypatch.setattr(W, "_seek_searches", searches)
    monkeypatch.setattr(W, "_seek_rerank", rerank)
    monkeypatch.setattr(W, "_seek_read", read)
    asyncio.run(W.cap_info_seek(question="what is the latest on the thing", trace_id="tr-9"))
    steps = [e for e in events if e.get("type") == "info.seek.step"]
    assert steps and all(e.get("trace_id") == "tr-9" for e in steps)


def test_no_results_says_so(monkeypatch):
    async def empty(query, limit, engine, discover):
        return {"results": []}
    events = []

    async def emit(ev):
        events.append(ev)
    monkeypatch.setattr(W, "emit_event", emit)
    monkeypatch.setattr(W, "cap_web_search", empty)
    asyncio.run(W.cap_web_research(query="nothing", results=3))
    assert [e["stage"] for e in events if e.get("type") == "info.seek.step"] == ["searching the web", "no results", "done"]
