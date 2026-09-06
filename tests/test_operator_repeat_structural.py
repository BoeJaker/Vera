"""Thrash on a LIVE page - the shape that got past both existing guards.

Census 35, author-then-edit. Three operator.run calls, 1452s, all three failed
the same way:

    9c81d67747  14 steps  time_budget   click e1 x7, click e3 x4, click e2
    15a94be192  10 steps  time_budget   click e1 x6, click e2, click e3

Two guards existed and neither could see it:

  * operator_loop's consecutive counter resets whenever the signature changes,
    and this alternated (e1, wait, e1, e3, e1, e1, e2) so it never reached five
    in a row;
  * operator_progress WOULD have caught it, until page_signature was given the
    page TEXT on 2026-09-05 so that a countdown ticking 01:30 -> 00:54 read as
    progress rather than as a dead page. It does - and that same change means a
    clock keeps the signature moving no matter what the operator does, so a run
    thrashing on a live page became invisible to it.

The tests below therefore hold BOTH properties at once, because fixing either
one alone is what produced this bug: a ticking clock is still progress, and
clicking a dead control while the clock ticks is still thrash.

Pure: no browser, no Redis, no app import.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import operator_loop as OL              # noqa: E402
from vera.operator import repeat_guard as RG               # noqa: E402
from vera.operator import safety as _safety                # noqa: E402


class _El:
    def __init__(self, ref):
        self.ref = ref


class _Obs:
    """An observation the loop can read: url, title, elements, text."""

    def __init__(self, url="http://x/timer.html", title="Timer",
                 refs=("e1", "e2", "e3"), text=""):
        self.url = url
        self.title = title
        self.elements = [_El(r) for r in refs]
        self.text = text
        self.screenshot_path = ""


class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def _run(decisions, observations, max_steps=20, pin_url=""):
    """Drive the REAL loop. `observations[i]` is the page seen at step i+1."""
    calls = {"think": 0, "act": 0}

    async def observe(_s, i):
        return observations[min(i - 1, len(observations) - 1)]

    async def think(_g, _o, _h, _c):
        calls["think"] += 1
        return decisions[min(calls["think"] - 1, len(decisions) - 1)]

    async def act(*_a, **_k):
        calls["act"] += 1
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "verify the countdown", _Session(), observe_fn=observe, think_fn=think,
        act_fn=act, max_steps=max_steps, pin_url=pin_url,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    return res, calls


def _click(el):
    return {"action": "click", "args": {"ref": el}, "thought": "t"}


def _ticking(n):
    """n observations of ONE page whose clock is running: identical structure,
    different text every step. This is what defeated operator_progress."""
    return [_Obs(text="01:%02d remaining" % (30 - i)) for i in range(n)]


# ── the census 35 failure ───────────────────────────────────────────────────
def test_alternating_clicks_on_a_ticking_page_stop_the_run():
    """Run 9c81d67747's exact action sequence, on a page whose text moves."""
    seq = ["e1", "e1", "e3", "e1", "e1", "e2", "e3", "e1", "e1", "e3", "e3", "e1"]
    res, calls = _run([_click(e) for e in seq], _ticking(20), max_steps=20)
    assert res["reason"] == RG.STOP_REASON
    assert calls["act"] < len(seq), "should stop before exhausting the sequence"


def test_the_real_run_would_have_stopped_four_steps_early():
    """Run 9c81d67747's literal step list, including the wait at step 2:

        e1, wait, e1, e3, e1, e1, e2, e3, e1, e1, e3, e3, e1

    e1's fifth attempt is step 9, so the run ends there instead of at 13. The
    four steps saved include step 13 - the 199s / 3382-token decision that took
    the run from 480s of budget to 581s spent.
    """
    seq = [_click("e1"), {"action": "wait", "args": {"ms": 2000}, "thought": "t"},
           _click("e1"), _click("e3"), _click("e1"), _click("e1"), _click("e2"),
           _click("e3"), _click("e1"), _click("e1"), _click("e3"), _click("e3"),
           _click("e1")]
    res, calls = _run(seq, _ticking(20), max_steps=13)
    assert res["reason"] == RG.STOP_REASON
    assert calls["act"] == 9, "stops on e1's fifth attempt, not at step 13"


def test_the_stop_says_what_repeated_and_that_the_page_held_still():
    seq = ["e1", "e3", "e1", "e2", "e1", "e3", "e1"]
    res, _ = _run([_click(e) for e in seq], _ticking(20), max_steps=20)
    last = res["steps"][-1]
    assert last["phase"] == "repeating"
    assert "click" in last["reason"] and "structure" in last["reason"]


