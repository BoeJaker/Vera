"""The census recorded completion and called it success.

done / wall-cap / error says whether a run STOPPED, not whether it built
anything. Scored retrospectively over census 33, the two readings disagree:

    underspecified    wall-cap  quality 1.0   - produced a working timer.html
    build-multifile   done      quality 0.667 - finished, one expectation unmet
    research-report   wall-cap  quality 0.0   - produced nothing
    long-horizon      wall-cap  quality 0.6   - built index.html, 3 of 5

9 of 12 "done" against a mean quality of 0.86, and a goal marked failed that
had in fact done the job.

Checks are DECLARED and objective - no LLM judgement anywhere. A model scoring
its own output is the false positive the verifier gate exists to prevent, and
putting one inside the measuring instrument would make the instrument as
unreliable as the thing it measures.

Pure: no I/O, no network.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.census.quality import (evaluate, evaluate_one,      # noqa: E402
                                 files_wanted)

pytestmark = pytest.mark.critical

CLOCK = {"/workspace/clock.html":
         "<html><script>setInterval(tick,1000);d.getHours()</script>"
         "<button id=fmt>24h</button></html>"}


# ── the scoring contract ───────────────────────────────────────────────────

def test_a_goal_with_no_checks_is_unmeasured_not_zero_and_not_perfect():
    """None, deliberately. A 0 would drag the mean down for a goal nobody
    wrote expectations for; a 1 would inflate it. Unmeasured must LOOK
    unmeasured."""
    r = evaluate([], {}, "")
    assert r["score"] is None and r["total"] == 0


def test_the_score_is_the_fraction_of_checks_that_passed():
    r = evaluate([{"file": "clock.html", "exists": True},
                  {"file": "clock.html", "contains": "nope"}], CLOCK)
    assert r["score"] == 0.5 and r["passed"] == 1 and r["failed"] == 1


def test_every_result_carries_a_reason_a_human_can_act_on():
    r = evaluate([{"file": "clock.html", "contains": "nope",
                   "why": "the clock actually ticks"}], CLOCK)
    res = r["results"][0]
    assert res["ok"] is False
    assert res["label"] == "the clock actually ticks"
    assert res["detail"], "a failure with no detail is a number, not a finding"


# ── file lookup ────────────────────────────────────────────────────────────

def test_a_file_is_found_by_basename_not_just_exact_path():
    """The run writes /workspace/clock.html; the check names clock.html."""
    assert evaluate([{"file": "clock.html", "exists": True}], CLOCK)["score"] == 1.0


def test_a_missing_file_fails_every_check_about_it_with_the_same_reason():
    r = evaluate([{"file": "gone.html", "exists": True},
                  {"file": "gone.html", "contains": "x"}], CLOCK)
    assert r["passed"] == 0
    assert all("no such file" in x["detail"] for x in r["results"])


# ── the check kinds ────────────────────────────────────────────────────────

def test_contains_is_case_insensitive():
    assert evaluate_one({"file": "clock.html", "contains": "SETINTERVAL"}, CLOCK)["ok"]


def test_absent_catches_what_should_not_be_there():
    assert evaluate_one({"file": "clock.html", "absent": "TODO"}, CLOCK)["ok"]
    assert not evaluate_one({"file": "clock.html", "absent": "setInterval"}, CLOCK)["ok"]


def test_any_of_needs_one_and_reports_which():
    r = evaluate_one({"file": "clock.html", "any_of": ["nope", "setInterval"]}, CLOCK)
    assert r["ok"] and "setInterval" in r["detail"]
    assert not evaluate_one({"file": "clock.html",
                             "any_of": ["zzz_absent", "qqq_absent"]}, CLOCK)["ok"]


def test_all_of_needs_every_one_and_names_the_missing():
    r = evaluate_one({"file": "clock.html", "all_of": ["setInterval", "nope"]}, CLOCK)
    assert not r["ok"] and "nope" in r["detail"]


def test_min_bytes_rejects_a_stub_and_says_the_size():
    r = evaluate_one({"file": "clock.html", "min_bytes": 10 ** 6}, CLOCK)
    assert not r["ok"] and "bytes" in r["detail"]


def test_regex_matches_across_lines():
    assert evaluate_one({"file": "clock.html", "regex": "script.*button"}, CLOCK)["ok"]


def test_a_broken_regex_in_the_CHECK_is_reported_not_raised():
    """A malformed expectation must not crash the census."""
    r = evaluate_one({"file": "clock.html", "regex": "("}, CLOCK)
    assert r["ok"] is False and "invalid" in r["detail"]


# ── answer checks ──────────────────────────────────────────────────────────

def test_answer_contains_reads_the_runs_prose():
    assert evaluate_one({"answer_contains": "391"}, {}, "The answer is 391.")["ok"]
    assert not evaluate_one({"answer_contains": "391"}, {}, "about four hundred")["ok"]


def test_answer_min_words_rejects_a_one_liner_and_says_the_count():
    r = evaluate_one({"answer_min_words": 100}, {}, "too short")
    assert not r["ok"] and "2 words" in r["detail"]


def test_answer_regex_finds_a_citation():
    assert evaluate_one({"answer_regex": "https?://"}, {}, "see https://x.dev")["ok"]


# ── things that must never silently pass ───────────────────────────────────

def test_an_unknown_check_kind_fails_loudly():
    """A check that quietly does nothing is worse than no check - it looks
    like coverage."""
    r = evaluate_one({"file": "clock.html", "vibes": "good"}, CLOCK)
    assert r["ok"] is False and "unsupported" in r["detail"]


def test_a_check_naming_neither_a_file_nor_the_answer_fails():
    r = evaluate_one({"contains": "x"}, CLOCK)
    assert r["ok"] is False


def test_files_wanted_lists_what_the_caller_must_fetch():
    checks = [{"file": "a.html", "exists": True}, {"file": "a.html", "contains": "x"},
              {"answer_contains": "y"}, {"file": "b.md", "exists": True}]
    assert files_wanted(checks) == ["a.html", "b.md"]
    assert files_wanted(None) == []


def test_nothing_here_asks_a_model_for_a_verdict():
    """The instrument must not depend on the thing it measures.

    Checked on the AST, not on the text: the module's own docstring explains
    WHY there is no model here, and a substring scan trips on the explanation.
    """
    import ast
    import inspect
    from vera.census import quality
    tree = ast.parse(inspect.getsource(quality))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    for banned in ("ollama", "httpx", "requests", "openai", "vera"):
        assert banned not in imported, "quality scoring imported %s" % banned
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for banned in ("generate", "embed", "complete", "ask"):
        assert banned not in called, "quality scoring called %s()" % banned


# ── run-level rollup ───────────────────────────────────────────────────────

from vera.census.census_core import (quality_rollup,          # noqa: E402
                                     failure_causes, summarise_run)


def _rec(score, passed=0, total=0, **kw):
    r = {"status": "done", "warnings": []}
    r.update(kw)
    if score is not None or total:
        r["quality"] = {"score": score, "passed": passed, "total": total}
    return r


def test_unmeasured_goals_are_excluded_from_the_mean_not_counted_as_zero():
    """Census 33's records predate quality entirely; the run must read as
    unscored, not as a run that scored 0."""
    roll = quality_rollup([_rec(None), _rec(None)])
    assert roll["quality_mean"] is None and roll["quality_scored"] == 0


def test_the_mean_says_how_many_goals_it_covers():
    roll = quality_rollup([_rec(1.0, 3, 3), _rec(0.5, 1, 2), _rec(None)])
    assert roll["quality_mean"] == 0.75
    assert roll["quality_scored"] == 2, "a mean over 2 of 3 must say so"
    assert roll["checks_passed"] == 4 and roll["checks_total"] == 5


def test_perfect_goals_are_counted_separately_from_the_mean():
    roll = quality_rollup([_rec(1.0, 2, 2), _rec(1.0, 1, 1), _rec(0.0, 0, 2)])
    assert roll["quality_perfect"] == 2


def test_a_run_summary_carries_quality_and_causes():
    s = summarise_run("runX", [_rec(1.0, 2, 2), _rec(0.0, 0, 2)])
    assert s["quality_mean"] == 0.5
    assert "causes" in s


# ── failure taxonomy ───────────────────────────────────────────────────────

def test_causes_group_the_warnings_a_person_would_group():
    recs = [{"warnings": [
        "step 2: code.edit FAILED - edit 1: `find` text not present in the file",
        "step 3: code.edit FAILED - edit 1: `find` text not present in the file",
        "step 7: operator.run FAILED - time_budget: stopped after 480s",
        "step 1: exec.bash.run FAILED - rc=1",
    ]}]
    c = failure_causes(recs)
    assert c["code.edit: anchor missing"] == 2
    assert c["operator: out of time"] == 1
    assert c["exec failed"] == 1


def test_a_shape_warning_is_not_counted_as_a_failure():
    """'called 3x' describes how a step behaved, not a fault - counting it
    would make every run look broken."""
    assert failure_causes([{"warnings": ["step 1: web.fetch called 3x"]}]) == {}


def test_an_unrecognised_failure_lands_in_other_rather_than_vanishing():
    c = failure_causes([{"warnings": ["step 1: something.odd FAILED - who knows"]}])
    assert c == {"other": 1}


def test_causes_are_ordered_most_frequent_first():
    recs = [{"warnings": ["a FAILED exec.bash.run FAILED"] * 1
                         + ["x FAILED `find` text not present in the file"] * 3}]
    assert list(failure_causes(recs))[0] == "code.edit: anchor missing"


def test_the_specific_pattern_wins_over_the_general():
    """A code.edit JSON failure must not be filed as 'other'."""
    c = failure_causes([{"warnings":
        ["step 1: code.edit FAILED - the editor's reply was not the requested JSON object"]}])
    assert c == {"code.edit: reply unusable": 1}
