"""The operator finishes the job and then does not stop.

Census 30, run 344edbb13e, "Verify inline validation behavior in a real
browser". Fifteen steps. At step 4 its own thought read

    "We've verified inline validation is working - the textbox has ..."

It had done the job and said so, then spent eleven more steps retyping into the
same field and ended on max_steps - which is recorded as a FAILURE. Across the
19 most recent runs only 3 ended in `done`: 5 max_steps, 4 time_budget, 3
too_many_errors, 2 no_progress, 2 repeating_action. Running out IS the normal
ending.

Two prompt-shape causes, no reasoning failure:
  * the reply template pre-filled `"done": false` on every turn;
  * the history line recorded action and result but never the thought, so the
    model's own conclusion scrolled past unreferenced.

Also here: `press {"key": "'Tab'"}` from run c493020948 - a keyword argument
wrapped in literal quotes, pressing a key that does not exist.

Pure: no browser, no LLM.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import completion as C          # noqa: E402
from vera.operator import actions as A             # noqa: E402
from vera.operator.thinker import build_prompt     # noqa: E402

pytestmark = pytest.mark.critical


# -- recognising the claim, from the real thoughts --------------------------

def test_the_census_30_thought_is_recognised():
    assert C.claims_completion(
        "We've verified inline validation is working - the textbox has an error") is True


@pytest.mark.parametrize("thought", [
    "Both requirements are met",
    "The goal is achieved - the countdown decremented",
    "I have confirmed the error message appears",
    "Verification complete",
    "Successfully verified the toggle switches format",
])
def test_other_ways_of_saying_it_are_recognised(thought):
    assert C.claims_completion(thought) is True


@pytest.mark.parametrize("thought", [
    "I need to verify the toggle format functionality by clicking",
    "To verify inline validation behavior in this email form, I need to",
    "The form is showing an error message",
    "Need to first activate the timer with a visible countdown",
    "",
])
def test_an_intention_to_verify_is_not_a_finding(thought):
    """The commonest thought in every trace is the model saying what it is
    ABOUT to check. Firing on that would nudge every run to stop early."""
    assert C.claims_completion(thought) is False


def test_a_claim_with_work_still_outstanding_does_not_fire():
    """'verified X, now I need Y' is progress, not completion."""
    assert C.claims_completion(
        "We've verified the clock ticks, now I need to check the toggle") is False
    assert C.claims_completion(
        "I have confirmed the first requirement but still need the second") is False


# -- the nudge --------------------------------------------------------------

def test_a_claim_that_was_not_acted_on_gets_nudged():
    h = [{"action": "click", "args": {}, "result": {"ok": True},
          "thought": "We've verified inline validation is working"}]
    assert C.nudge_for(h) == C.NUDGE


def test_a_run_that_already_said_done_is_not_nudged():
    h = [{"action": "done", "args": {}, "result": {"ok": True},
          "thought": "We've verified inline validation is working"}]
    assert C.nudge_for(h) == ""


def test_only_the_most_recent_step_is_considered():
    """A claim two steps ago was already nudged once; repeating it every turn
    would train the model to ignore it."""
    h = [{"action": "click", "thought": "We've verified it works"},
         {"action": "click", "thought": "Now checking the second field"}]
    assert C.nudge_for(h) == ""


def test_no_history_is_not_a_nudge():
    assert C.nudge_for([]) == ""
    assert C.nudge_for(None) == ""


def test_the_nudge_asks_rather_than_orders():
    """A model that has NOT finished must not be pushed into saying it has."""
    assert "If it genuinely is" in C.NUDGE
    assert "If it is not" in C.NUDGE


# -- the prompt ------------------------------------------------------------

def _obs():
    o = type("O", (), {})()
    o.url, o.title, o.text, o.elements = "http://x", "X", "", []
    return o


def test_the_template_no_longer_pre_fills_the_answer():
    """A slot shown with an answer already in it gets copied - measured on
    this codebase the same day with an editor placeholder."""
    p = build_prompt("goal", _obs(), history=[])
    assert '"done": false}' not in p["user"], "the answer is pre-filled again"
    assert '"done"' in p["user"], "done must still be reachable"


def test_the_nudge_reaches_the_prompt():
    h = [{"action": "click", "args": {}, "result": {"ok": True},
          "thought": "We've verified inline validation is working"}]
    assert C.NUDGE in build_prompt("goal", _obs(), history=h)["user"]


def test_an_ordinary_step_adds_no_nudge():
    h = [{"action": "click", "args": {}, "result": {"ok": True},
          "thought": "I need to click the button next"}]
    assert C.NUDGE not in build_prompt("goal", _obs(), history=h)["user"]


def test_the_loop_puts_the_thought_into_history():
    """Without this the nudge has nothing to read - the history line used to
    record only action and result.

    Driven, not grepped: '"thought": thought' appears five times in run_loop
    for other records, so a source check cannot tell whether the HISTORY entry
    carries it. This inspects the history the next think() is actually handed.
    """
    import asyncio
    from vera.operator import operator_loop as OL
    from vera.operator import safety as _safety

    seen = {}

    class _S:
        def __init__(self):
            self.history = []
            self.session_id = "s1"

    async def observe(_s, i):
        o = _obs()
        o.screenshot_path = ""
        return o

    async def think(_g, _o, hist, _c):
        if hist:                       # the second step sees the first's record
            seen["history"] = [dict(h) for h in hist]
        return {"action": "click", "args": {"ref": "e1"},
                "thought": "step %d thinking" % (len(hist) + 1), "done": False}

    async def act(*_a, **_k):
        return {"ok": True}

    asyncio.run(OL.run_loop(
        "goal", _S(), observe_fn=observe, think_fn=think, act_fn=act, max_steps=2,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    assert seen.get("history"), "think() never saw a history entry"
    assert seen["history"][0].get("thought") == "step 1 thinking", \
        "the history entry reached think() without its thought"


# -- quoted keyword values --------------------------------------------------

def test_a_quoted_key_name_is_unwrapped():
    """Run c493020948 pressed \"'Tab'\" twice. There is no such key."""
    assert A.validate_action("press", {"key": "'Tab'"})["args"]["key"] == "Tab"
    assert A.validate_action("press", {"key": '"Enter"'})["args"]["key"] == "Enter"


