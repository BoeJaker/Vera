"""The terminal WebSocket routes refuse a foreign Origin (4403) and a missing,
wrong or reused ticket (4401); a ticket from conn.open or remote.term.ticket
opens the socket once.

The routes run on a bare FastAPI app (no Vera lifespan); the shell bridge is
faked. Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import Vera.vera.remote.remote_capabilities as rc  # noqa: E402  (fails loudly, never skips)
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY  # noqa: E402

OWN = {"origin": "http://testserver"}


def raw(name):
    return CAPABILITY_REGISTRY[name]["raw"]


@pytest.fixture
def client(monkeypatch):
    events = []

    async def fake_emit(evt):
        events.append(evt)

    async def fake_bridge(ws, *, ssh_host_id, command="", cols=80, rows=24, banner=""):
        await ws.send_bytes(f"shell on {ssh_host_id}".encode())
        await ws.close()

    monkeypatch.setattr(rc, "emit_event", fake_emit)
    monkeypatch.setattr(rc, "_ssh_pty_bridge", fake_bridge)
    monkeypatch.setattr(rc, "_docker_mod", lambda: None)      # docker route then answers 4404
    app = FastAPI()
    app.add_api_websocket_route("/remote/ssh/term/ws/{host_id}", rc.remote_ssh_term_ws)
    app.add_api_websocket_route("/remote/docker/term/ws/{host_id}/{container}", rc.remote_docker_term_ws)
    c = TestClient(app)
    c.events = events
    return c


def close_code(client, path, headers=None):
    """Open ``path``; return (first bytes frame, close code)."""
    with client.websocket_connect(path, headers=headers or {}) as ws:
        first = b""
        try:
            first = ws.receive_bytes()
            ws.receive_bytes()
        except WebSocketDisconnect as e:
            return first, e.code
    return first, None


def test_foreign_origin_is_refused_with_4403_even_with_a_ticket(client):
    t = rc.TICKETS.mint("ssh", "h1")
    first, code = close_code(client, f"/remote/ssh/term/ws/h1?ticket={t}", {"origin": "https://evil.example"})
    assert code == 4403 and b"open the terminal from Vera" in first
    first, code = close_code(client, "/remote/docker/term/ws/local/web", {"origin": "https://evil.example"})
    assert code == 4403
    assert any(e.get("type") == "remote.term.refused" and e.get("code") == 4403 for e in client.events)


def test_no_ticket_is_refused_with_4401(client):
    for path in ("/remote/ssh/term/ws/h1?shell=", "/remote/docker/term/ws/local/web?shell=sh"):
        first, code = close_code(client, path, OWN)
        assert code == 4401, path
        assert b"no valid ticket" in first
    _, code = close_code(client, "/remote/ssh/term/ws/h1")      # no Origin at all: still needs a ticket
    assert code == 4401


def test_a_ticket_for_another_host_is_refused(client):
    t = rc.TICKETS.mint("ssh", "other")
    _, code = close_code(client, f"/remote/ssh/term/ws/h1?ticket={t}", OWN)
    assert code == 4401
    t = rc.TICKETS.mint("docker", "local", "db")
    _, code = close_code(client, f"/remote/docker/term/ws/local/web?ticket={t}", OWN)
    assert code == 4401


def test_conn_open_ticket_opens_the_shell_once(client):
    d = asyncio.run(raw("conn.open")(kind="ssh", ssh_host_id="h1"))
    assert d["ok"] and "ticket=" in d["ws_path"]
    first, _ = close_code(client, d["ws_path"], OWN)
    assert first == b"shell on h1"
    _, code = close_code(client, d["ws_path"], OWN)
    assert code == 4401                                          # spent


def test_remote_term_ticket_mints_for_a_saved_path(client):
    saved = "/remote/docker/term/ws/local/web?shell=sh&ticket=stale"
    d = asyncio.run(raw("remote.term.ticket")(ws_path=saved))
    assert d["ok"] and d["expires_in"] == 90 and "stale" not in d["ws_path"]
    _, code = close_code(client, d["ws_path"], OWN)
    assert code == 4404                                          # past the gate: unknown docker host (faked)
    bad = asyncio.run(raw("remote.term.ticket")(ws_path="/ws/mcp"))
    assert not bad["ok"]


def test_conn_open_docker_path_carries_a_ticket_for_that_container():
    d = asyncio.run(raw("conn.open")(kind="docker", docker_host_id="local", container="web app"))
    assert d["ok"]
    from Vera.vera.remote import term_tickets as tt
    assert tt.target_of_path(d["ws_path"]) == ("docker", "local", "web app")
    ticket = d["ws_path"].split("ticket=")[1]
    assert rc.TICKETS.consume(ticket, "docker", "local", "web app") == (True, "")
