"""An operator run needs a clock, a relative goto is not a hostname, and a
static file has a cheaper check than a browser.

All three come from census 23, build-browser-verified, which spent over an hour
on one goal:

    cyc4   operator.run  1,344,677 ms  -> max_steps        (22 minutes, ONE call)
    cyc5   operator.run    505,815 ms  -> max_steps: type requires 'text'
    cyc6   operator.run    116,880 ms  -> too_many_errors: type requires 'text'
    cyc26  operator.run    379,878 ms  -> max_steps
    cyc27  operator.run     36,960 ms  -> blocked: host 'form.html' is not a
                                          local/Vera surface
    cyc28  operator.run    397,197 ms  -> no_progress
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import operator_budget as ob        # noqa: E402
from vera.operator import safety as sf                 # noqa: E402
from vera.operator import stop_explanation as se       # noqa: E402

pytestmark = pytest.mark.critical


# ── the clock ───────────────────────────────────────────────────────────────

def test_the_22_minute_run_would_have_been_stopped():
    """cyc4: 1,344,677 ms against a goal budget of 1800s."""
    assert ob.exhausted(started_at=0, now=1344.677, max_seconds=ob.DEFAULT_MAX_SECONDS)


def test_a_run_inside_the_budget_continues():
    """The legitimate runs observed finish inside ~400s."""
    assert ob.exhausted(0, 400, ob.DEFAULT_MAX_SECONDS) is False


def test_the_default_leaves_the_goal_most_of_its_allowance():
    """A 1800s goal must not lose everything to one browser call."""
    assert ob.DEFAULT_MAX_SECONDS < 1800 / 2


def test_zero_means_no_clock():
    """A deliberate long drive must be able to opt out."""
    assert ob.exhausted(0, 10 ** 6, 0) is False
    assert ob.exhausted(0, 10 ** 6, -1) is False


def test_remaining_goes_negative_once_overspent():
    assert ob.remaining(0, 100, 480) == 380
    assert ob.remaining(0, 500, 480) == -20


def test_the_reason_says_it_is_about_TIME_not_the_page():
    """max_steps was read as a verdict on the page; this must not be."""
    d = ob.describe(1344, ob.DEFAULT_MAX_SECONDS, steps_done=9)
    assert "TIME limit" in d and "not evidence about the page" in d
    assert "1344s" in d and "9 step" in d


def test_budget_junk_does_not_raise():
    assert ob.exhausted("x", "y", 480) is False
    assert isinstance(ob.describe("x"), str)


# ── a relative goto is navigation, not a host ───────────────────────────────

def test_a_relative_goto_is_no_longer_blocked_as_a_hostname():
    """cyc27 verbatim: goto 'form.html' from the preview page."""
    pol = sf.SafetyPolicy.for_target("url", "https://localhost:8999")
    cur = "https://localhost:8999/remote/sandbox/preview/abc/index.html"
    out = sf.evaluate(pol, cur, "goto", {"url": "form.html"})
    assert out["allowed"] is True, out["reason"]


def test_the_old_behaviour_really_was_broken():
    """Guard the premise: unresolved, the PATH parses as the host."""
    assert sf.host_of("form.html") == "form.html"
    assert sf.is_local_host("form.html") is False


def test_an_absolute_goto_still_decides_on_its_own_host():
    """Resolving must not make an external destination look local."""
    pol = sf.SafetyPolicy.for_target("url", "https://localhost:8999")
    out = sf.evaluate(pol, "https://localhost:8999/x.html", "goto",
                      {"url": "https://evil.example.com/steal"})
    assert out["allowed"] is False
    assert "evil.example.com" in out["reason"]


def test_a_relative_goto_from_an_EXTERNAL_page_stays_external():
    """The resolution must inherit the current host, not launder it."""
    pol = sf.SafetyPolicy.for_target("url", "https://example.com")
    out = sf.evaluate(pol, "https://example.com/a/b.html", "goto", {"url": "c.html"})
    assert out["allowed"] is False
    assert "example.com" in out["reason"]


def test_an_absolute_path_goto_resolves_against_the_current_origin():
    pol = sf.SafetyPolicy.for_target("url", "https://localhost:8999")
    out = sf.evaluate(pol, "https://localhost:8999/a/b.html", "goto", {"url": "/form.html"})
    assert out["allowed"] is True


def test_an_empty_goto_falls_back_to_the_current_page():
    pol = sf.SafetyPolicy.for_target("url", "https://localhost:8999")
    out = sf.evaluate(pol, "https://localhost:8999/a.html", "goto", {})
    assert out["allowed"] is True


# ── the cheaper check ───────────────────────────────────────────────────────

def test_a_static_file_failure_names_the_python_alternative():
    """Census 21 passed this goal exactly that way after three operator.run
    failures; census 23 kept driving the browser for an hour."""
    out = se.explain("max_steps", [{"reason": "ran out"}]) + se.static_check_hint(
        "https://localhost:8999/remote/sandbox/preview/abc/form.html")
    assert "exec.python.run" in out


def test_the_hint_says_what_the_browser_is_still_for():
    """A hint that just says "use python" would push it off real browser work."""
    assert "cannot establish BEHAVIOUR" in se.STATIC_HINT
    assert "needs a browser" in se.STATIC_HINT


def test_the_hint_requires_reading_from_disk():
    """Census 21 cyc12 pasted a COPY of the HTML into its own source and tested
    that. Reading the artifact is the difference between checking the file and
    checking a string literal."""
    assert "FROM DISK" in se.STATIC_HINT


def test_the_hint_refuses_a_self_declared_pass():
    """Census 21 cyc12 printed "SUCCESS CRITERION MET: PASS" and the verifier
    quoted it back as its reason. A script asserting its own success is the
    false positive this whole hint nearly encouraged."""
    assert "printing PASS is not evidence" in se.STATIC_HINT
    assert "not a verdict you printed about yourself" in se.STATIC_HINT


def test_an_application_page_gets_no_such_hint():
    out = se.explain("max_steps", [{"reason": "ran out"}]) + se.static_check_hint(
        "https://localhost:8999/chat")
    assert "exec.python.run" not in out


def test_a_query_string_does_not_hide_the_extension():
    assert se.static_check_hint("https://h/a/form.html?x=1#y") != ""


def test_time_budget_is_an_unsuccessful_stop_and_is_explained():
    assert "time_budget" in se.UNSUCCESSFUL
    out = se.explain("time_budget", [])
    assert "TIME budget" in out


def test_a_successful_run_still_explains_nothing():
    assert se.explain("done", [{"phase": "done"}]) == ""


# ── call sites ──────────────────────────────────────────────────────────────

def _read(*parts):
    with open(os.path.join(os.path.dirname(__file__), "..", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_loop_checks_the_clock_before_each_step():
    """After the step it could overshoot by a whole generation - which is the
    22 minutes."""
    src = _read("vera", "operator", "operator_loop.py")
    body = src[src.index("for i in range(1, max_steps + 1):"):]
    # The call APPEARING is not enough - it must be the live condition and it
    # must break. `if False and _budget.exhausted(...)` still contains the call.
    assert "if _budget is not None and _budget.exhausted(" in body[:900]
    guard = body[body.index("if _budget is not None and _budget.exhausted("):]
    assert "break" in guard[:600], "the budget check must end the run"
    # and it must come before the model call, or it overshoots by a generation
    assert body.index("_budget.exhausted(") < body.index("decision = await think(")


def test_a_failed_budget_import_cannot_break_the_signature():
    """A default evaluated at import cannot reach through a failed import."""
    src = _read("vera", "operator", "operator_loop.py")
    assert "max_seconds: float = _OP_MAX_SECONDS" in src
    assert 'getattr(_budget, "DEFAULT_MAX_SECONDS", 480)' in src


def test_the_cap_lets_a_caller_tighten_the_budget():
    """The knob still exists - but the value is floored on the way in now.

    This used to assert the cap passed float(max_seconds) straight through,
    which is exactly what let census 24's model ask for 95 seconds and kill
    its own run at 103. budget_kwargs replaced that raw forward; tightening
    is still allowed, starving is not. See tests/test_progress_content.py.
    """
    src = _read("vera", "operator", "operator_web_capabilities.py")
    assert "max_seconds: float = 0" in src
    assert "_op_budget.budget_kwargs(max_seconds)" in src
    assert ob.budget_kwargs(600) == {"max_seconds": 600.0}


def test_the_explanation_is_given_the_page_url():
    src = _read("vera", "operator", "operator_loop.py")
    assert "static_check_hint" in src and "_last_url" in src