def test_a_plain_key_is_untouched():
    assert A.validate_action("press", {"key": "Control+A"})["args"]["key"] == "Control+A"


def test_a_quoted_direction_is_unwrapped():
    assert A.validate_action("nav", {"direction": "'back'"})["args"]["direction"] == "back"


def test_typed_text_keeps_its_quotes():
    """A user may legitimately type a quoted string - `text` is free text, not
    a keyword, and must never be unwrapped."""
    assert A.validate_action("type", {"text": "'quoted'"})["args"]["text"] == "'quoted'"


def test_mismatched_quotes_are_left_alone():
    assert A.validate_action("press", {"key": "'Tab\""})["args"]["key"] == "'Tab\""


# â”€â”€ run ce4b0a4725: a reused session ignores the url it was given â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

from vera.operator import session_target as ST      # noqa: E402


def test_a_reused_session_must_be_moved_to_the_requested_page():
    """The loop passed .../preview/79d473ec-.../timer.html and the run opened
    on the dashboard, then spent all twelve steps failing to reach it."""
    assert ST.needs_navigation(
        "https://localhost:8999/",
        "https://localhost:8999/remote/sandbox/preview/abc/timer.html") is True


def test_a_session_already_on_the_page_is_left_alone():
    u = "https://localhost:8999/remote/sandbox/preview/abc/timer.html"
    assert ST.needs_navigation(u, u) is False


def test_a_trailing_slash_or_fragment_is_not_a_different_page():
    assert ST.needs_navigation("http://x/a", "http://x/a/") is False
    assert ST.needs_navigation("http://x/a", "http://x/a#top") is False


