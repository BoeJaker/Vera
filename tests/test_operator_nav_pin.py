"""A run aimed at one file must not wander off it.

Census 35, author-then-edit run b71aa5cb70. Opened correctly on

    https://localhost:8999/remote/sandbox/preview/<session>/timer.html

and its first decision was to leave for an invented `https://localhost:8999/
timer.html`, then goto -> reload -> back, three times round, twelve steps, 384s,
ending at max_steps having never returned to the page it started on.

The scope rule matters as much as the guard: a goal like "find X on wikipedia"
passes a url it is SUPPOSED to navigate away from. Only a sandbox preview URL -
one file, served out of a session - is pinned.

Pure: no browser, no Redis, no app import.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import nav_pin as NP                    # noqa: E402
from vera.operator import operator_loop as OL              # noqa: E402
from vera.operator import safety as _safety                # noqa: E402

PIN = "https://localhost:8999/remote/sandbox/preview/abc123/timer.html"


# ── what is pinnable ────────────────────────────────────────────────────────
def test_a_sandbox_preview_url_is_pinnable():
    assert NP.is_pinnable(PIN) is True


def test_an_ordinary_site_is_not_pinnable():
    """The wikipedia case: a run that must navigate is never constrained."""
    assert NP.is_pinnable("https://en.wikipedia.org/wiki/Timer") is False
    assert NP.is_pinnable("https://localhost:8999/") is False
    assert NP.is_pinnable("") is False


def test_nothing_is_refused_when_the_target_is_not_pinnable():
    """The destination is on ANOTHER HOST on purpose. An earlier version of this
    test navigated /wiki/Timer -> /wiki/Clock, which is a sibling and therefore
    allowed even when the scope rule is broken - so it passed no matter what
    is_pinnable did, and mutation testing caught it doing nothing."""
    err = NP.off_pin("https://en.wikipedia.org/wiki/Timer",
                     "https://en.wikipedia.org/wiki/Timer",
                     "goto", {"url": "https://example.org/clock"})
    assert err == ""


# ── the b71aa5cb70 sequence ─────────────────────────────────────────────────
def test_the_invented_url_is_refused():
    """Step 1 of the real run."""
    err = NP.off_pin(PIN, PIN, "goto", {"url": "https://localhost:8999/timer.html"})
    assert err
    assert PIN in err, "the error must name the URL that DOES work"


def test_a_bare_relative_filename_is_refused():
    """Step 4 of the real run: `goto /timer.html`, resolved against the page."""
    err = NP.off_pin(PIN, PIN, "goto", {"url": "/timer.html"})
    assert err


def test_returning_to_the_pinned_page_is_allowed():
    assert NP.off_pin(PIN, "https://localhost:8999/timer.html",
                      "goto", {"url": PIN}) == ""


def test_a_sibling_file_in_the_same_sandbox_is_allowed():
    """A page the same run wrote - a stylesheet, a second page - stays reachable."""
    sib = "https://localhost:8999/remote/sandbox/preview/abc123/style.css"
    assert NP.off_pin(PIN, PIN, "goto", {"url": sib}) == ""


def test_a_query_or_fragment_on_the_pinned_page_is_allowed():
    assert NP.off_pin(PIN, PIN, "goto", {"url": PIN + "?v=2"}) == ""
    assert NP.off_pin(PIN, PIN, "goto", {"url": PIN + "#end"}) == ""


def test_another_sessions_sandbox_is_refused():
    """The preview route is shared; a different session id is a different run's
    workspace and is not this goal's page."""
    other = "https://localhost:8999/remote/sandbox/preview/zzz999/timer.html"
    assert NP.off_pin(PIN, PIN, "goto", {"url": other})


# ── only goto is judged ─────────────────────────────────────────────────────
def test_clicks_and_reloads_are_never_judged():
    """reload/back/forward move within history the run already has; refusing
    them would strand a run whose first step was legitimate."""
    for action, args in (("click", {"ref": "e1"}),
                         ("nav", {"direction": "reload"}),
                         ("nav", {"direction": "back"}),
                         ("wait", {"ms": 500})):
        assert NP.off_pin(PIN, PIN, action, args) == ""


