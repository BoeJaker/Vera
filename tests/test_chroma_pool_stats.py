"""memory.chroma.pool reports where a process's Chroma connections live (RF-82)."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory  # noqa: E402

pytestmark = pytest.mark.critical


class _Conn:
    def __init__(self, idle, port=8008):
        self._idle, self._origin = idle, types.SimpleNamespace(port=port)

    def is_idle(self):
        return self._idle

    def is_closed(self):
        return False


def _client(conns):
    pool = types.SimpleNamespace(connections=conns, _max_connections=None, _max_keepalive_connections=40)
    sess = types.SimpleNamespace(_transport=types.SimpleNamespace(_pool=pool))
    return types.SimpleNamespace(_server=types.SimpleNamespace(_session=sess))


def test_known_clients_are_counted(monkeypatch):
    backend = memory.ChromaBackend()
    backend._client = _client([_Conn(True), _Conn(True), _Conn(False)])
    monkeypatch.setitem(memory.MEMORY._backends, "chroma", backend)
    out = memory.chroma_pool_stats()
    m = out["clients"]["memory"]
    assert (m["connections"], m["idle"], m["closed"], m["max_keepalive"]) == (3, 2, 0, 40)


def test_a_broken_client_is_reported_not_raised(monkeypatch):
    backend = memory.ChromaBackend()
    backend._client = object()
    monkeypatch.setitem(memory.MEMORY._backends, "chroma", backend)
    assert "error" in memory.chroma_pool_stats()["clients"]["memory"]


def test_deep_finds_httpx_clients_talking_to_chroma():
    import httpx
    c = httpx.Client()
    out = memory.chroma_pool_stats(deep=True)
    assert "httpx_client_count" in out and "connections_total" in out
    c.close()


def test_deep_names_the_holder_of_a_leaked_client():
    import httpx

    class Leaker:
        def __init__(self):
            self.session = httpx.Client(headers={"user-agent": "probe"})

    keep = Leaker()
    holders = memory._holders(keep.session)
    assert holders and holders[0].endswith("Leaker")
    keep.session.close()
