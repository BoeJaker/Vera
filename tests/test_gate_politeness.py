"""The suite must yield the GPU the way the census harness does.

Step 2 of flattening the census into the Loop Lab suite. The suite runner
already bounds a task (timeout_s), kills an idle run (run_idle_timeout_s) and
cancels cleanly - it is not careless. What it has never had is POLITENESS: it
starts the next task regardless of who else is using the box.

The ollama GPU gate is capacity 1. A suite started while another agent, a dream
cycle or a chat is mid-generation does not run alongside it - it QUEUES, and the
waiting is charged to the task's own timeout_s. The task then reads as slow or
timed out when nothing was wrong with it. The census harness has waited for the
box since its early runs, and it is the one piece of its behaviour that cannot
be dropped in the migration.

Pure: the caller supplies the gate reading and the running-loop count.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve import gate_politeness as GP               # noqa: E402


def _gate(*nodes):
    return {"nodes": list(nodes)}


FREE = _gate({"node": "gpu-250", "gated": True, "capacity": 1, "held": 0, "owners": []},
             {"node": "cpu-246", "gated": False, "capacity": 0})
HELD = _gate({"node": "gpu-250", "gated": True, "capacity": 1, "held": 1,
              "owners": ["LLM:1336228:196e2753"]},
             {"node": "cpu-246", "gated": False, "capacity": 0})


# ── when the box is busy ────────────────────────────────────────────────────
def test_a_held_gate_is_busy_and_names_the_owner():
    """The harness prints the owner so a wait is attributable, not mysterious."""
    r = GP.busy_reason(HELD, 0)
    assert r
    assert "196e2753" in r


def test_a_running_loop_is_busy_even_with_a_free_gate():
    """A loop between generations holds no lease but is about to take one."""
    assert GP.busy_reason(FREE, 1) == "another loop is running"


def test_a_free_box_is_free():
    assert GP.busy_reason(FREE, 0) == ""


# ── what must NOT count as busy ─────────────────────────────────────────────
def test_an_ungated_node_never_counts():
    """The CPU nodes are ungated - they have no capacity limit to contend for,
    so a busy one is not a reason to hold up a GPU task."""
    busy_cpu = _gate({"node": "cpu-246", "gated": False, "capacity": 0, "held": 3})
    assert GP.busy_reason(busy_cpu, 0) == ""


def test_a_gated_node_with_nothing_held_is_free():
    assert GP.busy_reason(_gate({"node": "gpu", "gated": True, "held": 0}), 0) == ""


def test_a_missing_or_malformed_gate_reads_as_free():
    """An unreadable gate must not stall a benchmark forever."""
    for bad in (None, {}, {"nodes": None}, {"nodes": ["nonsense"]},
                _gate({"gated": True, "held": "x"})):
        assert GP.busy_reason(bad, 0) == ""


def test_a_malformed_loop_count_reads_as_free():
    assert GP.busy_reason(FREE, None) == ""
    assert GP.busy_reason(FREE, "x") == ""


# ── which tasks wait ────────────────────────────────────────────────────────
def test_loop_tasks_wait():
    assert GP.needs_free_box("loop") is True
    assert GP.needs_free_box("sim") is True


def test_cap_smoke_tests_do_not_wait():
    """The suite deliberately runs fast cap tests first so the counter moves
    immediately. Making them queue behind a 20-minute loop would destroy that."""
    assert GP.needs_free_box("cap") is False


def test_an_unknown_type_waits():
    """Default to the cautious side: an unrecognised type is treated as
    GPU-bound rather than allowed to barge in."""
    assert GP.needs_free_box("") is True
    assert GP.needs_free_box(None) is True


# ── the ceiling ─────────────────────────────────────────────────────────────
def test_there_is_a_ceiling_on_waiting():
    """A benchmark that silently never runs is worse than one that runs
    contended and is labelled as such."""
    assert GP.DEFAULT_MAX_WAIT_S > 0
    note = GP.gave_up(GP.DEFAULT_MAX_WAIT_S)
    assert "running anyway" in note
    assert "contended" in note, "the result must be marked as not comparable"


def test_a_wait_is_described_so_it_is_not_mistaken_for_a_hang():
    d = GP.describe_wait("gate held by ['x']", 90)
    assert "gate held by" in d and "90" in d


def test_the_poll_gap_is_sane():
    assert 5 <= GP.POLL_SECONDS <= 120
