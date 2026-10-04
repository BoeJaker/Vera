"""A gate whose pipeline merged while it was still running (a forced promote) goes "moot":
it stops holding restarting releases and is not shown as queued (4 Oct 2026: release
0340fcd1 waited on the leftover gate of a36b3baf, which had already been merged)."""
import inspect

import pytest

from vera import activity_health_core as A
from vera.evolve import evolve_capabilities as evolve

pytestmark = pytest.mark.critical


def test_moot_gate_is_not_running_and_stays_moot():
    evolve._GATES_ACTIVE["p1"] = {"id": "p1", "branch": "b", "state": "running", "since": "x"}
    try:
        evolve._moot_gate("p1")
        assert evolve._GATES_ACTIVE["p1"]["state"] == "moot"
        evolve._gate_running("p1")              # a later "running" write must not revive it
        assert evolve._GATES_ACTIVE["p1"]["state"] == "moot"
        evolve._moot_gate("absent")             # unknown ids are ignored
        evolve._gate_running("absent")
        assert "absent" not in evolve._GATES_ACTIVE
    finally:
        evolve._GATES_ACTIVE.pop("p1", None)


def test_queued_gate_becomes_running():
    evolve._GATES_ACTIVE["p2"] = {"id": "p2", "branch": "b", "state": "queued", "since": "x"}
    try:
        evolve._gate_running("p2")
        assert evolve._GATES_ACTIVE["p2"]["state"] == "running"
    finally:
        evolve._GATES_ACTIVE.pop("p2", None)


def test_code_merge_moots_the_gate_and_tier_uses_the_guard():
    src = inspect.getsource(evolve)
    assert "_moot_gate(rec.get(\"id\"))" in src
    tier = inspect.getsource(evolve._critical_tier_queued)
    assert '["state"] = "running"' not in tier and "_gate_running(" in tier


def test_health_summary_leaves_moot_gates_out_of_running_and_queued():
    s = A.summarise([], [{"id": "a", "branch": "merged", "state": "moot"},
                         {"id": "b", "branch": "next", "state": "queued"}], [])
    assert "gate running" not in s["summary"]
    assert "1 gate queued" in s["summary"]
