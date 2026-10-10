"""Stories: the same news carried by several sources becomes one item with its
sources, sites, first seen and momentum; joins record when each source came in."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_story_core as story  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_join_records_who_and_when():
    s = {"id": "a", "url": "https://www.bbc.co.uk/x"}
    s.update(story.add_join(s, "https://ft.com/y", _iso(NOW)))
    s.update(story.add_join(s, "https://ft.com/y", _iso(NOW)))            # the same url twice is one join
    assert s["also"] == ["https://ft.com/y"] and s["joins"] == [{"url": "https://ft.com/y", "site": "ft.com", "at": _iso(NOW)}]
    for i in range(30):
        s.update(story.add_join(s, f"https://n{i}.test/z", _iso(NOW)))
    assert len(s["joins"]) == story.MAX_JOINS and len(s["also"]) == 12


def test_the_story_view_counts_sources_sites_and_momentum():
    sig = {"id": "a", "topic_id": "t", "title": "Acme pilot", "url": "https://www.bbc.co.uk/x", "seen": _iso(NOW - timedelta(days=3)),
           "also": ["https://ft.com/y", "https://ft.com/z", "https://reuters.com/w"],
           "joins": [{"url": "https://ft.com/y", "at": _iso(NOW - timedelta(days=2))},
                     {"url": "https://ft.com/z", "at": _iso(NOW - timedelta(hours=5))},
                     {"url": "https://reuters.com/w", "at": _iso(NOW - timedelta(hours=1))}]}
    v = story.story(sig, NOW)
    assert v["sources"] == 4 and v["sites"] == ["bbc.co.uk", "ft.com", "reuters.com"]
    assert v["momentum"] == 2 and v["last_join"] == _iso(NOW - timedelta(hours=1))
    old = story.story({"id": "b", "url": "https://a.test/1", "also": ["https://b.test/2"]}, NOW)
    assert old["sources"] == 2 and old["momentum"] == 0 and old["last_join"] == ""   # joined before join times existed


def test_stories_rank_by_momentum_then_spread():
    a = {"id": "a", "url": "https://a.test/1", "also": ["https://b.test/1", "https://c.test/1"], "seen": "2026-10-01T00:00:00Z"}
    b = {"id": "b", "url": "https://a.test/2", "also": ["https://b.test/2"], "seen": "2026-10-02T00:00:00Z",
         "joins": [{"url": "https://b.test/2", "at": _iso(NOW - timedelta(hours=2))}]}
    c = {"id": "c", "url": "https://a.test/3", "seen": "2026-10-03T00:00:00Z"}               # one source: not a story
    assert [r["id"] for r in story.stories([a, b, c], NOW)] == ["b", "a"]
    assert [r["id"] for r in story.stories([a, b, c], NOW, min_sources=1, limit=2)] == ["b", "a"]


def test_triage_records_joins_and_the_capability_reads_them(tmp_path):
    from test_intel_capabilities import _load
    mod, orch = _load(tmp_path)
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="AI property valuation", brief="AI valuation in UK property")["topic"]
    assert R("intel.sweep", topic_id=t["id"])["swept"][0]["joined"] == 1     # the second pilot story joins the first
    st = R("intel.stories.get", topic_id=t["id"])["stories"]
    assert len(st) == 1 and st[0]["sources"] == 2 and st[0]["momentum"] == 1
    assert st[0]["sites"] == ["news.example", "other.example"] and st[0]["last_join"]
    every = R("intel.stories.get")["stories"]
    assert every and every[0]["topic"] == "AI property valuation"             # across topics, with the topic's name
    assert R("intel.stories.get", topic_id=t["id"], min_sources=3)["stories"] == []
