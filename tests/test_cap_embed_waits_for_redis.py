"""The startup capability-embedding pass never re-embeds every cap because Redis was not
connected yet (3 Oct 2026: 2,695 caps re-embedded for ~14 min after every restart, starving
the GPU runner, while a call 3 s later found them all cached)."""

import asyncio
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from Vera.vera.dag import dag_store as D
except Exception:                                    # pragma: no cover
    D = None

needs_app = pytest.mark.skipif(D is None, reason="app module not importable here")


class _Redis:
    def __init__(self, data=None, fail=False):
        self.data, self.fail = dict(data or {}), fail

    async def hgetall(self, key):
        if self.fail:
            raise ConnectionError("not ready")
        return self.data

    async def hset(self, *a, **k):
        return 1


def _index(monkeypatch, redis_seq, cached_text=None):
    idx = D.CapabilityIndex()
    idx._index = {"a.cap": {"embed_text": "a.cap: does a", "embedding": []},
                  "b.cap": {"embed_text": "b.cap: does b", "embedding": []}}
    calls = []

    async def fake_embed(name, entry, h=""):
        calls.append(name)
        return True

    seq = list(redis_seq)
    monkeypatch.setattr(D, "_redis", lambda: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(D, "CAP_EMBED_REDIS_WAIT_S", 2)
    monkeypatch.setattr(idx, "_embed_one", fake_embed)

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)
    return idx, calls


@needs_app
def test_no_redis_means_no_blind_re_embed(monkeypatch):
    idx, calls = _index(monkeypatch, [None])
    asyncio.run(idx.start_embedding(embed_missing=True))
    assert calls == []


@needs_app
def test_a_failed_cache_read_means_no_re_embed(monkeypatch):
    idx, calls = _index(monkeypatch, [_Redis(fail=True)])
    asyncio.run(idx.start_embedding(embed_missing=True))
    assert calls == []


@needs_app
def test_waits_for_redis_then_uses_the_cache(monkeypatch):
    cache = {}
    for name, e in (("a.cap", "a.cap: does a"), ("b.cap", "b.cap: does b")):
        cache[name] = json.dumps({"vec": [0.1] * 16, "hash": D.CapabilityIndex._embed_text_hash(e)})
    idx, calls = _index(monkeypatch, [None, _Redis(cache)])
    asyncio.run(idx.start_embedding(embed_missing=True))
    assert calls == [] and idx._index["a.cap"]["embedding"] == [0.1] * 16


@needs_app
def test_a_truly_empty_cache_still_embeds_and_one_pass_at_a_time(monkeypatch):
    idx, calls = _index(monkeypatch, [_Redis({})])
    asyncio.run(idx.start_embedding(embed_missing=True))
    assert sorted(calls) == ["a.cap", "b.cap"]
    idx2, calls2 = _index(monkeypatch, [_Redis({})])
    idx2._embed_running = True
    asyncio.run(idx2.start_embedding(embed_missing=True))
    assert calls2 == []
