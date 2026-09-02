"""A countdown is progress, and a caller may not starve its own run.

Census 24, author-then-edit. Every attempt was a variant of "click Start, wait,
confirm the countdown decrements", and three of them died:

    cyc5   no_progress  after wait, click
    cyc18  no_progress  after wait, click, screenshot
    cyc19  no_progress  after click, wait
    cyc7   time_budget  stopped after 103s (budget 95s) - the MODEL asked for 95

The page was working the whole time. A countdown changes only its text, and the
signature compared url, title and element refs - so a timer ticking down looked
identical to a frozen page, and the operator was stopped for doing exactly what
it had been asked to do.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import operator_progress as pr   # noqa: E402
from vera.operator import operator_budget as ob     # noqa: E402
from vera.operator import operator_loop as OL       # noqa: E402
from vera.operator import safety as _safety         # noqa: E402

pytestmark = pytest.mark.critical


TIMER_URL = "https://localhost:8999/remote/sandbox/preview/abc/timer.html"


def _sig(text, url=TIMER_URL, title="Timer", refs=("start", "reset")):
    return pr.page_signature(url=url, title=title, refs=list(refs), text=text)


# ── the failure, reproduced ─────────────────────────────────────────────────

def test_a_ticking_countdown_is_a_changed_page():
    """The exact case: same url, same title, same two buttons, new time."""
    assert _sig("01:30 Start Reset") != _sig("01:29 Start Reset")


def test_the_old_signature_could_not_see_it():
    """Guard the premise - without text these are identical, which is why the
    run was stopped."""
    old_a = pr.page_signature(url=TIMER_URL, title="Timer", refs=["start", "reset"])
    old_b = pr.page_signature(url=TIMER_URL, title="Timer", refs=["start", "reset"])
    assert old_a == old_b


def test_a_countdown_resets_the_stall_counter():
    """Five ticks must not accumulate towards a stall, or the run dies mid-task."""
    st = None
    for t in ("01:30", "01:29", "01:28", "01:27", "01:26", "01:25"):
        st = pr.update(st, _sig(t + " Start Reset"), last_action="wait")
        assert not pr.should_stop(st, pr.DEFAULT_TOLERANCE)


def test_a_genuinely_frozen_page_still_stops_the_run():
    """The detector must keep doing its job: identical text, identical
    everything, repeated acts."""
    st = None
    for _ in range(pr.DEFAULT_TOLERANCE + 1):
        st = pr.update(st, _sig("Nothing ever happens"), last_action="click")
    assert pr.should_stop(st, pr.DEFAULT_TOLERANCE) is True


# ── the other content-only changes this was blind to ────────────────────────

@pytest.mark.parametrize("before,after", [
    ("Progress: 10%", "Progress: 40%"),                       # a bar filling
    ("Email", "Email Please enter a valid address"),          # validation appearing
    ("log: 3 lines", "log: 4 lines"),                         # a live log growing
    ("Total 12.00", "Total 18.50"),                           # a total recalculating
])
def test_content_only_changes_count_as_progress(before, after):
    assert _sig(before) != _sig(after)


def test_whitespace_alone_is_not_a_change():
    """Re-rendering the same text with different spacing is the same page - it
    must not reset the counter for ever."""
    assert _sig("01:30   Start\n Reset") == _sig("01:30 Start Reset")


def test_a_real_navigation_is_still_a_change():
    assert _sig("same", url=TIMER_URL) != _sig("same", url=TIMER_URL + "?x=1")


def test_new_controls_are_still_a_change():
    assert _sig("same", refs=("start",)) != _sig("same", refs=("start", "stop"))


def test_ref_order_still_does_not_matter():
    assert _sig("same", refs=("a", "b")) == _sig("same", refs=("b", "a"))


def test_signature_junk_does_not_raise():
    assert isinstance(pr.page_signature(), str)
    assert isinstance(pr.page_signature(url=None, title=None, refs=None, text=None), str)


# ── a caller may not starve its own run ─────────────────────────────────────

def test_the_models_95_second_budget_is_floored():
    """cyc7 verbatim: it asked for 95s and died at 103s having managed five
    steps of a task needing a click and a two-second wait."""
    assert ob.caller_budget(95) == ob.MIN_CALLER_SECONDS
    assert ob.MIN_CALLER_SECONDS >= 180


def test_a_sensible_tightening_is_honoured():
    """The point is a floor, not ignoring the caller."""
    assert ob.caller_budget(600) == 600
    assert ob.caller_budget(200) == 200


def test_no_preference_takes_the_default():
    assert ob.caller_budget(0) == ob.DEFAULT_MAX_SECONDS
    assert ob.caller_budget(None) == ob.DEFAULT_MAX_SECONDS


def test_the_floor_leaves_room_for_several_cycles():
    """One observe -> think -> act cycle contains a full generation; a budget
    that cannot fit a few of them only turns a slow run into a failed one."""
    assert ob.MIN_CALLER_SECONDS >= 3 * 40


def test_budget_junk_does_not_raise():
    assert ob.caller_budget("nonsense") == ob.DEFAULT_MAX_SECONDS


# ── the call sites ──────────────────────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(os.path.dirname(__file__), "..", *parts),
              encoding="utf-8") as fh:
        return fh.read()


# â”€â”€ the wiring, driven rather than read â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
# A signature that can see text is worthless if the loop never hands it any, and
# an earlier draft of these tests asserted only that the call APPEARS in the
# source - which passes just as happily when the value is wrong. These drive the
# real run_loop over the census-24 page instead.

class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


class _El:
    def __init__(self, ref):
        self.ref = ref


def _drive(texts, tolerance=3):
    """Run the REAL loop over pages differing ONLY in their text.

    url, title and both controls are identical at every step - the shape of the
    countdown page. Actions alternate so the repeat guard (action+args+url)
    cannot fire and steal the outcome; what is under test is the stall detector.
    """
    async def observe(_s, i):
        o = type("O", (), {})()
        o.url, o.title = TIMER_URL, "Timer"
        o.elements = [_El("start"), _El("reset")]
        o.text = texts[min(i - 1, len(texts) - 1)]
        o.screenshot_path = ""
        return o

    async def think(_g, _o, _h, _c):
        # Clicks, because a stall is only counted after a CHANGE-SEEKING action
        # - waiting and screenshotting assert nothing, so a run of those could
        # never trip the detector and would make these tests pass vacuously.
        # Alternating the TARGET keeps the repeat guard (action+args+url) out.
        el = "start" if len(_h) % 2 == 0 else "reset"
        return {"action": "click", "args": {"element": el}, "thought": "t"}

    async def act(*_a, **_k):
        return {"ok": True}

    return asyncio.run(OL.run_loop(
        "click Start and confirm the countdown decrements", _Session(),
        observe_fn=observe, think_fn=think, act_fn=act,
        max_steps=len(texts), progress_tolerance=tolerance,
        policy=_safety.SafetyPolicy(allowlist=["localhost"],
                                    allow_destructive=True, confirm=True)))


def test_a_decrementing_countdown_runs_to_the_end():
    """The census-24 failure itself: the run must NOT be stopped."""
    res = _drive(["Time left: %02d Start Reset" % n for n in range(30, 20, -1)])
    assert res.get("reason") != pr.STOP_REASON, res.get("steps")


def test_a_genuinely_frozen_page_is_still_stopped():
    """The detector must not be defanged - an unchanging page still stops."""
    res = _drive(["Time left: 30 Start Reset"] * 10)
    assert res.get("reason") == pr.STOP_REASON


# â”€â”€ the caller's budget, at the boundary the cap uses â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_no_preference_passes_no_budget_at_all():
    """Not 'the default' - NOTHING, so run_loop's own default stays the one
    source of truth and cannot drift from this module's copy of it."""
    assert ob.budget_kwargs(0) == {}
    assert ob.budget_kwargs(None) == {}
    assert ob.budget_kwargs("") == {}


def test_the_models_95_seconds_arrives_at_the_loop_floored():
    assert ob.budget_kwargs(95) == {"max_seconds": ob.MIN_CALLER_SECONDS}


def test_a_generous_caller_budget_is_passed_through():
    assert ob.budget_kwargs(600) == {"max_seconds": 600.0}


def test_a_nonsense_budget_is_not_a_crash_in_the_browser_path():
    assert ob.budget_kwargs("soon") == {}


def test_the_cap_hands_its_caller_value_to_that_helper():
    """The one line a unit test cannot reach without a real browser."""
    src = _src("vera", "operator", "operator_web_capabilities.py")
    assert "**_op_budget.budget_kwargs(max_seconds)," in src
