"""Eleven clicks on one button is not a budget problem.

Census run 15, author-then-edit. Its verification step (operator run 44138b2db0)
ran 15 steps / 437s and hit its ceiling - 29% of that goal's whole budget. The
step thoughts show what happened: it clicked Start repeatedly, ran the timer to
completion, then spent the back half unable to tell whether "Time's up!" meant
success or the residue of its own clicks. phases={'act': 15} - every step acted.

Two things had to be true before a guard was worth writing, and the ORDER
matters:

  1. the signal had to become precise. The step events recorded `action` but not
     `args`, so "click 11x" was ambiguous between eleven clicks on ONE element
     (thrash) and eleven on DIFFERENT ones (progress). A guard built on the verb
     alone would fire on healthy runs;
  2. only then a guard, keyed on action + args + url.

The URL is in the key deliberately: a paginating "Next" repeats the identical
action and args, and it is progress - the page changes. That is what keeps
legitimate repetition safe.

Pure: no Redis, no browser, no app import.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import operator_loop as OL          # noqa: E402
from vera.operator import safety as _safety            # noqa: E402
from vera.operator.operator_trace_core import digest_events  # noqa: E402


class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def _obs_at(url):
    class _O:
        pass
    o = _O(); o.url = url; o.screenshot_path = ""
    return o


def _run(decisions, urls=None, max_steps=20):
    """Drive the real loop. `urls` gives the page seen at each step."""
    calls = {"think": 0, "act": 0}
    urls = urls or []

    async def observe(_s, i):
        return _obs_at(urls[i - 1] if i - 1 < len(urls) else "http://example.com/p")

    async def think(_g, _o, _h, _c):
        calls["think"] += 1
        return decisions[min(calls["think"] - 1, len(decisions) - 1)]

    async def act(*_a, **_k):
        calls["act"] += 1
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "verify the thing", _Session(), observe_fn=observe, think_fn=think, act_fn=act,
        max_steps=max_steps,
        policy=_safety.SafetyPolicy(allowlist=["example.com"], allow_destructive=True,
                                    confirm=True)))
    return res, calls


def _click(el):
    return {"action": "click", "args": {"element": el}, "thought": "t"}


# ── the run-15 case ─────────────────────────────────────────────────────────
def test_hammering_one_element_on_one_page_stops():
    """The observed failure: Start clicked over and over on an unchanged page."""
    res, calls = _run([_click("e1")] * 20)
    assert res["reason"] == "repeating_action"
    assert calls["act"] == OL._REPEAT_LIMIT, "should stop AT the limit, not after 20"


def test_the_stop_is_recorded_with_its_reason():
    res, _ = _run([_click("e1")] * 20)
    last = res["steps"][-1]
    assert last["phase"] == "repeating"
    assert "same action was repeated" in last["reason"]


def test_it_is_not_reported_as_success():
    """`reason` carries the truth and `ok` must agree with it - the L8 property."""
    res, _ = _run([_click("e1")] * 20)
    assert res["ok"] is False and res["done"] is False


# ── what must NOT be stopped ────────────────────────────────────────────────
def test_clicking_different_elements_is_progress_not_thrash():
    """Eleven clicks on eleven elements is a working run. A guard keyed on the
    VERB alone would have killed this.

    The urls are supplied because a WORKING run is one whose page responds:
    since 2026-08-31 the loop also stops a run whose page never changes at all
    (operator_progress), and this stub previously reported the identical page
    forever. That is a fixture gap, not a conflict - clicking twelve elements
    that each do nothing is the census run 18 failure, and
    test_twelve_dead_clicks_on_a_static_page_stop_the_run below pins it."""
    urls = ["http://example.com/p%d" % i for i in range(1, 13)]
    res, calls = _run([_click("e%d" % i) for i in range(1, 13)], urls=urls, max_steps=12)
    assert res["reason"] != "repeating_action"
    assert calls["act"] == 12


def test_repeating_across_a_page_change_is_progress():
    """Pagination: the identical action and args, but the page moves. This is
    why the URL is part of the key."""
    urls = ["http://example.com/p%d" % i for i in range(1, 13)]
    res, calls = _run([_click("next")] * 12, urls=urls, max_steps=12)
    assert res["reason"] != "repeating_action"
    assert calls["act"] == 12


def test_a_few_repeats_are_tolerated():
    """Browsers legitimately repeat a little - a retried slow click, a banner
    that reappears. The limit is deliberately generous."""
    n = OL._REPEAT_LIMIT - 1
    res, calls = _run([_click("e1")] * n, max_steps=n)
    assert res["reason"] != "repeating_action" and calls["act"] == n


def test_an_interrupted_streak_resets():
    """A different action in the middle means the run was doing something."""
    seq = [_click("e1"), _click("e1"), _click("e2"), _click("e1"), _click("e1")]
    res, calls = _run(seq, max_steps=5)
    assert res["reason"] != "repeating_action" and calls["act"] == 5


# ── the signature ───────────────────────────────────────────────────────────
def test_the_signature_separates_element_and_page():
    s = OL._repeat_signature
    assert s("click", {"element": "e1"}, "u") == s("click", {"element": "e1"}, "u")
    assert s("click", {"element": "e1"}, "u") != s("click", {"element": "e2"}, "u")
    assert s("click", {"element": "e1"}, "u") != s("click", {"element": "e1"}, "v")
    # Key order must not change identity.
    assert s("t", {"a": 1, "b": 2}, "u") == s("t", {"b": 2, "a": 1}, "u")


def test_the_signature_survives_unserialisable_args():
    assert OL._repeat_signature("click", {"x": object()}, "u")


# ── the trace must stop over-reporting bare verbs ──────────────────────────
def _step_ev(i, action, args, url="http://example.com/p"):
    return {"type": "operator.step", "i": i, "phase": "act", "action": action,
            "args": args, "url": url}


def test_thrash_is_counted_by_action_AND_args():
    """Counting bare verbs reported 'click 11x' for eleven clicks on eleven
    different elements - a non-problem, presented as the headline."""
    ev = [_step_ev(i, "click", {"element": "e%d" % i}) for i in range(1, 12)]
    assert digest_events(ev)["counters"]["repeated_actions"] == 0


def test_real_repetition_is_still_caught():
    ev = [_step_ev(i, "click", {"element": "e1"}) for i in range(1, 12)]
    d = digest_events(ev)
    assert d["counters"]["repeated_actions"] == 1
    assert any("11x" in w for w in d["warnings"])


def test_records_without_args_fall_back_to_the_old_behaviour():
    """Runs recorded before args were captured cannot do better than the verb."""
    ev = [{"type": "operator.step", "i": i, "phase": "act", "action": "click"}
          for i in range(1, 12)]
    assert digest_events(ev)["counters"]["repeated_actions"] == 1


def test_twelve_dead_clicks_on_a_static_page_stop_the_run():
    """The other half of the rule, added 2026-08-31. Twelve clicks on twelve
    DIFFERENT elements that never change the page is not progress - it is
    census run 18's author-then-edit, which spent 21 minutes doing exactly this
    and reported max_steps."""
    res, calls = _run([_click("e%d" % i) for i in range(1, 13)], max_steps=12)
    assert res["reason"] == "no_progress"
    assert calls["act"] < 12
