"""Stop a run that is getting nowhere - not one that is merely long.

Census run 18, author-then-edit: two operator.run calls spent 1262 of a 1500s
goal budget and both ended at max_steps. The traces show six clicks on one
element, each timing out, interleaved with waits and scrolls - so no two
CONSECUTIVE actions were byte-identical and the existing repeat guard
(action+args+url) never fired.

The instrument is the PAGE, not the action: clicking a different dead element
is not progress, and treating it as progress is what let that run reach the
ceiling.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.operator.operator_progress import (        # noqa: E402
    DEFAULT_TOLERANCE, STOP_REASON, describe, page_signature, should_stop, update,
)


def _advance(sigs, tolerance=DEFAULT_TOLERANCE, action="click"):
    """Drive the state as the loop does: each signature is the page AFTER the
    previous action."""
    st = None
    for i, s in enumerate(sigs):
        st = update(st, s, last_action=(action if i else None))
        if should_stop(st, tolerance):
            return st, True
    return st, False


# --- the run-18 case --------------------------------------------------------

def test_a_page_that_never_changes_stops_the_run():
    """Six clicks on a dead element: same url, same title, same controls."""
    sig = page_signature(url="https://x/app", title="App", refs=["e7", "e9", "e15"])
    st, stopped = _advance([sig] * 10)
    assert stopped is True


def test_a_run_that_keeps_changing_the_page_is_never_stopped():
    """The explicit requirement: do not restrict turns while it is working."""
    sigs = [page_signature(url="https://x/p%d" % i, title="P%d" % i, refs=["a"])
            for i in range(60)]
    _st, stopped = _advance(sigs)
    assert stopped is False


def test_a_different_action_on_an_unchanged_page_is_not_progress():
    """click e7, wait, scroll, click e9 - all leaving the page identical."""
    sig = page_signature(url="https://x/app", title="App", refs=["e7", "e9"])
    _st, stopped = _advance([sig] * DEFAULT_TOLERANCE + [sig])
    assert stopped is True


def test_filling_a_form_is_not_a_stall():
    """Five type actions legitimately leave url/title/controls identical.
    Counting those would stop a working run - the thing this must not do."""
    sig = page_signature(url="https://x/form", title="Form", refs=["name", "email"])
    _st, stopped = _advance([sig] * 12, action="type")
    assert stopped is False


def test_scrolling_and_waiting_neither_stall_nor_reset():
    """They are not evidence either way - run 18 interleaved them with the
    clicks that were the real problem, and resetting on them would have made
    the guard unfireable."""
    sig = page_signature(url="u", title="t", refs=["a"])
    st = None
    for act in ("click", "wait", "click", "scroll", "click"):
        st = update(st, sig, last_action=act)
    # Two, not three: the first observation establishes the baseline - there is
    # no previous page to compare it against - so only the 2nd and 3rd clicks
    # can count. The wait and the scroll count for nothing either way.
    assert st["stalled"] == 2


def test_progress_resets_the_counter():
    a = page_signature(url="https://x/a", title="A", refs=["e1"])
    b = page_signature(url="https://x/b", title="B", refs=["e2"])
    st, stopped = _advance([a, a, a, b, a, a, a])
    assert stopped is False
    assert st["stalled"] == 2


# --- what counts as the same page ------------------------------------------

def test_the_same_controls_in_a_different_order_are_the_same_page():
    """Otherwise a re-render with identical content resets the counter forever."""
    assert (page_signature(url="u", title="t", refs=["a", "b", "c"]) ==
            page_signature(url="u", title="t", refs=["c", "a", "b"]))


def test_a_new_control_appearing_is_progress():
    """A dialog opening or a menu revealing an option IS the page changing."""
    assert (page_signature(url="u", title="t", refs=["a"]) !=
            page_signature(url="u", title="t", refs=["a", "b"]))


def test_navigation_is_progress_even_with_identical_controls():
    assert (page_signature(url="https://x/1", title="t", refs=["a"]) !=
            page_signature(url="https://x/2", title="t", refs=["a"]))


def test_a_title_change_is_progress():
    assert (page_signature(url="u", title="Loading", refs=["a"]) !=
            page_signature(url="u", title="Ready", refs=["a"]))


# --- bounds -----------------------------------------------------------------

def test_the_tolerance_is_generous_enough_for_a_few_waits():
    """A page needing a couple of waits before responding must not be cut off."""
    assert DEFAULT_TOLERANCE >= 4
    sig = page_signature(url="u", title="t", refs=["a"])
    _st, stopped = _advance([sig] * 3)
    assert stopped is False


def test_a_zero_tolerance_disables_the_guard():
    sig = page_signature(url="u", title="t", refs=["a"])
    _st, stopped = _advance([sig] * 50, tolerance=0)
    assert stopped is False


def test_a_fresh_run_never_stops_immediately():
    assert should_stop(None) is False
    assert should_stop(update(None, "x")) is False


def test_the_reason_names_what_did_not_change():
    """"ran out of steps" made two different failures indistinguishable."""
    sig = page_signature(url="u", title="t", refs=["a"])
    st, _ = _advance([sig] * 8)
    msg = describe(st, DEFAULT_TOLERANCE, ["click", "wait", "click", "scroll"])
    assert "changed nothing" in msg
    assert "not evidence the goal was reached" in msg
    assert "click" in msg


def test_the_stop_reason_is_distinct_from_the_step_ceiling():
    """A caller must be able to tell 'stuck' from 'long'."""
    assert STOP_REASON == "no_progress" and STOP_REASON != "max_steps"


# --- the callsite -----------------------------------------------------------

def test_the_loop_checks_progress_before_thinking():
    """A dead page must not cost another 40-second decision."""
    path = os.path.join(os.path.dirname(__file__), "..", "vera", "operator",
                        "operator_loop.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    check = src.index("_progress.should_stop(")
    think = src.index("decision = await think(")
    assert check < think, "the progress check must precede the think call"


def test_the_ceiling_reason_list_knows_about_no_progress():
    path = os.path.join(os.path.dirname(__file__), "..", "vera", "operator",
                        "operator_trace_core.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "CEILING_REASONS" in src