def test_a_different_query_is_a_different_page():
    assert ST.needs_navigation("http://x/a?v=1", "http://x/a?v=2") is True


def test_no_requested_url_means_stay_put():
    """A caller that named no target is happy where the session is; moving it
    would undo a deliberate hand-off between steps."""
    assert ST.needs_navigation("http://x/a", "") is False
    assert ST.needs_navigation("http://x/a", None) is False


def test_a_relative_url_is_left_to_open_session_to_resolve():
    """It cannot be compared here, and _open_session resolves it against
    base_url. Guessing would turn a working relative target into a wrong one."""
    assert ST.needs_navigation("http://x/a", "/timer.html") is False


def test_an_unknown_current_page_still_navigates():
    assert ST.needs_navigation("", "http://x/a") is True


def test_cap_run_is_wired_to_move_a_reused_session():
    """Pins the call site: the helper is inert unless cap_run consults it."""
    import inspect
    from vera.operator import operator_web_capabilities as OW
    src = inspect.getsource(OW.cap_run)
    assert "_sess_target.needs_navigation(_cur, url)" in src
    assert 'await _actions.perform(s, "goto", {"url": url})' in src


# â”€â”€ run 00f154eb0e: the history could not show what had been seen â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_the_history_line_carries_what_the_page_showed():
    """'observe X count down from 01:30 to 00:00' is impossible to satisfy if
    every turn shows one snapshot and no record of the previous ones."""
    o = _obs()
    o.title, o.text = "Timer", "00:00 Start Pause Reset"
    h = [{"action": "click", "args": {"ref": "e1"}, "result": {"ok": True},
          "thought": "t", "seen": "01:30 Start Pause Reset"},
         {"action": "wait", "args": {"ms": 5000}, "result": {"ok": True},
          "thought": "t", "seen": "00:54 Start Pause Reset"}]
    user = build_prompt("observe the countdown", o, history=h)["user"]
    assert "page showed: 01:30 Start Pause Reset" in user
    assert "page showed: 00:54 Start Pause Reset" in user, \
        "the model cannot see the change it is asked to confirm"


def test_a_step_with_nothing_seen_adds_no_noise():
    h = [{"action": "click", "args": {}, "result": {"ok": True}, "thought": "t"}]
    assert "page showed:" not in build_prompt("g", _obs(), history=h)["user"]


def test_the_observed_digest_is_bounded_and_single_line():
    from vera.operator.operator_loop import _observed_text, SEEN_CHARS
    o = _obs()
    o.text = ("line one\n\n   line two   \n" + "x" * 500)
    seen = _observed_text(o)
    assert "\n" not in seen and len(seen) <= SEEN_CHARS
    assert seen.startswith("line one line two")


def test_the_digest_falls_back_to_the_title_when_there_is_no_text():
    from vera.operator.operator_loop import _observed_text
    o = _obs()
    o.text, o.title = "", "Timer"
    assert _observed_text(o) == "Timer"


def test_the_loop_records_what_the_page_showed():
    """Driven, not grepped - the digest is useless if run_loop never stores it."""
    import asyncio
    from vera.operator import operator_loop as OL
    from vera.operator import safety as _safety
    seen = {}

    class _S:
        def __init__(self):
            self.history = []
            self.session_id = "s1"

    async def observe(_s, i):
        o = _obs()
        o.screenshot_path = ""
        o.text = "%02d:00 Start" % (90 - i)
        return o

    async def think(_g, _o, hist, _c):
        if hist:
            seen["history"] = [dict(x) for x in hist]
        return {"action": "click", "args": {"ref": "e1"}, "thought": "t", "done": False}

    async def act(*_a, **_k):
        return {"ok": True}

    asyncio.run(OL.run_loop(
        "observe", _S(), observe_fn=observe, think_fn=think, act_fn=act, max_steps=2,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    assert seen.get("history"), "think() never saw history"
    assert seen["history"][0].get("seen") == "89:00 Start", \
        "the history entry reached think() without what the page showed"
