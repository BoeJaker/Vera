"""agent.list must not turn another feature's hash under vera:agents: into an agent (18 Oct 2026: vera:agents:contacts and
vera:agents:work came out as one blank, undeletable agent with a new random id on every read)."""
import asyncio
import importlib


def _mod():
    for name in ("Vera.vera.agents.agents", "vera.agents.agents"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    raise ImportError("agents module")


class _R:
    def __init__(self, data):
        self.data = data
    async def keys(self, pat):
        return [k.encode() for k in self.data]
    async def hgetall(self, k):
        k = k.decode() if isinstance(k, bytes) else k
        return {a.encode(): b.encode() for a, b in self.data[k].items()}


def test_foreign_hashes_are_not_agents(monkeypatch):
    m = _mod()
    data = {
        "vera:agents:1b2c3d4e-0000-4000-8000-000000000001": {"id": "1b2c3d4e-0000-4000-8000-000000000001", "name": "helper", "label": "Helper"},
        "vera:agents:contacts": {"alice": "{}", "bob": "{}"},
        "vera:agents:work": {"job1": "{}"},
    }
    monkeypatch.setattr(m, "_redis", lambda: _R(data))
    monkeypatch.setattr(m, "_pg", lambda: None)
    reg = m.AgentRegistry() if callable(getattr(m, "AgentRegistry", None)) else m.AGENT_REGISTRY
    out = asyncio.get_event_loop().run_until_complete(reg.list_all()) if False else asyncio.new_event_loop().run_until_complete(reg.list_all())
    names = [r.name for r in out]
    assert names == ["helper"], names
