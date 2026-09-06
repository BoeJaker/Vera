"""One unusable reply must not throw away a run that has budget left.

Census 37, author-then-edit. The output cap I added after census 35 was set to
1024 tokens on the evidence that the largest legitimate decision was 816. Across
36 think calls in this run the working range was 211-854 and TWO landed on
exactly eval_count=1024 - the cap itself, not a natural stop. One of those opened
with prose:

    think_error: could not parse decision JSON: The user wants to verify that
    `timer.html` starts a countdown from 90 seconds instead of 60 ...

and never reached its JSON. operator_loop broke on the first think error, so a
single truncated reply ended the whole operator run with every step and second
of its budget unspent.

Both halves are fixed here and both are tested: the cap has real headroom now,
and - the part that matters more, because the cap can always be wrong again - a
bad reply costs one step instead of the run.

Pure: the thinker is a stub, so this asserts the LOOP's behaviour.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import operator_loop as OL              # noqa: E402
from vera.operator import safety as _safety                # noqa: E402
from vera.operator import thinker as TH                    # noqa: E402


class _Obs:
    def __init__(self, url="http://x/timer.html"):
        self.url = url
        self.title = "Timer"
        self.elements = []
        self.text = "01:30"
        self.screenshot_path = ""


class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def _run(decisions, max_steps=12):
    calls = {"think": 0, "act": 0}
    seen_history = []

    async def observe(_s, _i):
        return _Obs()

    async def think(_g, _o, hist, _c):
        calls["think"] += 1
        seen_history.append(list(hist))
        return decisions[min(calls["think"] - 1, len(decisions) - 1)]

    async def act(*_a, **_k):
        calls["act"] += 1
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "verify the countdown", _Session(), observe_fn=observe, think_fn=think,
        act_fn=act, max_steps=max_steps,
        policy=_safety.SafetyPolicy(allowlist=["x"], allow_destructive=True,
                                    confirm=True)))
    return res, calls, seen_history


_BAD = {"error": "could not parse decision JSON: The user wants to verify that..."}
_TRUNC = {"error": "the reply was cut off at the output limit before it produced "
                   "a JSON object", "truncated": True}


def _click(el="e1"):
    return {"action": "click", "args": {"ref": el}, "thought": "t"}


def _done():
    return {"action": "done", "args": {"summary": "verified"}, "thought": "t"}


# ── the census 37 case ──────────────────────────────────────────────────────
def test_one_bad_reply_no_longer_ends_the_run():
    """The exact regression: parse failure on step 1, everything else fine."""
    res, calls, _ = _run([_TRUNC, _click(), _done()])
    assert res["done"] is True
    assert res["reason"] == "done"
    assert calls["act"] == 1, "the run went on to do real work"


def test_the_run_survives_a_bad_reply_in_the_middle():
    res, _calls, _ = _run([_click("e1"), _BAD, _click("e2"), _done()])
    assert res["done"] is True


def test_a_persistent_thinker_failure_still_stops_the_run():
    """The property the old code got right and this must not lose."""
    res, calls, _ = _run([_BAD] * 12)
    assert res["reason"] == "think_error"
    assert res["ok"] is False
    assert calls["think"] == OL._THINK_ERROR_LIMIT, "stops AT the limit"
    assert calls["act"] == 0


def test_the_streak_resets_on_a_good_reply():
    """Two bad, one good, two bad must NOT stop a run whose limit is three -
    otherwise the counter is measuring the total, not a streak."""
    res, _calls, _ = _run([_BAD, _BAD, _click(), _BAD, _BAD, _done()])
    assert res["done"] is True


# ── what the model is told next ─────────────────────────────────────────────
def test_the_next_prompt_carries_the_failure():
    """Asking the identical question again after a bad reply just invites the
    same reply. The history is how build_prompt tells the model what went wrong."""
    _res, _calls, history = _run([_TRUNC, _click(), _done()])
    second = history[1]
    assert second, "the second think call saw a non-empty history"
    assert any((h.get("result") or {}).get("error") for h in second)


def test_the_failed_step_is_recorded_with_its_reason():
    res, _calls, _ = _run([_TRUNC, _click(), _done()])
    think_steps = [s for s in res["steps"] if s.get("phase") == "think"]
    assert think_steps and think_steps[0].get("truncated") is True


# ── the cap itself ──────────────────────────────────────────────────────────
def test_the_cap_clears_the_observed_working_range():
    """Census 37's 36 think calls ran 211-854 tokens, and two hit the old 1024
    ceiling exactly. A cap must sit ABOVE the distribution, not on it."""
    assert TH.THINK_MAX_TOKENS >= 2048


def test_the_cap_still_bounds_the_runaway_it_exists_for():
    """Census 35 spent 199s on a single 3382-token decision, against a ceiling
    of 16384."""
    assert TH.THINK_MAX_TOKENS < 3382


def test_a_truncated_reply_is_reported_as_truncated():
    """The loop distinguishes 'cut off' from 'malformed', so decide() must say
    which it was."""
    seen = {}

    async def call_cap(name, **kwargs):
        seen["kwargs"] = kwargs
        return {"text": "The user wants to verify that the timer", "truncated": True}

    class _O:
        url = title = ""
        elements = []
        text = ""

        def compact(self, max_elements=60):
            return ""

    out = asyncio.run(TH.decide("goal", _O(), [], call_cap))
    assert out.get("truncated") is True
    assert "cut off" in out.get("error", "")
