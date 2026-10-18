"""The usage counter through the real dispatcher wrapper: each call is counted with the actor the dispatcher knows
and the surface the middleware read; the flush writes one hash per UTC day with a 91-day TTL and puts the batch
back when Redis fails; caps.usage reads it ranked by person. The middleware sets CALLER_SURFACE for HTTP and
WebSocket requests and only for the request.

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
from Vera.vera import caller_context as ccm  # noqa: E402
from Vera.vera import caller_context_core as cc  # noqa: E402

PROBE = "test.usage.probe"
OUTER = "test.usage.outer"


@orch.capability(PROBE, memory="off", silent=True, mcp_expose=False)
async def _probe(trace_id=None):
    return {"ok": True}


@orch.capability(OUTER, memory="off", silent=True, mcp_expose=False)
async def _outer(trace_id=None):
    return await orch.CAPABILITY_REGISTRY[PROBE]["func"]()


class FakePipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def hincrby(self, k, f, n):
        self.ops.append(("hincrby", k, f, n))

    def expire(self, k, s):
        self.ops.append(("expire", k, s))

    async def execute(self):
        if self.r.fail:
            raise ConnectionError("down")
        for op in self.ops:
            if op[0] == "hincrby":
                h = self.r.data.setdefault(op[1], {})
                h[op[2]] = h.get(op[2], 0) + op[3]
            else:
                self.r.ttl[op[1]] = op[2]


class FakeRedis:
    def __init__(self):
        self.data, self.ttl, self.fail = {}, {}, False

    def pipeline(self, transaction=True):
        return FakePipe(self)

    async def hgetall(self, k):
        return {f.encode(): str(n).encode() for f, n in self.data.get(k, {}).items()}


def call(name=PROBE, **ctx):
    toks = []
    for var, value in ctx.items():
        cv = getattr(orch, var)
        toks.append((cv, cv.set(value)))
    try:
        return asyncio.run(orch.CAPABILITY_REGISTRY[name]["func"]())
    finally:
        for cv, t in reversed(toks):
            cv.reset(t)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    buf = cc.UsageBuffer(interval=3600)
    buf.last_flush = __import__("time").monotonic()     # not due: tests flush explicitly
    monkeypatch.setattr(orch, "_USAGE", buf)
    monkeypatch.setattr(orch, "REDIS", None)
    yield


def counted():
    return {k: v for k, v in orch._USAGE.snapshot().items() if k.startswith("test.usage.")}


def test_each_call_is_counted_with_its_actor_and_surface():
    call(CURRENT_HTTP_CAP=PROBE, CALLER_SURFACE=("panel:markets", "person"))
    call(MCP_CALL_ACTIVE=True, CALLER_SURFACE=("chat", "auto"))
    call(MCP_CALL_ACTIVE=True, CALLER_KIND="claude")
    call(BACKGROUND_LLM="dream:c1")
    call()
    assert counted() == {
        "test.usage.probe|ui:route|panel:markets|person": 1,
        "test.usage.probe|ui:mcp|chat|auto": 1,
        "test.usage.probe|bridge:claude|-|-": 1,
        "test.usage.probe|background:dream|-|-": 1,
        "test.usage.probe|internal|-|-": 1,
    }


def test_a_nested_call_counts_as_nested_and_the_outer_as_its_route():
    call(OUTER, CURRENT_HTTP_CAP=OUTER, CALLER_SURFACE=("desk", "person"))
    assert counted() == {"test.usage.outer|ui:route|desk|person": 1,
                         "test.usage.probe|nested|desk|person": 1}


def test_flush_writes_one_day_hash_with_ttl_and_restores_on_failure(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(orch, "REDIS", r)
    call(CURRENT_HTTP_CAP=PROBE)
    call(CURRENT_HTTP_CAP=PROBE)
    asyncio.run(orch._flush_usage())
    key = cc.day_key()
    assert r.data[key]["test.usage.probe|ui:route|-|-"] == 2 and r.ttl[key] == cc.RETENTION_S
    assert not counted() and orch._USAGE.flushing is False
    r.fail = True
    call(CURRENT_HTTP_CAP=PROBE)
    asyncio.run(orch._flush_usage())
    assert counted() == {"test.usage.probe|ui:route|-|-": 1}   # put back, counted next time
    assert orch._USAGE.flushing is False


def test_caps_usage_reads_the_days_and_the_pending_counts(monkeypatch):
    r = FakeRedis()
    r.data[cc.day_key()] = {"test.usage.probe|ui:route|desk|auto": 500,
                            "test.usage.outer|ui:mcp|chat|person": 4}
    monkeypatch.setattr(orch, "REDIS", r)
    call(CURRENT_HTTP_CAP=PROBE, CALLER_SURFACE=("desk", "person"))
    out = asyncio.run(orch.CAPABILITY_REGISTRY["caps.usage"]["raw"](days=7, cap="test.usage."))
    assert out["days"] == 7 and out["pending"] >= 1
    assert [b["cap"] for b in out["by_cap"]] == ["test.usage.outer", "test.usage.probe"]
    p = next(b for b in out["by_cap"] if b["cap"] == "test.usage.probe")
    assert (p["person"], p["auto"]) == (1, 500)


def test_the_middleware_sets_the_surface_only_for_the_request():
    from fastapi import FastAPI, WebSocket
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.get("/s")
    async def s():
        return {"v": list(orch.CALLER_SURFACE.get())}

    @app.websocket("/w")
    async def w(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"v": list(orch.CALLER_SURFACE.get())})
        await ws.close()

    app.add_middleware(ccm.CallerSurface, var=orch.CALLER_SURFACE)
    c = TestClient(app)
    assert c.get("/s", headers={"X-Vera-Surface": "dashboard; trigger=auto"}).json() == {"v": ["dashboard", "auto"]}
    assert c.get("/s").json() == {"v": ["", ""]}
    with c.websocket_connect("/w?surface=lhm&trigger=person") as ws:
        assert ws.receive_json() == {"v": ["lhm", "person"]}
    assert orch.CALLER_SURFACE.get() == ("", "")


def test_the_app_installs_the_middleware_inside_the_edge_guard():
    kinds = [m.cls for m in orch.APP.user_middleware]
    assert ccm.CallerSurface in kinds
    from Vera.vera import edge_guard
    assert kinds.index(edge_guard.EdgeGuard) < kinds.index(ccm.CallerSurface)   # first in the list = outermost
