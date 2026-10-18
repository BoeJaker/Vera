"""WSU-62: after a turn, memory.feedback says what the turn was GIVEN as well as what it USED, so the chat can leave out
the context runs of memories the reply did not draw on. record_turn_usage is run with its store faked."""
import asyncio

import pytest

try:
    import Vera.vera.capability_orchestration  # noqa: F401
    from Vera.vera.fabric import memory_hooks as MH
except Exception:                                   # the app module is not importable here (as test_crawl_policy)
    MH = None

pytestmark = pytest.mark.skipif(MH is None, reason="app module not importable here")


class _Conn:
    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return None

    async def executemany(self, *a, **k):
        return None


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


class _Pool:
    def acquire(self):
        return _Acq()


def test_feedback_names_what_was_given_and_what_was_used(monkeypatch):
    events = []

    async def emit(ev):
        events.append(ev)

    async def pool():
        return _Pool()

    monkeypatch.setattr(MH, "emit_event", emit)
    monkeypatch.setattr(MH, "_usage_pool", pool)
    monkeypatch.setattr(MH._usage_core, "counts_toward_use", lambda sid, reply: True)
    monkeypatch.setattr(MH, "_reval", None)
    monkeypatch.setitem(MH._INJECTED, "s-wsu62", {
        "m-used": "the gateway runs on port 8443 behind the nginx proxy with tls",
        "m-not": "a recipe for lemon cake with three eggs and a cup of sugar",
    })
    reply = "The gateway runs on port 8443 behind the nginx proxy, with TLS terminated there."
    out = asyncio.run(MH.record_turn_usage("s-wsu62", reply))
    fb = [e for e in events if e.get("type") == "memory.feedback"]
    assert fb, events
    e = fb[-1]
    assert set(e["injected_ids"]) == {"m-used", "m-not"}
    assert e["used_ids"] == ["m-used"]
    assert out["injected"] == 2 and out["used"] == 1
