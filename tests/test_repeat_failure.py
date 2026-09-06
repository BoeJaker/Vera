"""Three identical answers for 1807 seconds.

Census 39, author-then-edit. Steps 1 and 2 took ONE cycle each and produced a
correct artifact - 4/4 on quality, including the display check that had caught
the half-edit the run before. Then step 3:

    operator.run -> repeating_action: click attempted 5 times ... same url
    operator.run -> repeating_action: click attempted 5 times ... same url
    operator.run -> repeating_action: click attempted 5 times ... same url

Same target, same reason, three times, and the whole wall cap. The deliverable
had been finished after two cycles.

The existing guard keys on tool + ARGS, so it catches a verbatim repeat. The
executor rewords `goal` every attempt, so the signature differed each time even
though the question did not. This keys on what determines the answer instead:
the tool, the target, and the KIND of failure.

Pure: dicts in, decisions out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag import repeat_failure as RF                  # noqa: E402

URL = "https://localhost:8999/remote/sandbox/preview/abc/timer.html"
REASON = ("repeating_action: click was attempted 5 times (limit 5) without the "
          "page's structure changing -- same url, same title, same elements")


def _args(goal):
    return {"goal": goal, "url": URL}


# ── the census 39 case ──────────────────────────────────────────────────────
def test_a_reworded_repeat_is_recognised_as_the_same_question():
    """The three real goals differed only in prose."""
    seen = {}
    seen = RF.record(seen, "operator.run", _args("Click Start and observe"), REASON)
    seen = RF.record(seen, "operator.run", _args("Load timer.html and verify"), REASON)
    assert RF.blocked_kind(seen, "operator.run",
                           _args("Confirm the countdown begins")) == "repeating_action"


def test_the_first_two_attempts_are_allowed():
    """One failure can be bad luck; two is the answer."""
    seen = {}
    assert RF.blocked_kind(seen, "operator.run", _args("a")) == ""
    seen = RF.record(seen, "operator.run", _args("a"), REASON)
    assert RF.blocked_kind(seen, "operator.run", _args("b")) == ""


def test_the_prose_is_not_part_of_the_key():
    """Exactly why the existing signature never matched."""
    assert (RF.target_key("operator.run", _args("one")) ==
            RF.target_key("operator.run", _args("two")))


def test_the_target_is_part_of_the_key():
    other = {"goal": "x", "url": URL.replace("abc", "zzz")}
    assert RF.target_key("operator.run", _args("x")) != RF.target_key("operator.run", other)


def test_a_different_target_is_not_blocked():
    """A genuinely different page is new information."""
    seen = {}
    for g in ("a", "b"):
        seen = RF.record(seen, "operator.run", _args(g), REASON)
    other = {"goal": "x", "url": URL.replace("abc", "zzz")}
    assert RF.blocked_kind(seen, "operator.run", other) == ""


def test_a_different_failure_kind_is_not_blocked():
    """Same page failing a NEW way is worth another call."""
    seen = {}
    for g in ("a", "b"):
        seen = RF.record(seen, "operator.run", _args(g), REASON)
    assert RF.blocked_kind(seen, "operator.run", _args("c"), limit=2) == "repeating_action"
    fresh = RF.record({}, "operator.run", _args("a"), "time_budget: stopped after 579s")
    assert RF.blocked_kind(fresh, "operator.run", _args("b")) == ""


def test_a_different_tool_is_not_blocked():
    seen = {}
    for g in ("a", "b"):
        seen = RF.record(seen, "operator.run", _args(g), REASON)
    assert RF.blocked_kind(seen, "browser.navigate", _args("c")) == ""


# ── what counts as the same kind ────────────────────────────────────────────
def test_the_reason_code_is_the_kind():
    assert RF.failure_kind(REASON) == "repeating_action"
    assert RF.failure_kind("time_budget: stopped after 579s") == "time_budget"


def test_numbers_do_not_make_two_failures_different():
    """"stopped after 579s" and "after 488s" are the same answer."""
    a = RF.failure_kind("stopped after 579 seconds having taken 13 steps")
    b = RF.failure_kind("stopped after 488 seconds having taken 9 steps")
    assert a == b


def test_genuinely_different_messages_are_different_kinds():
    assert RF.failure_kind("connection refused") != RF.failure_kind("permission denied")


def test_an_empty_error_records_nothing():
    """A failure with no message must not group everything together."""
    assert RF.failure_kind("") == ""
    assert RF.record({}, "t", {}, "") == {}
    assert RF.count({}, "t", {}, "") == 0


# ── what the executor is told ───────────────────────────────────────────────
def test_the_message_quotes_the_answer_it_already_had():
    d = RF.describe("operator.run", "repeating_action", REASON)
    assert "repeating_action" in d
    assert "same url" in d, "the actual error is quoted back"


def test_the_message_offers_the_real_alternatives():
    d = RF.describe("operator.run", "repeating_action", REASON)
    assert "fix the underlying file" in d
    assert "done" in d


def test_counting_is_per_target_and_kind():
    seen = RF.record({}, "operator.run", _args("a"), REASON)
    seen = RF.record(seen, "operator.run", _args("b"), REASON)
    assert RF.count(seen, "operator.run", _args("c"), REASON) == 2
