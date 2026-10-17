"""The edge guard as ASGI middleware (vera/edge_guard.py), driven through a real
Starlette app with the TestClient: refusals happen before routing, WebSockets
are closed before accept, observe mode refuses nothing, and evidence events
fire. Behaviour only; nothing here reads the source.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from vera import edge_guard as eg  # noqa: E402

HOSTS = {"testserver", "localhost", "127.0.0.1"}


def _app(mode, events, host_mode="observe"):
    calls = []

    async def mcp_call(request):
        calls.append("mcp")
        return JSONResponse({"ok": True})

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_json({"hello": True})
        await websocket.close()

    inner = Starlette(routes=[Route("/mcp/call", mcp_call, methods=["POST", "GET"]),
                              WebSocketRoute("/ws/mcp", ws)])
    guarded = eg.EdgeGuard(inner, allowed_hosts=HOSTS, allowed_origins=[], trusted_proxies=[],
                           listen_port=None, on_event=events.append, mode=mode, host_mode=host_mode)
    return guarded, calls


def test_enforce_refuses_foreign_post_before_routing():
    ev = []
    app, calls = _app("enforce", ev)
    c = TestClient(app)
    r = c.post("/mcp/call", content="{}", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["rule"] == "origin"
    assert calls == []                                   # never reached the route
    assert ev and ev[0]["type"] == "auth.refused" and ev[0]["origin"] == "https://evil.example"


def test_enforce_refuses_csrf_shape_without_origin():
    app, calls = _app("enforce", [])
    r = TestClient(app).post("/mcp/call", content="{}", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and calls == []


def test_enforce_allows_same_origin_and_non_browser():
    app, calls = _app("enforce", [])
    c = TestClient(app)
    assert c.post("/mcp/call", content="{}", headers={"origin": "http://testserver"}).status_code == 200
    assert c.post("/mcp/call", content="{}").status_code == 200       # bridge shape: no Origin
    assert calls == ["mcp", "mcp"]


def test_enforce_closes_foreign_websocket_before_accept():
    app, _ = _app("enforce", [])
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/mcp", headers={"origin": "https://evil.example"}) as s:
            s.receive_json()
    assert e.value.code == 1008


def test_enforce_accepts_own_origin_websocket():
    app, _ = _app("enforce", [])
    with TestClient(app).websocket_connect("/ws/mcp", headers={"origin": "http://testserver"}) as s:
        assert s.receive_json() == {"hello": True}


def test_observe_refuses_nothing_but_records():
    ev = []
    app, calls = _app("observe", ev)
    c = TestClient(app)
    r = c.post("/mcp/call", content="{}", headers={"origin": "https://evil.example"})
    assert r.status_code == 200 and calls == ["mcp"]
    assert ev and ev[0]["type"] == "auth.would_refuse"
    st = app.status()
    assert st["counts"].get("auth.would_refuse:origin") == 1
    assert st["recent"][-1]["origin"] == "https://evil.example"


def test_host_rule_observed_unless_host_mode_enforce():
    ev = []
    app, calls = _app("enforce", ev, host_mode="observe")
    r = TestClient(app, base_url="http://attacker.example").post("/mcp/call", content="{}")
    assert r.status_code == 200 and ev[0]["type"] == "auth.would_refuse" and ev[0]["rule"] == "host"
    app2, _ = _app("enforce", [], host_mode="enforce")
    r2 = TestClient(app2, base_url="http://attacker.example").post("/mcp/call", content="{}")
    assert r2.status_code == 421


def test_off_mode_passes_everything():
    app, calls = _app("off", [])
    r = TestClient(app).post("/mcp/call", content="{}", headers={"origin": "https://evil.example"})
    assert r.status_code == 200 and calls == ["mcp"]


def test_redis_override_with_cache():
    class FakeRedis:
        def __init__(self, v):
            self.v = v

        async def get(self, k):
            return self.v

    app, _ = _app("enforce", [])
    app.redis_getter = lambda: FakeRedis(b"observe")
    assert asyncio.run(app.mode()) == "observe"
    app2, _ = _app("observe", [])
    app2.redis_getter = lambda: FakeRedis("bogus")
    assert asyncio.run(app2.mode()) == "observe"          # unknown value ignored


def test_event_rate_limited_per_rule_origin_path():
    ev = []
    app, _ = _app("observe", ev)
    c = TestClient(app)
    for _ in range(5):
        c.post("/mcp/call", content="{}", headers={"origin": "https://evil.example"})
    assert len(ev) == 1                                   # one event per minute per (rule, origin, path)
    assert app.status()["counts"]["auth.would_refuse:origin"] == 5
