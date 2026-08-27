"""web.fetch must return the page without waiting for enrichment.

Measured on prod 2026-08-27: the fetch is ~0.15s, but awaiting the ingest
(embedding + a GLiNER/spaCy NER pass) made the caller wait 12-126s, and one
research run spent over three minutes inside web.fetch. Nothing in the result
depended on the ingest finishing.

These pin the contract that makes that safe:
  * the caller is not blocked by ingest
  * `fabric_dataset` is still reported immediately (it is deterministic)
  * a background failure is ANNOUNCED, not swallowed
  * await_ingest=True still exists for callers that need the fabric populated

Imports the app module, so it runs in-container and skips on a host venv.
"""
import asyncio
import time

import pytest

try:
    # web_capabilities is a PLUGIN module: it raises at import unless the
    # orchestrator is already loaded, so importing it directly silently skips
    # every test here. Import the orchestrator first.
    import Vera.vera.capability_orchestration        # noqa: F401
    from Vera.vera.web import web_capabilities as W
except Exception:                                    # pragma: no cover
    W = None

pytestmark = pytest.mark.skipif(W is None, reason="app module not importable here")

SLOW_S = 2.0          # stands in for the 12-126s real ingest
URL = "https://example.com/a-page"


class _Disc:
    """A discovery module whose ingest is slow, so blocking is measurable."""

    def __init__(self, delay=SLOW_S, boom=False):
        self.delay, self.boom = delay, boom
        self.called = asyncio.Event()
        self.finished = False

    async def discover_recall(self, urls=None):
        return {"pages": [], "entities": []}

    async def discover_ingest_page(self, url, **kw):
        self.called.set()
        await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("ingest exploded")
        self.finished = True
        return {"ok": True, "dataset_id": kw.get("dataset_id"), "entities": 7,
                "record_id": "r1"}


def _patch(monkeypatch, disc, events):
    async def fake_fetch_page(url, timeout=8.0, max_chars=16000):
        return {"html": "<html><body>hi</body></html>", "text": "hi there",
                "title": "T", "status": 200}

    async def fake_emit(ev):
        events.append(ev)

    monkeypatch.setattr(W._wc, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(W, "_discovery", lambda: disc)
    monkeypatch.setattr(W, "emit_event", fake_emit)


def test_caller_is_not_blocked_by_the_ingest(monkeypatch):
    """The whole point: returning must not wait for enrichment."""
    disc, events = _Disc(), []
    _patch(monkeypatch, disc, events)

    async def go():
        t0 = time.monotonic()
        out = await W.cap_web_fetch(url=URL)
        elapsed = time.monotonic() - t0
        assert elapsed < SLOW_S / 2, (
            "web.fetch waited %.2fs for a %.1fs ingest - it is still blocking" % (elapsed, SLOW_S))
        assert out.get("ingest") == "scheduled"
        # and the work really was started, not silently dropped
        await asyncio.wait_for(disc.called.wait(), timeout=SLOW_S)
        return out

    asyncio.run(go())


def test_dataset_is_reported_immediately_even_though_ingest_is_pending(monkeypatch):
    """`fabric_dataset` survives backgrounding because it is deterministic."""
    disc, events = _Disc(), []
    _patch(monkeypatch, disc, events)
    out = asyncio.run(W.cap_web_fetch(url=URL))
    assert out.get("fabric_dataset") == "web.example_com"


def test_entities_are_absent_rather_than_wrong(monkeypatch):
    """Better to omit a value than to report 0 entities as if it were the answer."""
    disc, events = _Disc(), []
    _patch(monkeypatch, disc, events)
    out = asyncio.run(W.cap_web_fetch(url=URL))
    assert "entities" not in out, "a pending ingest must not report an entity count"
    assert "record_id" not in out


def test_await_ingest_restores_blocking_and_completes(monkeypatch):
    disc, events = _Disc(delay=0.05), []
    _patch(monkeypatch, disc, events)
    out = asyncio.run(W.cap_web_fetch(url=URL, await_ingest=True))
    assert out.get("ingest") == "done"
    assert disc.finished, "await_ingest must actually wait for the ingest"


def test_a_background_failure_is_announced_not_swallowed(monkeypatch):
    """A silent background failure is how 'the fabric is missing pages' becomes unfindable."""
    disc, events = _Disc(delay=0.05, boom=True), []
    _patch(monkeypatch, disc, events)

    async def go():
        await W.cap_web_fetch(url=URL)
        for _ in range(40):                     # let the background task finish
            await asyncio.sleep(0.05)
            if any(e.get("type") == "web.fetch.ingested" for e in events):
                break
        ing = [e for e in events if e.get("type") == "web.fetch.ingested"]
        assert ing, "no web.fetch.ingested event was emitted for a failed ingest"
        assert ing[0].get("error"), "the failure must be reported in the event"

    asyncio.run(go())


def test_ingest_disabled_is_reported_as_skipped(monkeypatch):
    disc, events = _Disc(), []
    _patch(monkeypatch, disc, events)
    out = asyncio.run(W.cap_web_fetch(url=URL, ingest_to_fabric=False))
    assert out.get("ingest") == "skipped"
    assert not disc.called.is_set(), "ingest must not run when it is switched off"


def test_this_module_actually_imported_the_app():
    """A skipped suite proves nothing - make the skip itself visible.

    web_capabilities raises unless capability_orchestration is loaded first, so
    an innocent-looking `from Vera.vera.web import web_capabilities` skips all
    six tests and reports green. This asserts we really have the module.
    """
    assert W is not None
    assert hasattr(W, "cap_web_fetch")
