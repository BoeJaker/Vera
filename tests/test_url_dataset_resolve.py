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
