"""A benchmark task must be able to assert what it PRODUCED, not just what it said.

The census and the Loop Lab suite are the same system built twice. The suite has
the storage, the runner and the whole UI; the census has all 41 runs of real
data and a richer scoring vocabulary. Reviewed 2026-09-07: `evolve.suites` and
`evolve.runs` were both EMPTY - the suite has never been run - while the census
ran outside the repo with no UI at all.

This is the first step of the merge: the suite's checks could only read the
ANSWER and the TOOL TRACE, so a task could not assert anything about the file a
run wrote. That is precisely the gap the census closed. Census 38,
`author-then-edit`: the run scored 100% on "the edit to 90 seconds landed"
while the page still DISPLAYED the old value, because nothing looked at the
artifact. An `absent` check on the file caught it and the score dropped to 75%.

So file checks are delegated to `vera.census.quality` rather than
reimplemented - one evaluator, already tested, already in the critical tier -
and the answer/trace checks stay where they are, because the census has no
`cap_called`, `min_steps` or `json_valid`.

Pure: no runner, no sandbox. The files are passed in, exactly as the census
harness passes them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve.evolve_capabilities import (                # noqa: E402
    _run_checks, _files_wanted, _census_check_kind, _census_check_value,
)

TIMER_HALF_EDITED = (
    "<div class='timer' id='timer'>01:00</div>\n"
    "let remainingSeconds = 90;\n"
    "setInterval(updateTimer, 1000);\n"
)
TIMER_FIXED = TIMER_HALF_EDITED.replace(">01:00<", ">01:30<")


def _task(*checks):
    return {"id": "t", "type": "loop", "checks": list(checks)}


def _score(task, files=None, final="", steps=None, elapsed=1.0):
    return _run_checks(task, final, steps or [], elapsed, files=files or {})


# ── the census 38 case, now expressible as a suite task ─────────────────────
def test_a_file_check_catches_the_half_edited_artifact():
    task = _task({"file": "timer.html", "absent": "01:00",
                  "why": "the DISPLAYED value was updated too"})
    bad = _score(task, {"timer.html": TIMER_HALF_EDITED})
    good = _score(task, {"timer.html": TIMER_FIXED})
    assert bad[0]["ok"] is False
    assert good[0]["ok"] is True


def test_the_old_vocabulary_alone_would_have_passed_it():
    """Why the file check is needed: a regex over the ANSWER cannot see the
    artifact at all."""
    task = _task({"type": "regex", "value": r"\b90\b"})
    assert _score(task, final="I set the countdown to 90 seconds.")[0]["ok"] is True


def test_a_missing_file_fails_honestly():
    task = _task({"file": "timer.html", "exists": True})
    r = _score(task, {})[0]
    assert r["ok"] is False
    assert "no such file" in r["note"]


def test_every_census_kind_is_accepted():
    checks = [
        {"file": "a.txt", "exists": True},
        {"file": "a.txt", "min_bytes": 3},
        {"file": "a.txt", "contains": "hello"},
        {"file": "a.txt", "absent": "goodbye"},
        {"file": "a.txt", "any_of": ["hello", "zzz"]},
        {"file": "a.txt", "all_of": ["hello", "world"]},
        {"file": "a.txt", "regex": r"h\w+o"},
    ]
    res = _score(_task(*checks), {"a.txt": "hello world"})
    assert all(r["ok"] for r in res), [r for r in res if not r["ok"]]
    assert "unknown check type" not in " ".join(r["note"] for r in res)


def test_answer_level_census_kinds_reach_the_census_evaluator():
    """answer_min_words has no suite equivalent and must not fall through to
    the 'unknown check type' branch."""
    task = _task({"answer_min_words": 3}, {"answer_contains": "webgpu"})
    res = _score(task, final="WebGPU is supported in Chrome")
    assert [r["ok"] for r in res] == [True, True]


# ── the suite's own vocabulary must survive ─────────────────────────────────
def test_trace_checks_are_untouched():
    """cap_called / min_steps / json_valid have no census equivalent - the merge
    must not trade them away."""
    steps = [{"cap": "code.author"}, {"cap": "exec.python.run"}]
    res = _score(_task({"type": "cap_called", "value": "code.author"},
                       {"type": "min_steps", "value": 2}), steps=steps)
    assert [r["ok"] for r in res] == [True, True]


def test_an_unknown_suite_check_still_reports_itself():
    r = _score(_task({"type": "nonsense", "value": 1}))[0]
    assert r["ok"] is False and "unknown check type" in r["note"]


def test_a_task_may_mix_both_vocabularies():
    """The point of the merge: assert what was produced AND how it got there,
    which neither system could do alone."""
    res = _score(_task({"file": "timer.html", "contains": "setInterval"},
                       {"type": "cap_called", "value": "code.author"}),
                 files={"timer.html": TIMER_FIXED},
                 steps=[{"cap": "code.author"}])
    assert [r["ok"] for r in res] == [True, True]


# ── plumbing ────────────────────────────────────────────────────────────────
def test_files_wanted_names_what_must_be_fetched():
    task = _task({"file": "timer.html", "exists": True},
                 {"file": "notes.md", "contains": "x"},
                 {"type": "cap_called", "value": "y"})
    assert sorted(_files_wanted(task)) == ["notes.md", "timer.html"]


def test_a_task_with_no_file_checks_fetches_nothing():
    assert _files_wanted(_task({"type": "cap_called", "value": "y"})) == []


def test_the_reported_kind_and_value_name_the_check():
    """The Runs UI renders these, so they must not all come back as 'file'."""
    assert _census_check_kind({"file": "a", "absent": "x"}) == "absent"
    assert _census_check_value({"file": "a", "absent": "x"}) == "x"
    assert _census_check_kind({"answer_min_words": 80}) == "answer_min_words"


def test_the_label_survives_for_the_ui():
    """`why` is what makes a failure read as a finding rather than a number."""
    task = _task({"file": "timer.html", "exists": True, "why": "timer was written"})
    assert _score(task, {"timer.html": "x"})[0]["label"] == "timer was written"