# ── through the real loop ───────────────────────────────────────────────────
class _Obs:
    def __init__(self, url=PIN):
        self.url = url
        self.title = "Timer"
        self.elements = []
        self.text = "01:30"
        self.screenshot_path = ""


class _Session:
    def __init__(self):
        self.history = []
        self.session_id = "s1"


def _run(decisions, pin_url=PIN, max_steps=12):
    calls = {"act": 0, "think": 0}

    async def observe(_s, _i):
        return _Obs()

    async def think(_g, _o, _h, _c):
        calls["think"] += 1
        return decisions[min(calls["think"] - 1, len(decisions) - 1)]

    async def act(*_a, **_k):
        calls["act"] += 1
        return {"ok": True}

    res = asyncio.run(OL.run_loop(
        "verify timer.html", _Session(), observe_fn=observe, think_fn=think,
        act_fn=act, max_steps=max_steps, pin_url=pin_url,
        policy=_safety.SafetyPolicy(allowlist=["localhost"],
                                    allow_destructive=True, confirm=True)))
    return res, calls


def _goto(url):
    return {"action": "goto", "args": {"url": url}, "thought": "t"}


def test_the_loop_never_performs_the_off_pin_navigation():
    """The act function must not be reached - that is what stops the browser
    actually leaving the page."""
    res, calls = _run([_goto("https://localhost:8999/timer.html")] * 12)
    assert calls["act"] == 0
    assert res["ok"] is False


def test_the_loop_stops_instead_of_burning_every_step():
    """The real run spent all twelve steps on this. Four errors is the loop's
    existing ceiling and it should be reached long before max_steps."""
    res, _ = _run([_goto("https://localhost:8999/timer.html")] * 12)
    assert res["reason"] == "too_many_errors"
    assert res["step_count"] < 12


def test_the_model_is_told_the_url_that_works():
    """The error goes into history, so the NEXT prompt carries the right URL -
    that is what lets a run recover rather than guess again."""
    res, _ = _run([_goto("https://localhost:8999/timer.html")] * 12)
    errs = [s for s in res["steps"] if (s.get("result") or {}).get("error")]
    assert errs and PIN in errs[0]["result"]["error"]


def test_a_run_that_recovers_onto_the_pinned_page_continues():
    """One wrong guess must not be fatal: the guard exists to redirect, not to
    end the run."""
    res, calls = _run([_goto("https://localhost:8999/timer.html"),
                       _goto(PIN),
                       {"action": "click", "args": {"ref": "e1"}, "thought": "t"},
                       {"action": "done", "args": {"summary": "counts down"},
                        "thought": "t"}])
    assert res["done"] is True
    assert calls["act"] == 2, "the two legitimate actions ran; the guess did not"


def test_an_unpinned_run_is_completely_unaffected():
    """No pin -> the loop behaves exactly as before. The destination is a local
    host because the safety allowlist, not the pin, governs external ones, and
    conflating the two would make this test pass for the wrong reason."""
    res, calls = _run([_goto("https://localhost:8999/somewhere-else"),
                       {"action": "done", "args": {"summary": "found"},
                        "thought": "t"}],
                      pin_url="")
    assert res["done"] is True and calls["act"] == 1


def test_the_same_navigation_is_refused_only_because_of_the_pin():
    """The pair that isolates the guard: identical decision, identical policy,
    the pin the only difference."""
    off = _goto("https://localhost:8999/somewhere-else")
    done = {"action": "done", "args": {"summary": "x"}, "thought": "t"}
    unpinned, c1 = _run([off, done], pin_url="")
    pinned, c2 = _run([off, done], pin_url=PIN)
    assert unpinned["done"] is True and c1["act"] == 1
    assert pinned["done"] is True and c2["act"] == 0, "the navigation was refused"