def test_it_is_not_reported_as_success():
    seq = ["e1", "e3", "e1", "e2", "e1", "e3", "e1"]
    res, _ = _run([_click(e) for e in seq], _ticking(20), max_steps=20)
    assert res["ok"] is False and res["done"] is False


# ── what must NOT be stopped ────────────────────────────────────────────────
def test_an_action_that_reshapes_the_page_keeps_its_full_allowance():
    """"Load more" / "show next", alternating with a second control so the
    ADJACENCY counter cannot be what saves it - this must isolate the new guard.

    Each click adds an element, so every step opens a fresh structural epoch and
    the counts reset. Without that reset "more" reaches its fifth attempt at
    step 9 and the run dies there; with it, all twelve steps run. That is the
    property the URL-keyed signature protected and this must not lose.
    """
    obs = [_Obs(refs=tuple("e%d" % k for k in range(1, 4 + i)), text="items")
           for i in range(12)]
    seq = [_click("more" if i % 2 == 0 else "less") for i in range(12)]
    res, calls = _run(seq, obs, max_steps=12)
    assert res["reason"] != RG.STOP_REASON
    assert calls["act"] == 12, "a page that keeps responding is never cut short"


def test_the_same_sequence_on_a_frozen_structure_does_stop():
    """The other side of the pair: identical decisions, identical alternation,
    but the page stops reshaping. Only the structure differs, so this is what
    proves the reset above is doing the work."""
    seq = [_click("more" if i % 2 == 0 else "less") for i in range(12)]
    res, calls = _run(seq, _ticking(20), max_steps=12)
    assert res["reason"] == RG.STOP_REASON
    assert calls["act"] == 9, "'more' reaches its fifth attempt at step 9"


def test_clicking_different_controls_on_a_ticking_page_is_not_thrash():
    """A run genuinely exercising Start/Pause/Reset must be left alone."""
    seq = ["e1", "e2", "e3", "e1", "e2", "e3", "e1", "e2"]
    res, calls = _run([_click(e) for e in seq], _ticking(20), max_steps=8)
    assert res["reason"] != RG.STOP_REASON
    assert calls["act"] == 8


def test_a_ticking_clock_is_still_progress():
    """The 2026-09-05 property that must survive: a page whose only movement is
    its clock must not be called dead. Guarding this here means a future fix to
    the thrash problem cannot quietly restore the false positive."""
    seq = ["e1", "e2", "e3", "e1", "e2", "e3"]
    res, _ = _run([_click(e) for e in seq], _ticking(20), max_steps=6)
    assert res["reason"] != "no_progress"


def test_a_genuinely_dead_page_is_still_stopped():
    """The other half: no text movement either, and operator_progress still
    fires. Clicking twelve different dead controls is census run 18."""
    obs = [_Obs(text="static") for _ in range(14)]
    res, calls = _run([_click("e%d" % i) for i in range(1, 14)], obs, max_steps=13)
    assert res["reason"] == "no_progress"
    assert calls["act"] < 13


# ── the guard's own contract ────────────────────────────────────────────────
def test_the_page_key_ignores_text_but_not_structure():
    """Text is deliberately excluded: that is the whole difference between this
    guard and operator_progress."""
    a = RG.page_key(url="u", title="t", refs=["e1", "e2"])
    b = RG.page_key(url="u", title="t", refs=["e1", "e2"])
    assert a == b
    assert a != RG.page_key(url="u", title="t", refs=["e1", "e2", "e3"])
    assert a != RG.page_key(url="v", title="t", refs=["e1", "e2"])
    assert a != RG.page_key(url="u", title="s", refs=["e1", "e2"])


def test_counts_reset_when_the_structure_changes():
    st = None
    for _ in range(4):
        st = RG.update(st, "page-a", "click", {"ref": "e1"})
    assert RG.count(st) == 4
    st = RG.update(st, "page-b", "click", {"ref": "e1"})
    assert RG.count(st) == 1, "a reshaped page starts a fresh allowance"


def test_the_signature_separates_arguments_and_ignores_key_order():
    assert RG.signature("click", {"ref": "e1"}) != RG.signature("click", {"ref": "e2"})
    assert RG.signature("t", {"a": 1, "b": 2}) == RG.signature("t", {"b": 2, "a": 1})


def test_the_signature_survives_unserialisable_args():
    assert RG.signature("click", {"x": object()})


def test_describe_names_the_count_and_the_limit():
    st = None
    for _ in range(5):
        st = RG.update(st, "p", "click", {"ref": "e1"})
    d = RG.describe(st, 5)
    assert "5" in d and "click" in d
