"""What landed between two censuses, without anyone remembering to label it.

The panel's "fixes per run" reads board items tagged census:fixed:<run>. Those
exist for run10-prefixes and `current` and stop there - nothing broke, the
convention simply stopped being followed; every fix landed across censuses
28-34 should have carried one and none did. A record that depends on somebody
remembering will always end up that way, so this derives it from the repository.

The subtlety that broke the first version of this module, measured on real
data 2026-09-05:

    fix/an-anchor…  committed 13:26   (onto bleeding-edge)
    census 33       ran 10:07-13:45
    prod restarted  13:57             <- when the code actually ran

By commit time alone that fix is credited to a census it could not have
affected. Commits made while a run was in flight are therefore FLAGGED, not
silently attributed.

Pure: no git, no subprocess, no filesystem.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.census.landed import (GIT_FORMAT, SEP, assign,      # noqa: E402
                                parse_log, summarise)

pytestmark = pytest.mark.critical


def _line(sha, ts, subject):
    return SEP.join([sha, str(ts), subject])


MERGE = "Loop Lab: merge fix/thing (pipeline ab12)"


# ── parsing ────────────────────────────────────────────────────────────────

def test_a_pipeline_merge_yields_its_branch_and_pipeline():
    c = parse_log(_line("abc1234567", 1000, MERGE))[0]
    assert c["branch"] == "fix/thing" and c["pipeline"] == "ab12"
    assert c["is_merge"] is True and c["sha"] == "abc1234567"


def test_a_plain_commit_is_kept_but_not_called_a_merge():
    c = parse_log(_line("def4567890", 1000, "tidy up"))[0]
    assert c["is_merge"] is False and c["branch"] == ""


def test_a_malformed_line_is_skipped_rather_than_guessed_at():
    """A half-parsed commit attributed to the wrong run is worse than a
    missing one."""
    text = "\n".join(["not-a-real-line", _line("aaa1111111", 5, MERGE),
                      SEP.join(["bbb", "not-a-number", "x"])])
    got = parse_log(text)
    assert [c["sha"] for c in got] == ["aaa1111111"]


def test_the_format_string_and_the_parser_cannot_drift():
    assert GIT_FORMAT.split(SEP) == ["%H", "%ct", "%s"]
    assert len(parse_log(_line("a" * 10, 1, "s"))) == 1


# ── attribution ────────────────────────────────────────────────────────────

#: r0 exists only to give r1 a predecessor. The EARLIEST run in any list has
#: no earlier boundary and therefore reports nothing - see below.
RUNS = [{"run_id": "r0", "ended_at": 500, "started_at": 400},
        {"run_id": "r1", "ended_at": 1000, "started_at": 900},
        {"run_id": "r2", "ended_at": 2000, "started_at": 1800}]


def test_a_commit_belongs_to_the_first_run_that_finished_after_it():
    cs = parse_log("\n".join([_line("a" * 10, 700, MERGE), _line("b" * 10, 1500, MERGE)]))
    by = assign(cs, RUNS)
    assert [c["sha"] for c in by["r1"]["commits"]] == ["a" * 10]
    assert [c["sha"] for c in by["r2"]["commits"]] == ["b" * 10]


def test_the_earliest_run_reports_nothing_because_its_window_is_unknown():
    """It has no predecessor, so claiming every commit back to the first in the
    repository would be a confident wrong answer."""
    by = assign(parse_log(_line("a" * 10, 10, MERGE)), RUNS)
    assert by["r0"]["count"] == 0
    assert by["r0"]["window_from"] is None, "an unknown window must say so"


def test_commits_older_than_every_run_are_dropped():
    by = assign(parse_log(_line("a" * 10, 10, MERGE)), RUNS)
    assert sum(v["count"] for v in by.values()) == 0


def test_a_commit_made_mid_run_is_flagged_not_silently_credited():
    """The case that broke the first version: committed 13:26, run ended
    13:45, prod restarted 13:57 - it could not have affected that run."""
    by = assign(parse_log(_line("a" * 10, 1900, MERGE)), RUNS)
    c = by["r2"]["commits"][0]
    assert c["during_run"] is True
    assert by["r2"]["during_run"] == 1
    assert "already in flight" in summarise(by["r2"])


def test_a_commit_made_before_the_run_started_carries_no_flag():
    by = assign(parse_log(_line("a" * 10, 1100, MERGE)), RUNS)
    c = by["r2"]["commits"][0]
    assert c["during_run"] is False
    assert "in flight" not in summarise(by["r2"])


def test_a_run_with_no_derivable_start_never_flags():
    """Rather than guess, say nothing - the flag must be believable when it
    does appear."""
    runs = [{"run_id": "r0", "ended_at": 500},
            {"run_id": "r1", "ended_at": 1000},
            {"run_id": "r2", "ended_at": 2000, "started_at": None}]
    by = assign(parse_log(_line("a" * 10, 1500, MERGE)), runs)
    assert by["r2"]["commits"][0]["during_run"] is False


def test_commits_are_newest_first_within_a_run():
    cs = parse_log("\n".join([_line("a" * 10, 1100, MERGE), _line("b" * 10, 1900, MERGE)]))
    by = assign(cs, RUNS)
    assert [c["ts"] for c in by["r2"]["commits"]] == [1900, 1100]


def test_runs_given_out_of_order_are_still_windowed_correctly():
    by = assign(parse_log(_line("a" * 10, 1500, MERGE)), list(reversed(RUNS)))
    assert by["r2"]["count"] == 1 and by["r1"]["count"] == 0


def test_a_run_with_no_end_time_is_ignored_rather_than_crashing():
    assert assign([], [{"run_id": "x"}]) == {}


# ── the summary line ───────────────────────────────────────────────────────

def test_the_summary_names_branches_rather_than_counting_commits():
    """'4 commits' says nothing about whether the run should have improved."""
    cs = parse_log("\n".join([
        _line("a" * 10, 1100, "Loop Lab: merge fix/one (pipeline a1)"),
        _line("b" * 10, 1150, "Loop Lab: merge fix/two (pipeline b2)")]))
    text = summarise(assign(cs, RUNS)["r2"])
    assert "fix/one" in text and "fix/two" in text


def test_an_empty_run_says_so_plainly():
    assert "nothing landed" in summarise({"commits": []})
    assert "nothing landed" in summarise(None)


def test_commits_with_no_pipeline_merge_are_reported_honestly():
    cs = parse_log(_line("a" * 10, 1100, "a plain commit"))
    assert "none of them a pipeline merge" in summarise(assign(cs, RUNS)["r2"])
