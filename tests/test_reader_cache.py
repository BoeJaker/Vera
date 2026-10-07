"""reader_cache: a page read once as an article, kept a week, keyed as a reader names it (canvas-enrichment slice 2)."""
import asyncio
import base64
import importlib.util
import json
import pathlib

_P = pathlib.Path(__file__).resolve().parent.parent / "vera" / "web" / "reader_cache.py"
_spec = importlib.util.spec_from_file_location("reader_cache", _P)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


class FakeRedis:
    def __init__(self):
        self.kv, self.ex = {}, {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k], self.ex[k] = v, ex

    async def ttl(self, k):
        return self.ex.get(k) or -1


def run(c):
    return asyncio.run(c)


def test_the_same_page_reached_two_ways_is_one_entry():
    a = rc.canonical("HTTPS://Example.COM/news/a?utm_source=x&id=7#top")
    b = rc.canonical("https://example.com/news/a?id=7")
    assert a == b == "https://example.com/news/a?id=7"
    assert rc.key_of("https://example.com/news/a?id=7&fbclid=zz") == rc.key_of(b)
    assert rc.key_of("https://example.com/news/b") != rc.key_of(b)


def test_put_get_keeps_a_week_and_trims_on_the_way_out():
    r = FakeRedis()
    rec = {"title": "T", "markdown": "word " * 3000, "words": 3000, "via": "fetch"}
    assert run(rc.put(r, "https://ex.com/p", rec))
    assert r.ex[rc.key_of("https://ex.com/p")] == rc.TTL_S == 7 * 24 * 3600
    got = run(rc.get(r, "https://ex.com/p?utm_medium=y"))
    assert got["title"] == "T" and got["canonical"] == "https://ex.com/p"
    t = rc.trimmed(got, 100)
    assert len(t["markdown"]) == 100 and t["truncated"] and len(got["markdown"]) > 100


def test_a_big_page_keeps_its_body_in_the_object_store():
    r, store = FakeRedis(), {}

    async def put(key, content_base64, content_type=""):
        store[key] = content_base64
        return {"ok": True}

    async def get(key):
        return {"content_base64": store.get(key, "")}
    big = "x" * (rc.BIG + 10)
    run(rc.put(r, "https://ex.com/big", {"markdown": big}, put))
    raw = json.loads(r.kv[rc.key_of("https://ex.com/big")])
    assert raw["markdown"] == "" and raw["body_key"] == rc.object_key("https://ex.com/big")
    assert base64.b64decode(store[raw["body_key"]]).decode() == big
    assert run(rc.get(r, "https://ex.com/big", get))["markdown"] == big


def test_update_adds_fields_and_keeps_the_expiry():
    r = FakeRedis()
    run(rc.put(r, "https://ex.com/e", {"markdown": "m"}))
    r.ex[rc.key_of("https://ex.com/e")] = 1234
    assert run(rc.update(r, "https://ex.com/e", entities=[{"text": "Apple", "type": "ORG", "count": 2}]))
    assert run(rc.get(r, "https://ex.com/e"))["entities"][0]["text"] == "Apple"
    assert r.ex[rc.key_of("https://ex.com/e")] == 1234
    assert run(rc.get(None, "https://ex.com/e")) is None and run(rc.get(r, "https://ex.com/none")) is None
