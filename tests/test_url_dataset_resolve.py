"""The urlâ†’dataset scan must keep its answer and stop stalling the loop.

Guards the 2026-08-26 web.fetch finding: `_dataset_for_url` ran a
leading-wildcard LIKE over 909k rows synchronously on the event loop (perf.scan:
4 stalls on that line, worst 50.5s). The fix caches the result and moves it off
the loop WITHOUT changing which dataset comes back â€” so these tests pin the
answer as much as the speed.

Pure: builds its own sqlite db, imports no app module.
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.fabric.url_dataset_resolve import (        # noqa: E402
    ResolveCache, resolve_cached, scan_for_dataset,
)


class CountingConn:
    """sqlite connection that counts queries, so 'was it cached' is observable."""

    def __init__(self, conn):
        self._conn = conn
        self.queries = 0

    def execute(self, *a, **kw):
        self.queries += 1
        return self._conn.execute(*a, **kw)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE fabric_records (id TEXT, dataset_id TEXT, data TEXT)")
    rows = [
        ("1", "topic.alpha", json.dumps({"url": "https://a.example/page", "t": "A"})),
        ("2", "topic.beta", json.dumps({"url": "https://b.example/other", "t": "B"})),
        ("3", "topic.frag", json.dumps({"url": "https://c.example/x#section", "t": "C"})),
        # top-level url differs; the QUERY url appears only in a nested field, so
        # the LIKE matches this row and only the json check can reject it.
        ("4", "topic.nested", json.dumps({"url": "https://outer.example/p",
                                          "source": {"url": "https://d.example/inner"}})),
    ]
    c.executemany("INSERT INTO fabric_records VALUES (?,?,?)", rows)
    c.commit()
    return CountingConn(c)


def test_finds_the_dataset_for_a_known_url(conn):
    assert scan_for_dataset(conn, "https://a.example/page") == "topic.alpha"


def test_returns_blank_for_an_unknown_url(conn):
    assert scan_for_dataset(conn, "https://nope.example/missing") == ""


def test_blank_url_short_circuits(conn):
    assert scan_for_dataset(conn, "") == ""
    assert conn.queries == 0, "an empty url must not hit the database at all"


def test_a_nested_url_match_is_rejected_by_the_json_check(conn):
    """The LIKE matches the blob; only the parse-and-compare stops a wrong answer.

    Record 4's top-level url is outer.example/p, but d.example/inner appears in a
    nested field. Searching for the nested url makes the SQL match, so if the
    confirmation step were dropped this would wrongly return "topic.nested".
    """
    assert scan_for_dataset(conn, "https://d.example/inner") == ""


def test_the_top_level_url_of_that_same_record_does_resolve(conn):
    """Control for the test above - the row IS findable by its real url."""
    assert scan_for_dataset(conn, "https://outer.example/p") == "topic.nested"


def test_a_stored_url_with_a_fragment_is_unreachable_either_way(conn):
    """Pins a latent bug in the original query, preserved deliberately.

    A record stored with "â€¦/x#section" can be found by NEITHER form:
      * query "â€¦/x"          -> the LIKE pattern carries the CLOSING quote
                                (`"url": "â€¦/x"`), so the row never matches;
      * query "â€¦/x#section"  -> the row matches, but the confirmation compares
                                `stored.split("#")[0]` against the FULL query
                                url, so it fails too.
    So the `split("#")` never helps and actively blocks the exact-match case.
    Recorded rather than repaired: changing it moves pages between datasets,
    which is a semantics decision for a human (see the plan's Phase F), not
    something to slip into a stall fix.
    """
    assert scan_for_dataset(conn, "https://c.example/x") == ""
    assert scan_for_dataset(conn, "https://c.example/x#section") == ""


def test_cache_avoids_a_second_scan(conn):
    cache = ResolveCache()
    first = resolve_cached(conn, "https://a.example/page", cache)
    after_first = conn.queries
    second = resolve_cached(conn, "https://a.example/page", cache)
    assert first == second == "topic.alpha"
    assert conn.queries == after_first, "second lookup must be served from cache"


def test_misses_are_cached_too(conn):
    """A miss costs a FULL scan, so not caching it leaves the worst path uncached."""
    cache = ResolveCache()
    assert resolve_cached(conn, "https://nope.example/x", cache) == ""
    after_first = conn.queries
    assert resolve_cached(conn, "https://nope.example/x", cache) == ""
    assert conn.queries == after_first, "a negative result must be cached"


def test_cache_entry_expires(conn):
    cache = ResolveCache(ttl_s=10.0)
    assert resolve_cached(conn, "https://a.example/page", cache, now=1000.0) == "topic.alpha"
    after_first = conn.queries
    # inside the ttl -> cached
    resolve_cached(conn, "https://a.example/page", cache, now=1005.0)
    assert conn.queries == after_first
    # past the ttl -> rescanned
    resolve_cached(conn, "https://a.example/page", cache, now=1011.0)
    assert conn.queries > after_first, "an expired entry must be re-scanned"


def test_cache_is_bounded(conn):
    cache = ResolveCache(max_entries=40)
    for i in range(200):
        cache.put("https://x.example/%d" % i, "ds%d" % i)
    assert len(cache) <= 40, "cache must not grow without bound"


def test_no_cache_still_returns_the_same_answer(conn):
    assert resolve_cached(conn, "https://a.example/page", None) == "topic.alpha"


def test_cached_answer_matches_the_uncached_scan(conn):
    """The whole point: caching must not change WHICH dataset comes back."""
    cache = ResolveCache()
    for url in ("https://a.example/page", "https://b.example/other",
                "https://c.example/x", "https://nope.example/none"):
        assert resolve_cached(conn, url, cache) == scan_for_dataset(conn, url)


# â”€â”€ web.fetch files pages under its own dataset â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Leaving the dataset unset made discover_ingest_page resolve it with the full
# fabric_records scan. Measured on 20 real pages that scan added nothing 12/20
# times, and the other 8 filed one-off fetches into curated corpora
# (agent_rag.*, research.citations) - mutating agents' knowledge bases as a side
# effect. These pin the naming so the scan branch stays unreachable from here.

from vera.fabric.url_dataset_resolve import auto_dataset_for_url  # noqa: E402


@pytest.mark.parametrize("url,expected", [
    ("https://en.wikipedia.org/wiki/Event_loop", "web.en_wikipedia_org"),
    ("http://rss.arxiv.org/rss/cs.CL", "web.rss_arxiv_org"),
    ("https://learn.microsoft.com/en-us/azure/", "web.learn_microsoft_com"),
    ("example.com/x", "web.example_com"),            # scheme-less still works
    ("https://UPPER.Example.COM/", "web.upper_example_com"),   # case-folded
    ("https://localhost:8999/health", "web.localhost_8999"),   # port kept, sanitised
])
def test_dataset_name_is_stable_per_domain(url, expected):
    assert auto_dataset_for_url(url) == expected


def test_dataset_name_is_never_empty_for_a_real_url():
    """The whole point: a non-empty dataset means the scan branch is never taken.

    discover_ingest_page only falls back to the fabric_records scan when
    dataset_id is blank, so as long as this returns something for every url
    web.fetch can reach, that path cannot be re-entered by accident.
    """
    for url in ("https://a.example/p", "http://b.example", "c.example/x",
                "https://d.example:8443/x?y=1#z"):
        assert auto_dataset_for_url(url).strip() not in ("", "web.")


def test_dataset_name_is_bounded():
    """Host slug is capped, so a hostile hostname cannot make an unbounded id."""
    url = "https://" + ("a" * 500) + ".example.com/x"
    ds = auto_dataset_for_url(url)
    assert len(ds) <= len("web.") + 30


def test_query_string_and_fragment_do_not_split_the_dataset():
    """Re-fetching must upsert in place, not scatter copies across datasets.

    The dataset comes from the HOST only, so tracking params and fragments -
    which change constantly on real links - must not mint a new dataset each
    time. (Record ids stay per-url, so distinct pages remain distinct.)
    """
    base = auto_dataset_for_url("https://en.wikipedia.org/wiki/Event_loop")
    for variant in ("https://en.wikipedia.org/wiki/Event_loop?utm_source=x",
                    "https://en.wikipedia.org/wiki/Event_loop#History",
                    "https://en.wikipedia.org/wiki/Other_Page"):
        assert auto_dataset_for_url(variant) == base, variant


def test_an_explicit_default_port_splits_the_dataset(): 
    """Pins a pre-existing wart, deliberately NOT repaired here.

    The slug is built from `netloc`, which keeps the port, so
    "https://host:443/x" files under `web.host_443` while "https://host/x"
    files under `web.host` - the same site in two datasets. Normalising it
    would move pages for every caller of discovery._auto_ds, including the
    crawl paths, so it is a separate decision rather than a rider on the
    web.fetch change. Recorded so the behaviour is known, not assumed.
    """
    assert auto_dataset_for_url("https://en.wikipedia.org:443/x") == "web.en_wikipedia_org_443"
    assert auto_dataset_for_url("https://en.wikipedia.org/x") == "web.en_wikipedia_org"


def test_different_hosts_get_different_datasets():
    """Control - the helper must not pass the tests above by returning a constant."""
    assert (auto_dataset_for_url("https://a.example/p")
            != auto_dataset_for_url("https://b.example/p"))
