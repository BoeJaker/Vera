"""A cancelled browser run must stop, and must never be scored as a result.

Observed 2026-08-30: a census run was cancelled and its operator generation ran
on for 1211s against the shared GPU. The agentic loop has had a cooperative
cancel check for a long time (test_gate_cancel_release pins its half); the
operator had none at all - nothing told the browser loop to stop, so it kept
observing, kept thinking, and kept issuing LLM calls.

Two properties, and the second is the subtler one:

  1. the loop stops BEFORE its next step, so a cancel does not buy one more
     browser action and one more LLM call;
  2. a cancelled run is not an OUTCOME. It says nothing about how well the
     operator performed - it measures whoever cancelled it. A cancelled run
     that happens to have zero errors would otherwise classify as `finished`,
     which is the worst available lie: a run that was stopped, reported as one
     that succeeded.

Pure: no Redis, no browser, no app import - the loop is driven with stub
observe/think/act functions.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import operator_loop as OL          # noqa: E402
from vera.operator import safety as _safety            # noqa: E402
from vera.census.operator_census_core import (          # noqa: E402
    CANCELLED, FINISHED, INCOMPLETE, compare_runs, outcome_of, summarise_run,
)
from vera.operator.operator_trace_core import digest_events, summarise_run as trace_summary  # noqa: E402


class _Obs:
    url = "http://example.com"
    screenshot_path = ""


class _Session:
    """The minimum the loop touches on a session."""
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def _stubs(actions):
    """observe/think/act that walk a fixed script of decisions."""
    seq = list(actions)
    calls = {"observe": 0, "think": 0, "act": 0}

    async def observe(_session, _i):
        calls["observe"] += 1
        return _Obs()

    async def think(_goal, _obs, _hist, _canvas):
        calls["think"] += 1
        return seq[min(calls["think"] - 1, len(seq) - 1)]

    async def act(*_a, **_k):
        calls["act"] += 1
        return {"ok": True}

    return observe, think, act, calls


def _run(**kw):
    """Drive the real loop with stubs. The policy must PERMIT the action, or the
    loop stops at its safety gate and never reaches the behaviour under test."""
    observe, think, act, calls = _stubs(kw.pop("actions", [{"action": "click",
                                                            "args": {}, "thought": "t"}]))
    kw.setdefault("policy", _safety.SafetyPolicy(allowlist=["example.com"],
                                                 allow_destructive=True, confirm=True))
    res = asyncio.run(OL.run_loop("do the thing", _Session(), observe_fn=observe,
                                  think_fn=think, act_fn=act, **kw))
    return res, calls


# ── the loop stops, and stops EARLY ────────────────────────────────────────
def test_a_cancelled_run_stops_and_says_so():
    async def cancelled():
        return True
    res, calls = _run(max_steps=10, should_cancel=cancelled)
    assert res["reason"] == "cancelled"


def test_cancel_is_checked_before_observing_or_thinking():
    """The point of checking first: a cancel must not buy one more browser
    action and one more LLM call."""
    async def cancelled():
        return True
    res, calls = _run(max_steps=10, should_cancel=cancelled)
    assert calls["observe"] == 0 and calls["think"] == 0 and calls["act"] == 0


def test_cancelling_partway_stops_at_that_step():
    state = {"n": 0}

    async def after_two():
        state["n"] += 1
        return state["n"] > 2
    res, calls = _run(max_steps=10, should_cancel=after_two)
    assert res["reason"] == "cancelled"
    assert calls["think"] == 2, "should have run exactly the steps before the cancel"


def test_no_cancel_callback_behaves_exactly_as_before():
    res, calls = _run(max_steps=3)
    assert res["reason"] != "cancelled" and calls["think"] == 3


def test_a_cancel_check_that_raises_does_not_kill_a_healthy_run():
    """Worst case must be the OLD behaviour, never a run stopped by a Redis blip."""
    async def broken():
        raise RuntimeError("redis down")
    res, calls = _run(max_steps=3, should_cancel=broken)
    assert res["reason"] != "cancelled" and calls["think"] == 3


def test_the_cancelled_step_is_recorded_so_the_trace_shows_why_it_stopped():
    async def cancelled():
        return True
    res, _ = _run(max_steps=5, should_cancel=cancelled)
    assert res["steps"] and res["steps"][-1]["phase"] == "cancelled"


# ── a cancelled run is not a result ────────────────────────────────────────
def _events(reason):
    return [{"type": "operator.run", "stage": "start", "run_id": "r1",
             "goal": "g", "ts": "2026-08-30T10:00:00Z"},
            {"type": "operator.step", "i": 1, "phase": "act", "action": "click"},
            {"type": "operator.run", "stage": "done", "run_id": "r1",
             "reason": reason, "steps": 1, "ts": "2026-08-30T10:01:00Z"}]


def test_the_trace_marks_a_cancelled_run_as_cancelled():
    assert digest_events(_events("cancelled"))["counters"]["cancelled"] is True
    assert digest_events(_events("done"))["counters"]["cancelled"] is False


def test_a_cancelled_run_with_no_errors_is_not_classified_as_finished():
    """The worst available lie: a run that was STOPPED, reported as one that
    succeeded. It has zero errors and a completion event, so without an explicit
    check it would classify as `finished`."""
    rec = trace_summary("r1", _events("cancelled"))
    assert rec["cancelled"] is True and rec["errors"] == 0 and rec["completed"] is True
    assert outcome_of(rec) == CANCELLED
    assert outcome_of(trace_summary("r1", _events("done"))) == FINISHED


def test_cancelled_ranks_with_incomplete_so_it_can_never_read_as_progress():
    from vera.census import operator_census_core as m
    assert m._RANK[CANCELLED] == m._RANK[INCOMPLETE]
    out = compare_runs([{"id": "g", "completed": False}],
                       [{"id": "g", "completed": True, "cancelled": True}])
    assert out[0]["outcome_change"] == "held"      # not an improvement


def test_a_run_containing_a_cancelled_goal_is_not_comparable():
    s = summarise_run("r", [{"id": "a", "completed": True},
                            {"id": "b", "completed": True, "cancelled": True}])
    assert s["cancelled"] == 1 and s["comparable"] is False


def test_a_cancelled_goal_says_it_measures_the_canceller():
    out = compare_runs([{"id": "g", "completed": True}],
                       [{"id": "g", "completed": True, "cancelled": True}])
    assert "not the operator" in out[0]["note"]
