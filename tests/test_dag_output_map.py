"""S12 / D13-1 (output_map part): a node's output_map puts fields of its result into the state, on every route that
runs stored DAGs.

The planner teaches `["dag.store_list", null, null, null, {"items": "dags"}]`, and so do the sys-dag-creation skill and
the Flow Builder, but no executor read output_map: an output_map-only node's result was thrown away and the next node
got nothing. Caps are faked in the registry; runs in the gate container."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as co  # noqa: E402

GRAPH = [
    ["t.list", None, None, None, {"items": "dags", "first": "dags.0.name", "nope": "missing.field"}],
    ["t.count", "n"],
]


@pytest.fixture
def caps(monkeypatch):
    async def t_list(**kw):
        return {"dags": [{"name": "alpha"}, {"name": "beta"}], "count": 2}

    async def t_count(items=None, **kw):
        return len(items or [])
    monkeypatch.setitem(co.CAPABILITY_REGISTRY, "t.list", {"func": t_list, "schema": {"properties": {}}})
    monkeypatch.setitem(co.CAPABILITY_REGISTRY, "t.count", {"func": t_count, "schema": {"properties": {"items": {}}}})


def _check(state):
    assert state["items"] == [{"name": "alpha"}, {"name": "beta"}]
    assert state["first"] == "alpha"
    assert "nope" not in state                       # a missing field is left unset, not None
    assert state["n"] == 2                            # the next node got the mapped key


def test_run_graph(caps):
    _check(asyncio.run(co.run_graph([list(n) for n in GRAPH], {})))


def test_supervised_run_graph(caps):
    _check(asyncio.run(co.supervised_run_graph([list(n) for n in GRAPH], {}, supervision_every=1000)))


def test_the_streamed_route(caps):
    async def run():
        state = {}
        async for _ev, _data in co._hitl_run_graph_stream([list(n) for n in GRAPH], state, False, 0):
            pass
        return state
    _check(asyncio.run(run()))


def test_parallel_branches_map_too(caps):
    state = asyncio.run(co.run_graph([[["t.list", None, None, None, {"items": "dags"}]], ["t.count", "n"]], {}))
    assert state["n"] == 2


def test_apply_output_map_reads_paths():
    st = {}
    missing = co.apply_output_map(["c", None, None, None, {"a": "x.y", "b": "l.-1", "c": "l.9"}],
                                  {"x": {"y": 1}, "l": [1, 2, 3]}, st)
    assert st == {"a": 1, "b": 3} and missing == ["l.9"]
    assert co.apply_output_map(["c", "o"], {"x": 1}, st) == []          # no output_map: nothing happens
