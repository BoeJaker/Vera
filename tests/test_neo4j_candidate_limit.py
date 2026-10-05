"""Neo4j fulltext memory search gives Lucene a candidate limit (2026-10-05).

Without it every matching node was scored before the WHERE/LIMIT: a 36-word chat message
took 377-932 ms (35 s cold once) and timed out chat's 2.5 s fast tier; with {limit: 300}
35-81 ms and the identical top results.
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import memory  # noqa: E402

pytestmark = pytest.mark.critical


class _Result:
    async def data(self):
        return []


class _Session:
    def __init__(self, seen):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, params):
        self.seen.append((cypher, dict(params)))
        return _Result()


class _Driver:
    def __init__(self, seen):
        self.seen = seen

    def session(self):
        return _Session(self.seen)


def _backend(seen):
    b = memory.Neo4jBackend()
    b._driver = _Driver(seen)
    return b


def test_fulltext_query_carries_a_generous_candidate_limit():
    seen = []
    asyncio.run(_backend(seen).search("what did we decide about backups", limit=36))
    cypher, params = seen[0]
    assert "{limit: $cand}" in cypher
    assert params["cand"] == max(memory._NEO_FULLTEXT_MIN_CANDIDATES, 36 * memory._NEO_FULLTEXT_CANDIDATES_PER_HIT)
    seen.clear()
    asyncio.run(_backend(seen).search("x y", limit=5))
    assert seen[0][1]["cand"] == memory._NEO_FULLTEXT_MIN_CANDIDATES


def test_an_empty_query_lists_without_fulltext():
    seen = []
    asyncio.run(_backend(seen).search("", limit=5))
    assert "queryNodes" not in seen[0][0] and "cand" not in seen[0][1]
