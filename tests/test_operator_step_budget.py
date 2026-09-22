"""1,109 seconds of thinking to verify a page that was already correct.

Census run61, build-browser-verified: 1,694s wall, quality 1.0 on every check,
ZERO model reloads - and 29 `operator.think` calls at 35-50s each, 17.5 tok/s.
The thinks are not slow any more. There are simply too many of them, and the
step still ended:

    operator.run FAILED - time_budget: stopped after 504s (budget 480s)
    having taken 11 step(s) without reaching the goal

after which the executor called operator.run AGAIN. `operator_budget` caps a
single call at 480s and deliberately refuses to let a caller shrink that (every
caller-supplied value ever observed came from the model and every one was too
small). Nothing capped how many times one step may call it.

Pure: a tally dict in, a decision out. No clock of its own.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.operator import operator_step_budget as B     # noqa: E402

pytestmark = pytest.mark.critical

STOP = ("time_budget: stopped after 504s (budget 480s) having taken 11 step(s) "
        "without reaching the goal")


# -- the census case ---------------------------------------------------------
def test_a_second_browser_run_in_one_step_is_refused():
    """Exactly run61: one run spends its own 480s cap, the step asks again."""
    tally = {}
    B.record(tally, "step-1", "operator.run", 504.0)
    assert not B.exhausted(tally, "step-1", "operator.run"), "the first run must be allowed"
    B.record(tally, "step-1", "operator.run", 120.0)
    assert B.exhausted(tally, "step-1", "operator.run")


def test_the_refusal_says_what_to_do_instead():
    msg = B.describe("operator.run", 624.0, last_stop=STOP)
    assert "already spent 624s" in msg and "allowance is 600s" in msg
    # it must not merely say no
    assert "emit `done`" in msg and "belongs in the FILE" in msg
    assert "time_budget" in msg          # quotes why the last run stopped


def test_the_allowance_is_per_step_not_per_run():
    tally = {}
    B.record(tally, "step-1", "operator.run", 700.0)
    assert B.exhausted(tally, "step-1", "operator.run")
    assert not B.exhausted(tally, "step-2", "operator.run")
    assert B.spent(tally, "step-2") == 0.0


def test_only_browser_calls_draw_on_it():
    """`operator.think` is what the browser calls spend their time ON -
    charging both would count every second twice."""
    tally = {}
    for tool in ("code.edit", "exec.bash.run", "operator.think", "web.fetch"):
        B.record(tally, "step-1", tool, 900.0)
        assert not B.exhausted(tally, "step-1", tool)
    assert tally == {}
    assert B.is_browser_call("operator.run")
    assert B.is_browser_call("browser.navigate")
    assert not B.is_browser_call("operator.think")


def test_a_cheap_run_leaves_room_for_another():
    """A verification that answers quickly must not cost the step its budget."""
    tally = {}
    for _ in range(5):
        B.record(tally, "step-1", "operator.run", 60.0)
        assert not B.exhausted(tally, "step-1", "operator.run")
    assert B.spent(tally, "step-1") == 300.0
    used, left = B.status(tally, "step-1")
    assert (used, left) == (300.0, 300.0)


# -- the edges ---------------------------------------------------------------
def test_a_zero_limit_turns_the_clock_off():
    tally = {"step-1": 10_000.0}
    assert not B.exhausted(tally, "step-1", "operator.run", limit=0)
    assert not B.exhausted(tally, "step-1", "operator.run", limit=-1)


def test_nonsense_never_raises():
    tally = {}
    B.record(tally, "step-1", "operator.run", None)
    B.record(tally, "step-1", "operator.run", "not a number")
    B.record(tally, "step-1", "operator.run", -5)
    assert tally == {}
    assert B.spent(None, "step-1") == 0.0
    assert not B.exhausted(None, "step-1", "operator.run")
    assert B.record(None, "s", "operator.run", 1.0) == {"s": 1.0}


def test_a_step_id_of_any_type_works():
    """Steps are numbered in some paths and named in others."""
    tally = {}
    B.record(tally, 3, "operator.run", 700.0)
    assert B.exhausted(tally, "3", "operator.run")
    assert B.exhausted(tally, 3, "operator.run")


def test_the_default_is_one_full_run_plus_a_look():
    """600s = one operator.run at its own 480s cap, plus a short second look,
    and a third of an 1800s goal."""
    from vera.operator import operator_budget as single
    assert B.STEP_MAX_SECONDS >= single.DEFAULT_MAX_SECONDS
    assert B.STEP_MAX_SECONDS < 1800
