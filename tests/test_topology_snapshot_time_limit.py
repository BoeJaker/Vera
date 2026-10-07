"""One slow source can't hold up the dashboard's subsystem snapshot.

/topology/snapshot and dash.health.summary share one fan-out of twelve caps.
It used to wait for the slowest, so the topology map took 10.8 s on every call
(bleeding-edge mirror, 2026-09-30). Each source now has
SNAPSHOT_SOURCE_TIMEOUT_S; a late one keeps running in the background, the
snapshot uses its last answer and names it under "stale", and a source is
never started twice at once.
"""
import asyncio

import pytest

pytestmark = pytest.mark.critical


@pytest.fixture
def orch(monkeypatch):
    from vera import capability_orchestration as O
    monkeypatch.setattr(O, "_SNAPSHOT_LAST", {})
    monkeypatch.setattr(O, "_SNAPSHOT_INFLIGHT", {})
    monkeypatch.setattr(O, "SNAPSHOT_SOURCE_TIMEOUT_S", 0.05)
    return O


def _fake_caps(monkeypatch, O, slow, calls, gate):
    async def cap_call(name, **_kw):
        calls.append(name)
        if name == slow:
            await gate.wait()
            return {"answer": f"{name} fresh"}
        return {"answer": name}
    monkeypatch.setattr(O, "_cap_call", cap_call)


def test_a_slow_source_does_not_hold_the_snapshot(orch, monkeypatch):
    calls = []

    async def run():
        gate = asyncio.Event()
        _fake_caps(monkeypatch, orch, "obs.node_temps", calls, gate)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        snap = await orch._gather_subsystem_snapshot()
        elapsed = loop.time() - t0
        gate.set()
        await asyncio.sleep(0)
        return snap, elapsed

    snap, elapsed = asyncio.run(run())
    assert elapsed < 1.0
    assert snap["stale"] == ["temps"]
    assert "still running" in snap["temps"]["error"]
    assert snap["sysmon"] == {"answer": "sysmon.status"}


def test_a_late_source_serves_its_last_answer_and_is_not_restarted(orch, monkeypatch):
    calls = []

    async def run():
        gate = asyncio.Event()
        _fake_caps(monkeypatch, orch, "obs.node_temps", calls, gate)
        gate.set()
        first = await orch._gather_subsystem_snapshot()   # fills the last answer
        gate.clear()
        second = await orch._gather_subsystem_snapshot()  # temps now slow
        third = await orch._gather_subsystem_snapshot()   # still the same call in flight
        gate.set()
        await asyncio.sleep(0)
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first["stale"] == []
    assert second["stale"] == ["temps"]
    assert second["temps"] == {"answer": "obs.node_temps fresh"}
    assert third["temps"] == {"answer": "obs.node_temps fresh"}
    assert calls.count("obs.node_temps") == 2, "a running source must not be started again"


def test_topology_snapshot_reports_what_is_stale(orch, monkeypatch):
    async def gather():
        return {k: {} for k, _c, _kw in orch._SNAPSHOT_SOURCES} | {"stale": ["temps"]}
    monkeypatch.setattr(orch, "_gather_subsystem_snapshot", gather)
    out = asyncio.run(orch.topology_snapshot())
    assert out["stale"] == ["temps"]
    assert out["nodes"][0]["id"] == "hub"
