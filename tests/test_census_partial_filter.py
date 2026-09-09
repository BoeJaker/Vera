"""Most of the census archive is not usable history, and it all looks alike.

Of the runs on disk on 2026-09-09, these ran a FULL twelve goals and are still
worthless as a data point:

    census.run41-failed-network-contention.jsonl   12 goals
    census.run43-failed-disk-full.jsonl            12 goals
    census.run6-stalled.jsonl                      12 goals

A goal-count cannot see any of that. The only record of why those runs are bad
is the name the archive gave them, so that is what is read here.

The pre-existing `partial` flag means one narrow thing - FEWER GOALS than the
fullest pass - and the panel and its tests already depend on that meaning, so it
keeps it. `excluded` is the union the caller actually wants, and
`exclude_reason` says which of the two applied.

Pure: no files, no capability, no HTTP.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.census import census_core as CC              # noqa: E402

pytestmark = pytest.mark.critical


def _sum(run_id, goals, done=0, reconcile=True):
    return {"run_id": run_id, "goals": goals, "done": done,
            "counters_reconcile": reconcile}


# ── a name is a verdict a goal-count cannot reach ───────────────────────────
def test_a_full_length_run_named_failed_is_excluded():
    """run41 covered all twelve goals under network contention. Nothing in the
    numbers says so; the filename is the only place it was recorded."""
    assert CC.name_marks_unusable("run41-failed-network-contention") == "failed"
    assert CC.exclude_reason("run41-failed-network-contention", 12, 12) == \
        "the run is named 'failed'"


def test_every_marker_the_archive_actually_uses_is_recognised():
    """Taken from real filenames, not invented."""
    for rid, marker in [("run3-partial", "partial"),
                        ("run6-stalled", "stalled"),
                        ("run7-wedged", "wedged"),
                        ("run14-aborted-wedged-gpu", "aborted"),
                        ("run25-interrupted", "interrupted"),
                        ("run27-invalid-stall", "invalid"),
                        ("run43-failed-disk-full", "failed")]:
        assert CC.name_marks_unusable(rid) == marker, rid


def test_an_ordinary_run_is_not_excluded():
    assert CC.name_marks_unusable("run49") == ""
    assert CC.name_marks_unusable("run10-prefixes") == ""
    assert CC.exclude_reason("run49", 12, 12) == ""


def test_a_short_run_is_excluded_and_says_how_short():
    assert CC.exclude_reason("run44", 3, 12) == "only 3 of 12 goals"


def test_the_live_run_is_excluded_while_it_is_still_going():
    """`census.jsonl` is partial for the hours a run takes."""
    assert CC.exclude_reason("current", 4, 12) == "only 4 of 12 goals"


def test_shortness_is_reported_before_the_name():
    """A run that is BOTH short and named partial should say the concrete
    thing - how far it actually got."""
    assert CC.exclude_reason("run9-partial", 2, 12) == "only 2 of 12 goals"


# ── history() keeps `partial` narrow and adds `excluded` ────────────────────
def test_history_separates_short_from_named_bad():
    h = CC.history([_sum("run41-failed-network-contention", 12, done=9),
                    _sum("run44", 3, done=1),
                    _sum("run48", 12, done=9),
                    _sum("run49", 12, done=11)])
    by = {r["run_id"]: r for r in h["runs"]}
    assert by["run44"]["partial"] is True          # short
    assert by["run41-failed-network-contention"]["partial"] is False   # full length
    assert by["run41-failed-network-contention"]["excluded"] is True   # but named bad
    assert by["run49"]["excluded"] is False
    assert h["short_count"] == 1
    assert h["named_bad_count"] == 1
    assert h["excluded_count"] == 2
    assert h["complete_count"] == 2


def test_a_named_bad_run_is_kept_out_of_the_trend():
    """The whole point: run41 sitting in the trend is a wrong number, not a
    missing one."""
    h = CC.history([_sum("run41-failed-network-contention", 12, done=2),
                    _sum("run48", 12, done=9),
                    _sum("run49", 12, done=11)])
    t = h["trend_complete"]
    assert t["from"] == "run48" and t["to"] == "run49"
    assert t["delta"] == 2


def test_every_run_still_appears_in_the_rows():
    """history() marks; it never drops. Filtering is the caller's choice, so
    the counts stay computed over everything."""
    h = CC.history([_sum("run41-failed-network-contention", 12),
                    _sum("run49", 12)])
    assert h["count"] == 2 and len(h["runs"]) == 2


# ── the flag is a query string, not a bool ──────────────────────────────────
def test_the_flag_reads_the_string_a_query_parameter_really_is():
    """?include_partial=false arrives as the STRING "false". A bare truth test
    would read that as ON and the filter would never filter."""
    assert CC.truthy("false") is False
    assert CC.truthy("0") is False
    assert CC.truthy("") is False
    assert CC.truthy(None) is False
    assert CC.truthy("true") is True
    assert CC.truthy("True") is True
    assert CC.truthy("1") is True
    assert CC.truthy("yes") is True
    assert CC.truthy("on") is True
    assert CC.truthy(True) is True
    assert CC.truthy(False) is False
