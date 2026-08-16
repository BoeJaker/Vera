"""Critical-tier tests for the closed-loop orchestrator decision core (M7 Phase B).

The orchestrator must (1) NEVER act unless autonomous mode is engaged, (2) only
dispatch a genuinely-ready, unclaimed, not-already-dispatched item, (3) leave items
another agent holds alone (so two machines on one board don't double-work), and
(4) NEVER auto-run v7 / any v1-v8 loop — it idles when the board is dry. Pure, no IO.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import orchestrator_core as oc  # noqa: E402


def _item(id, lane, agent="", pipeline="", title="t"):
    return {"id": id, "lane": lane, "agent": agent, "pipeline": pipeline, "title": title}


# ── the hard interlock ───────────────────────────────────────────────────────

def test_blocked_when_not_engaged():
    d = oc.next_action([_item("a", "ready")], engaged=False)
    assert d["action"] == "blocked"
    assert "not engaged" in d["reason"]


# ── dispatch selection ───────────────────────────────────────────────────────

def test_dispatches_a_ready_unclaimed_item():
    d = oc.next_action([_item("a", "ready")], engaged=True)
    assert d["action"] == "dispatch" and d["item"] == "a"


def test_inbox_lane_is_dispatchable_too():
    assert oc.is_dispatchable(_item("a", "inbox"), "claude_code") is True


def test_item_with_a_pipeline_is_not_dispatchable():
    assert oc.is_dispatchable(_item("a", "ready", pipeline="p1"), "claude_code") is False


def test_item_claimed_by_another_agent_is_left_alone():
    assert oc.is_dispatchable(_item("a", "ready", agent="other-bot"), "claude_code") is False


def test_item_claimed_by_me_is_dispatchable():
    assert oc.is_dispatchable(_item("a", "ready", agent="claude_code"), "claude_code") is True


def test_busy_and_done_lanes_are_not_dispatchable():
    for lane in ("in_progress", "review", "needs_review", "done", "dropped", "blocked"):
        assert oc.is_dispatchable(_item("a", lane), "claude_code") is False, lane


def test_pick_returns_first_dispatchable_respecting_order():
    items = [_item("busy", "in_progress"), _item("mine", "ready"), _item("also", "inbox")]
    assert oc.pick_dispatchable(items, "claude_code")["id"] == "mine"


def test_pick_returns_none_when_nothing_dispatchable():
    items = [_item("x", "done"), _item("y", "in_progress"), _item("z", "ready", agent="bot")]
    assert oc.pick_dispatchable(items, "claude_code") is None


# ── empty / dry board: idle, NEVER v7 ────────────────────────────────────────

def test_idle_when_nothing_dispatchable_and_work_in_flight():
    d = oc.next_action([_item("x", "in_progress")], engaged=True)
    assert d["action"] == "idle"
    assert "in flight" in d["reason"]


def test_idle_when_board_is_completely_dry_never_v7():
    d = oc.next_action([], engaged=True)
    assert d["action"] == "idle"          # NOT 'optimize_v7'
    assert "v7" in d["reason"]             # explicitly notes v7 is disabled


def test_no_action_path_ever_returns_a_v7_or_loop_action():
    # exhaustive-ish: across board states, the only actions are blocked/dispatch/idle
    boards = [[], [_item("a", "ready")], [_item("b", "in_progress")],
              [_item("c", "done"), _item("d", "ready", agent="bot")]]
    for engaged in (True, False):
        for b in boards:
            a = oc.next_action(b, engaged)["action"]
            assert a in ("blocked", "dispatch", "idle"), (engaged, b, a)
