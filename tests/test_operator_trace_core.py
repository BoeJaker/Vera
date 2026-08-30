"""A browser run must be readable back, and must not be trusted on its own word.

operator.run drives a real browser through observe -> think -> act, and it was
the only major subsystem in Vera you could not read back: 15 event types against
the agentic loop's 288, and none of them persisted.

That is not tidiness. Across census runs 12 and 13, EVERY goal with an
operator.run step consumed its entire 25-minute wall cap, while every goal
without one finished in under seven minutes - and nothing recorded what the
browser did with that time. build-browser-verified spent 15 cycles on step 1
alone. The one subsystem reliably eating the budget was the one with no
instrumentation.

The sharpest thing this digest has to do is refuse to launder the run's own
claim: L8 was landed because an operator run could report success after hitting
its step ceiling, so `reason` is a claim to check, never a result.

Pure: no Redis, no browser, no app import.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator.operator_trace_core import (          # noqa: E402
    digest_events, summarise_run,
)


def _start(goal="verify the error appears", target="url", ts="2026-08-30T10:00:00Z"):
    return {"type": "operator.run", "stage": "start", "run_id": "abc123",
            "goal": goal, "target": target, "ts": ts}


def _step(i, phase="act", action="click #submit", thought="try submitting",
          error="", shot="/operator/artifact?path=x.png", ts=None):
    return {"type": "operator.step", "run_id": "abc123", "i": i, "phase": phase,
            "action": action, "thought": thought, "reason": "", "error": error,
            "screenshot": shot, "ts": ts or "2026-08-30T10:00:%02dZ" % min(i, 59)}


def _done(reason="done", steps=3, ts="2026-08-30T10:02:00Z"):
    return {"type": "operator.run", "stage": "done", "run_id": "abc123",
            "reason": reason, "steps": steps, "ts": ts}


# ── the basic read-back ─────────────────────────────────────────────────────
def test_a_run_can_be_read_back_at_all():
    d = digest_events([_start(), _step(1, "observe"), _step(2, "think"),
                       _step(3, "act"), _done()])
    assert d["run"]["goal"] == "verify the error appears"
    assert d["run"]["target"] == "url"
    assert d["counters"]["steps"] == 3
    assert d["counters"]["phases"] == {"observe": 1, "think": 1, "act": 1}
    assert d["duration_s"] == 120.0


def test_each_step_keeps_what_it_was_thinking_and_doing():
    """Action + thought are what tell you whether it was aimed at the goal."""
    d = digest_events([_start(), _step(1, "act", "click #submit", "submit the bad email")])
    s = d["steps"][0]
    assert s["action"] == "click #submit"
    assert s["thought"] == "submit the bad email"
    assert s["screenshot"].endswith("x.png")


# ── the L8 property: its own word is a claim, not a result ─────────────────
def test_running_out_of_steps_is_flagged_next_to_the_runs_own_reason():
    """An operator run reporting success after hitting its step ceiling is a real
    observed failure (L8), so the ceiling must sit beside the claim."""
    d = digest_events([_start(), _step(1), _done(reason="max_steps", steps=1)])
    assert d["counters"]["ceiling_hit"] is True
    assert d["run"]["reason"] == "max_steps"
    assert any("NOT evidence the goal was reached" in w for w in d["warnings"])


def test_a_genuine_completion_is_not_flagged_as_a_ceiling():
    d = digest_events([_start(), _step(1), _done(reason="done")])
    assert d["counters"]["ceiling_hit"] is False
    assert not any("ran out of steps" in w for w in d["warnings"])


def test_the_digest_offers_no_verdict_of_its_own():
    """It assembles evidence. A passed/success field here would relaunder exactly
    the claim L8 exists to distrust."""
    d = digest_events([_start(), _step(1), _done()])
    for banned in ("passed", "success", "verdict", "achieved"):
        assert banned not in d and banned not in d["counters"]


# ── the failure shapes worth naming ────────────────────────────────────────
def test_the_same_action_repeated_is_the_browser_thrash_signature():
    ev = [_start()] + [_step(i, "act", "click #submit") for i in range(1, 6)] + [_done()]
    d = digest_events(ev)
    assert d["counters"]["repeated_actions"] == 1
    assert any("attempted 5x" in w and "click #submit" in w for w in d["warnings"])


def test_two_attempts_is_not_yet_thrash():
    ev = [_start()] + [_step(i, "act", "click #submit") for i in (1, 2)] + [_done()]
    assert digest_events(ev)["counters"]["repeated_actions"] == 0


def test_errored_steps_are_counted_and_named():
    d = digest_events([_start(), _step(1, "act", "click #x", error="no such element"),
                       _step(2, "act", "click #y"), _done()])
    assert d["counters"]["errors"] == 1
    assert any("step 1" in w and "no such element" in w for w in d["warnings"])


def test_a_run_with_no_screenshots_has_nothing_to_verify_against():
    d = digest_events([_start(), _step(1, shot=""), _done()])
    assert d["counters"]["screenshots"] == 0
    assert any("nothing visual to verify" in w for w in d["warnings"])


def test_a_run_that_never_acted_says_so():
    d = digest_events([_start(), _done(steps=0)])
    assert d["counters"]["steps"] == 0
    assert any("never got as far as acting" in w for w in d["warnings"])


# ── an unfinished run must be visible as unfinished ────────────────────────
def test_a_run_with_no_done_event_is_not_reported_as_complete():
    """Observed: a cancelled census run left an operator.think generation running
    1211s against the shared GPU. A run whose `done` never arrived was cancelled
    or its process died holding it - that must not look like a normal run."""
    s = summarise_run("abc123", [_start(), _step(1)])
    assert s["completed"] is False
    assert s["duration_s"] is None


def test_a_finished_run_summarises_for_a_list_view():
    s = summarise_run("abc123", [_start(), _step(1), _step(2), _done(reason="done", steps=2)])
    assert s["completed"] is True and s["steps"] == 2
    assert s["reason"] == "done" and s["ceiling_hit"] is False
    assert s["goal"] == "verify the error appears"


def test_summarise_falls_back_to_the_supplied_run_id():
    s = summarise_run("fallback-id", [_step(1)])
    assert s["run_id"] == "fallback-id"


# ── robustness: reading a run must never be what breaks ────────────────────
def test_malformed_and_empty_event_lists_do_not_explode():
    assert digest_events([])["counters"]["steps"] == 0
    d = digest_events([None, {}, {"type": "operator.step"}, "nonsense"])
    assert d["counters"]["steps"] == 1        # the typed one, with empty fields


def test_unparseable_timestamps_give_no_duration_rather_than_raising():
    d = digest_events([_start(ts="not-a-time"), _done(ts="also-not")])
    assert d["duration_s"] is None


def test_long_text_is_clipped_for_a_panel():
    d = digest_events([_start(), _step(1, thought="t" * 900)])
    assert d["steps"][0]["thought"].endswith("…")
    assert len(d["steps"][0]["thought"]) <= 301
