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

#: run62 analyse-data: the same traceback, four times, wrapped by the loop in
#: the "ERROR: " envelope it adds before recording the failure.
TRACEBACK = ('ERROR: Traceback (most recent call last):\n'
             '  File "/workspace/analyze_nums.py", line 63, in <module>\n'
             '    main()\n'
             '  File "/workspace/analyze_nums.py", line 46, in main\n'
             '    std_dev = statistics.stdev(data)\n'
             'statistics.StatisticsError: stdev requires at least two data points\n')

#: The same run's OTHER exception, from the same file. Its first 120 characters
#: are identical to TRACEBACK's - which is exactly why the prefix fallback could
#: not tell them apart.
TRACEBACK_OTHER = ('ERROR: Traceback (most recent call last):\n'
                   '  File "/workspace/analyze_nums.py", line 63, in <module>\n'
                   '    main()\n'
                   '  File "/workspace/analyze_nums.py", line 21, in main\n'
                   '    data.append(int(float(row[0])))\n'
                   'OverflowError: cannot convert float infinity to integer\n')

#: A traceback cut off mid-frame by the preview budget: no exception line to
#: key on, so the prefix fallback still has to group identical ones.
TRACEBACK_CUT = TRACEBACK.rsplit("\n", 2)[0] + "\n"

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


# ?? run62: the reworded SHELL command ???????????????????????????????????????
def test_a_reworded_shell_command_is_the_same_target():
    """The four spellings census run62's analyse-data actually sent.

    `cmd` is in TARGET_ARGS because a command says what is acted on - but for a
    shell it is also the wording, so each rewording used to open its own tally
    and four identical failures never reached the limit."""
    spellings = ["python analyze_nums.py",
                 "cd /workspace && python analyze_nums.py",
                 "python3 ./analyze_nums.py",
                 "  sudo   python3   ./analyze_nums.py  "]
    keys = {RF.target_key("exec.bash.run", {"cmd": c}) for c in spellings}
    assert len(keys) == 1, keys

    seen = {}
    blocked = []
    for c in spellings:
        blocked.append(bool(RF.blocked_kind(seen, "exec.bash.run", {"cmd": c})))
        seen = RF.record(seen, "exec.bash.run", {"cmd": c}, TRACEBACK)
    assert blocked == [False, False, True, True]


def test_a_genuinely_different_command_is_still_new_information():
    """Only noise is normalised away - a different program, flag or operand is
    a different question and must run."""
    k = lambda c: RF.target_key("exec.bash.run", {"cmd": c})          # noqa: E731
    base = k("pytest -q tests/test_stats.py")
    assert base != k("pytest -q tests/test_other.py")     # different operand
    assert base != k("pytest tests/test_stats.py")        # different flags
    assert base != k("python -m pytest -q tests/test_stats.py")
    seen = {}
    for c in ("python a.py", "python a.py"):
        seen = RF.record(seen, "exec.bash.run", {"cmd": c}, TRACEBACK)
    assert RF.blocked_kind(seen, "exec.bash.run", {"cmd": "python b.py"}) == ""


def test_command_signature_keeps_what_decides_the_answer():
    assert RF.command_signature("cd /workspace && python3 ./x.py") == "python x.py"
    assert RF.command_signature("cd /a && cd /b; time python x.py") == "python x.py"
    assert RF.command_signature("/usr/bin/python3 x.py") == "python x.py"
    assert RF.command_signature("PYTHONPATH=. python x.py") == "PYTHONPATH=. python x.py"
    assert RF.command_signature("   ") == ""
    assert RF.command_signature(None) == ""
    assert RF.command_signature("cd /only/a/dir") == "cd /only/a/dir"


# ?? the loop's "ERROR: " envelope ???????????????????????????????????????????
def test_the_error_envelope_does_not_hide_the_reason_code():
    """The loop records `"ERROR: " + str(error)`. The reason-code pattern
    required a lower-case first character, so every kind degraded to a
    120-character prefix and stopped grouping."""
    assert RF.failure_kind(REASON) == "repeating_action"
    assert RF.failure_kind("ERROR: " + REASON) == "repeating_action"
    assert RF.failure_kind("error: " + REASON) == "repeating_action"
    assert RF.failure_kind("Exception: time_budget: stopped after 504s") == "time_budget"


def test_two_operator_failures_group_once_the_envelope_is_stripped():
    """Same reason, different trailing detail - the case the prefix fallback
    could not see, because the url and the counts differ."""
    a = "ERROR: repeating_action: click attempted 5 times on #start, url=/a/timer.html"
    b = "ERROR: repeating_action: click attempted 7 times on #begin, url=/b/timer.html"
    assert RF.failure_kind(a) == RF.failure_kind(b) == "repeating_action"
    seen = {}
    seen = RF.record(seen, "operator.run", _args("one"), a)
    seen = RF.record(seen, "operator.run", _args("two"), b)
    assert RF.blocked_kind(seen, "operator.run", _args("three")) == "repeating_action"


def test_a_traceback_is_keyed_on_its_exception_line():
    """A traceback has no reason code, and its first 120 characters are
    boilerplate plus a file and line - the same for two different exceptions
    raised in the same script. So identical tracebacks must group, and
    different ones must NOT: a run that fixed one bug and hit another was
    being refused its next attempt as a "repeat"."""
    assert RF.failure_kind(TRACEBACK).startswith("statistics.statisticserror")
    assert RF.failure_kind(TRACEBACK_OTHER).startswith("overflowerror")
    assert RF.failure_kind(TRACEBACK) != RF.failure_kind(TRACEBACK_OTHER)
    # the prefix fallback alone could not separate them
    assert TRACEBACK[:120] == TRACEBACK_OTHER[:120]

    args = {"path": "/workspace/analyze_nums.py"}
    seen = {}
    for _ in range(2):
        seen = RF.record(seen, "exec.python.run", args, TRACEBACK)
    assert RF.blocked_kind(seen, "exec.python.run", args) == RF.failure_kind(TRACEBACK)


def test_a_truncated_traceback_falls_back_to_the_prefix():
    """Cut off mid-frame by the preview budget: no exception line to key on,
    so identical ones must still group rather than each counting as new."""
    assert RF._traceback_kind(TRACEBACK_CUT) == ""
    args = {"path": "/workspace/analyze_nums.py"}
    seen = {}
    for _ in range(2):
        seen = RF.record(seen, "exec.python.run", args, TRACEBACK_CUT)
    assert RF.blocked_kind(seen, "exec.python.run", args) != ""
