"""Memory is searched in the time a question names, then wider, then all time (roadmap CU-M.9)."""
import asyncio
import importlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.fabric import recall_scope_core as S  # noqa: E402

NOW = datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc)          # a Tuesday
DAY = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,since,until,phrase", [
    ("what did we discuss yesterday?", DAY - timedelta(days=1), DAY, "yesterday"),
    ("the plan from earlier today", DAY, NOW, "earlier today"),
    ("what did I ask last week", DAY - timedelta(days=8), DAY - timedelta(days=1), "last week"),
    ("anything this week about markets", DAY - timedelta(days=1), NOW, "this week"),
    ("in the last 3 days", DAY - timedelta(days=3), NOW, "in the last 3 days"),
    ("notes from last month", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc),
     "last month"),
    ("what we said on 2026-09-30", datetime(2026, 9, 30, tzinfo=timezone.utc),
     datetime(2026, 10, 1, tzinfo=timezone.utc), "on 2026-09-30"),
    ("the scan back in March", datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 4, 1, tzinfo=timezone.utc),
     "back in March"),
    ("the report in december", datetime(2025, 12, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc),
     "in december"),
    ("what have we done recently", DAY - timedelta(days=14), NOW, "recently"),
])
def test_time_phrases(text, since, until, phrase):
    sc = S.time_scope(text, NOW)
    assert (sc["since"], sc["until"], sc["phrase"]) == (since, until, phrase)


def test_two_weeks_ago_is_a_window_around_then():
    sc = S.time_scope("the pokedex two weeks ago", NOW)
    assert sc["since"] < DAY - timedelta(days=14) < sc["until"] <= NOW


def test_no_time_no_scope():
    assert S.time_scope("what is the current bash user", NOW) is None
    assert S.time_scope("", NOW) is None


def test_the_phrase_leaves_the_text_query():
    assert S.strip("what did we discuss yesterday?", "yesterday") == "what did we discuss"
    assert S.strip("yesterday", "yesterday") == "yesterday"         # never an empty query


def test_widening_grows_around_the_window_and_stops_at_now():
    sc = S.time_scope("what did we discuss yesterday", NOW)
    w = S.widen(sc, NOW)
    assert w["since"] == sc["since"] - timedelta(days=1.5) and w["until"] == NOW and w["kind"] == "widened"


class _Store:
    """HybridMemoryStore._time_tier over a stub search_fast that finds `found[tier]` hits."""

    def __init__(self, M, found):
        self.M, self.found, self.calls = M, found, []

    async def search_fast(self, q, limit=10, filters=None, timeout=1.0):
        self.calls.append((q, filters))
        n = self.found.get("scoped" if (filters["until"] - filters["since"]) <= timedelta(days=1)
                           else "widened", 0)
        return [{"record": {"id": str(i)}} for i in range(n)]


def _tier(found, query="what did we discuss yesterday", filters=None):
    try:
        M = importlib.import_module("Vera.vera.fabric.memory")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")
    st = _Store(M, found)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(M.HybridMemoryStore._time_tier(st, query, 10, filters)), st
    finally:
        loop.close()


def test_the_named_time_is_searched_first():
    (q, f, tier), st = _tier({"scoped": 3})
    assert q == "what did we discuss" and tier == "yesterday" and f["since"] < f["until"] and len(st.calls) == 1


def test_too_little_there_widens_then_falls_back_to_all_time():
    (q, f, tier), st = _tier({"scoped": 1, "widened": 3})
    assert tier == "widened" and len(st.calls) == 2
    (q, f, tier), st = _tier({})
    assert tier == "all time" and "since" not in f and q == "what did we discuss"


def test_a_callers_own_window_and_untimed_questions_are_left_alone():
    (q, f, tier), st = _tier({}, filters={"since": "2026-01-01T00:00:00+00:00"})
    assert tier is None and st.calls == []
    (q, f, tier), st = _tier({}, query="the current bash user")
    assert tier is None and q == "the current bash user" and st.calls == []
