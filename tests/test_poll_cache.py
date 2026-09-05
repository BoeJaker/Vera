"""Vera asked every Ollama node its version every ten seconds.

_fetch_instance_detail polls /api/version and /api/ps together on every cluster
tick. Measured on prod 2026-09-05 over an exact 120-second window of newly
written log, per node:

    /api/version  33     /api/ps  34     /api/tags  6     /api/embed  0

Identical counts, because they were issued as a pair - ~17 of each per minute
per node, about 100 requests a minute across three nodes, for one string that
changes only when somebody upgrades Ollama.

/api/ps stays on the tick: resident models and VRAM are the point of the
poller. The version is cached, and the cache is dropped when /api/ps FAILS -
a node that stopped answering may be restarting, and a restart is the one
event that can change the version.

Pure: no network, no clock.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.workers import poll_cache as P     # noqa: E402

pytestmark = pytest.mark.critical


def test_an_unknown_node_is_always_fetched():
    assert P.should_fetch(None, 0.0) is True
    assert P.should_fetch({}, 0.0) is True


def test_a_fresh_version_is_not_refetched():
    """The whole point: 17 requests a minute become one an hour."""
    assert P.should_fetch(("0.32.6", 100.0), 100.0 + 10) is False
    assert P.should_fetch(("0.32.6", 100.0), 100.0 + 3599) is False


def test_a_stale_version_is_refetched():
    assert P.should_fetch(("0.32.6", 100.0), 100.0 + 3601) is True


def test_a_failed_read_is_retried_rather_than_remembered_as_empty():
    """Caching "" would pin a node's version to blank for the whole TTL."""
    assert P.should_fetch(("", 100.0), 101.0) is True
    assert P.should_fetch(("   ", 100.0), 101.0) is True


def test_an_empty_version_is_never_cached():
    c = {}
    P.remember(c, "cpu-247", "", 10.0)
    assert c == {}


def test_a_good_version_is_cached_and_returned_without_refetching():
    c = {}
    P.remember(c, "cpu-246", "0.32.6", 10.0)
    assert P.cached_value(c["cpu-246"]) == "0.32.6"
    assert P.should_fetch(c["cpu-246"], 20.0) is False


def test_a_node_that_stops_answering_forgets_its_version():
    """A restart is the one event that changes the version - and it is exactly
    what makes /api/ps stop answering. Without this the TTL would hide an
    upgrade for an hour."""
    c = {}
    P.remember(c, "cpu-246", "0.32.6", 10.0)
    P.forget(c, "cpu-246")
    assert P.should_fetch(c.get("cpu-246"), 11.0) is True


def test_corrupt_cache_entries_do_not_wedge_a_node():
    for junk in (("only-one",), ("v", "not-a-number"), 42, ""):
        assert P.should_fetch(junk, 0.0) is True
    assert P.cached_value(42) == "" or True   # must not raise


def test_forgetting_an_unknown_node_is_harmless():
    assert P.forget({}, "nope") == {}


# ── the wiring, counted ────────────────────────────────────────────────────

def test_the_poller_asks_for_the_version_once_not_every_tick():
    """Driven against the real _fetch_instance_detail with a stubbed HTTP
    client, counting actual GETs - a source check cannot tell a live cache
    from a dead one."""
    import asyncio
    cl = pytest.importorskip("vera.workers.cluster")

    calls = {"version": 0, "ps": 0}

    class _Resp:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            if url.endswith("/api/version"):
                calls["version"] += 1
                return _Resp({"version": "0.32.6"})
            if url.endswith("/api/ps"):
                calls["ps"] += 1
                return _Resp({"models": []})
            return _Resp({})
        async def post(self, url, **k):
            return _Resp({})

    real_client = cl.httpx.AsyncClient
    cl.httpx.AsyncClient = _Client
    cl._VERSION_CACHE.clear()
    try:
        inst = {"url": "http://node:11435", "models": [], "num_ctx": 4096}
        for _ in range(6):
            asyncio.run(cl._fetch_instance_detail("cpu-246", inst))
        assert calls["ps"] == 6, "/api/ps must still run every tick"
        assert calls["version"] == 1, \
            "version fetched %d times across 6 ticks - the cache is not working" % calls["version"]
        assert inst["version"] == "0.32.6", "the cached version must still be reported"
    finally:
        cl.httpx.AsyncClient = real_client
        cl._VERSION_CACHE.clear()


def test_a_node_whose_ps_fails_refetches_its_version_next_tick():
    """The invalidation CALL SITE, not just the helper. A node that stopped
    answering may be restarting, which is the one thing that changes the
    version - so the next successful tick must ask again rather than serve a
    stale string for the rest of the TTL."""
    import asyncio
    cl = pytest.importorskip("vera.workers.cluster")

    calls = {"version": 0}
    state = {"ps_ok": True}

    class _Resp:
        def __init__(self, payload, code=200): self._p, self.status_code = payload, code
        def json(self): return self._p

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            if url.endswith("/api/version"):
                calls["version"] += 1
                return _Resp({"version": "0.32.6"})
            if url.endswith("/api/ps"):
                if not state["ps_ok"]:
                    raise RuntimeError("node restarting")
                return _Resp({"models": []})
            return _Resp({})
        async def post(self, url, **k): return _Resp({})

    real = cl.httpx.AsyncClient
    cl.httpx.AsyncClient = _Client
    cl._VERSION_CACHE.clear()
    try:
        inst = {"url": "http://node:11435", "models": [], "num_ctx": 4096}
        asyncio.run(cl._fetch_instance_detail("cpu-246", inst))   # fetch + cache
        asyncio.run(cl._fetch_instance_detail("cpu-246", inst))   # cached
        assert calls["version"] == 1

        state["ps_ok"] = False                                    # node goes away
        asyncio.run(cl._fetch_instance_detail("cpu-246", inst))
        state["ps_ok"] = True                                     # and comes back
        asyncio.run(cl._fetch_instance_detail("cpu-246", inst))
        assert calls["version"] == 2, \
            "version was not re-read after the node stopped answering"
    finally:
        cl.httpx.AsyncClient = real
        cl._VERSION_CACHE.clear()
