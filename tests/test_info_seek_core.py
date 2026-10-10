"""info.seek's pure decisions (info-seeking plan SK-3.1). The owner, 2026-10-10: "latest updates on a topic" reached
for research.run (a report job of minutes) or got answered from memory with guesses. Pure: no network."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.web import info_seek_core as C                   # noqa: E402

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("q,need", [
    ("what's the latest on the EU AI Act?", "latest"), ("any news about n8n", "latest"),
    ("give me updates on UK house prices", "latest"), ("who won the race today", "latest"),
    ("what is the Bank of England base rate", "fact"), ("tell me about Copilot Studio agents", "fact"),
    ("explain how HNSW indexes work", "overview"), ("compare LangGraph and CrewAI", "overview"),
    ("write me a report on RAG rerankers", "deep"), ("do a deep dive into MCP security", "deep"),
])
def test_needs(q, need):
    assert C.classify_need(q)["need"] == need, q


@pytest.mark.parametrize("q,subject", [
    ("what's the latest on the EU AI Act?", "EU AI Act"), ("any news about n8n", "n8n"),
    ("give me updates on UK house prices", "UK house prices"), ("updates on MCP right now", "MCP"),
    ("latest AI/ML updates", "AI/ML"), ("write me a report on RAG rerankers", "RAG rerankers"),
    ("tell me about Copilot Studio agents", "Copilot Studio agents"), ("the news", "the news"),
])
def test_subject(q, subject):
    assert C.subject_of(q) == subject


def test_latest_queries_are_anchored_on_the_subject_and_dated():
    qs = C.latest_queries("EU AI Act", "what's the latest on the EU AI Act?", NOW)
    assert qs == ["EU AI Act latest news", "EU AI Act news October 2026", "what's the latest on the EU AI Act?"]
    assert C.latest_queries("MCP", "MCP", NOW) == ["MCP latest news", "MCP news October 2026"]


def test_merge_drops_the_same_page_and_the_same_headline():
    a = [{"url": "https://www.example.com/a/?utm_source=x", "title": "Rates held at 4 per cent by the Bank"}]
    b = [{"url": "https://example.com/a", "title": "other"},
         {"url": "https://mirror.example.org/a", "title": "Rates held at 4 per cent by the Bank"},
         {"url": "https://example.com/b", "title": "B"}]
    m = C.merge_hits([a, b])
    assert [x["url"] for x in m] == ["https://www.example.com/a/?utm_source=x", "https://example.com/b"]


@pytest.mark.parametrize("s,day", [
    ("2026-09-17", "2026-09-17"), ("2026-09-17T10:00:00Z", "2026-09-17"), ("Thu, 17 Sep 2026 10:00:00 +0000", "2026-09-17"),
    ("17 September 2026", "2026-09-17"), ("Sep 17, 2026", "2026-09-17"), ("posted 2026-09-17 by x", "2026-09-17"),
])
def test_dates(s, day):
    assert C.parse_date(s).strftime("%Y-%m-%d") == day


def test_no_date_is_none():
    assert C.parse_date("") is None and C.parse_date("yesterday") is None


def test_latest_order_newest_since_first_then_undated_then_old_and_future_is_undated():
    items = [{"t": "old", "published": "2026-08-01"}, {"t": "undated"}, {"t": "new1", "published": "2026-10-01"},
             {"t": "new2", "published": "2026-10-09"}, {"t": "future", "published": "2027-01-01"}]
    out = [x["t"] for x in C.order_latest(items, "2026-09-26", NOW)]
    assert out == ["new2", "new1", "undated", "future", "old"]


def test_per_site_keeps_depth_across_sources():
    items = [{"url": f"https://a.com/{i}"} for i in range(4)] + [{"url": "https://b.com/1"}]
    assert [x["url"] for x in C.per_site(items, 2)] == ["https://a.com/0", "https://a.com/1", "https://b.com/1"]


def test_excerpt_cuts_at_a_sentence():
    t = "First sentence here. Second sentence is longer than that. Third."
    assert C.excerpt(t, 40) == "First sentence here."
    assert C.excerpt("short", 40) == "short"


def test_pack_numbers_dates_and_says_what_is_missing():
    items = [{"title": "A", "url": "https://a.com/x", "published": "2026-10-08", "text": "body"},
             {"title": "B", "url": "https://b.com/y", "snippet": "snip", "kind": "intel"}]
    p = C.build_pack("latest", "q", "S", items, since="2026-09-26", checked=["web: 3"], now=NOW, elapsed_ms=5)
    assert [r["n"] for r in p["items"]] == [1, 2]
    assert p["items"][0]["published"] == "2026-10-08" and p["items"][0]["age_days"] == 2.8 and p["items"][0]["read"]
    assert p["items"][1]["kind"] == "intel" and p["items"][1]["text"] == "snip" and p["items"][1]["published"] == ""
    assert p["gaps"] == [] and "never fill a gap" in p["guide"]
    stale = C.build_pack("latest", "q", "S", [{"title": "old", "url": "https://o.com", "published": "2026-01-01"}],
                         since="2026-09-26", now=NOW)
    assert stale["gaps"] == ["no source found is dated on or after 2026-09-26"]
    assert C.build_pack("fact", "q", "S", [], now=NOW)["gaps"] == ["nothing was found"]
