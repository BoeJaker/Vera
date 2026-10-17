"""The web map's receipts (vera/fabric/ingress_receipts_core.py + ingress_writer.py; web-ingress WI-3): a fetch
result becomes rows in the real shapes, the writer batches them into a (fake) Postgres pool in one transaction,
enqueueing costs the request path microseconds and never blocks, and web_client's hook fires for every fetch."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

from vera.fabric import ingress_receipts_core as rc  # noqa: E402
from vera.fabric import ingress_writer as iw  # noqa: E402
import fixture_site as site  # noqa: E402


def _page(path, **over):
    r = site.respond("GET", path)
    body = r.body.decode("utf-8", "replace")
    d = dict(url="http://fixture.test" + path, final_url="http://fixture.test" + path, status=r.status,
             content_type=r.headers.get("content-type", ""), html=body if "html" in r.headers.get("content-type", "") else "",
             text="" if "html" in r.headers.get("content-type", "") else body, elapsed_ms=12, caller="web.fetch",
             purpose="page", etag=r.headers.get("etag", ""), last_modified=r.headers.get("last-modified", ""),
             title="t", requested="full")
    d.update(over)
    return rc.enqueue_item(**d)


# ── enqueue_item and rows_for ─────────────────────────────────────────────────

def test_internal_hosts_and_installs_leave_no_receipt():
    assert rc.enqueue_item("https://llm.int:8999/ui", status=200, requested="full") is None
    assert rc.enqueue_item("http://192.168.0.96:8123/api", status=200, requested="full") is None
    assert rc.enqueue_item("https://get.docker.com/", status=200, purpose="install", requested="full") is None
    it = rc.enqueue_item("https://api.ebay.com/sell/x", status=200, html="<html>secret</html>", purpose="account",
                         requested="full")
    assert it["policy"] == "meta" and it["html"] == "" and it["text"] == ""          # private: never a body
    it = rc.enqueue_item("https://en.wikipedia.org/wiki/X", status=200, html="<html>x</html>", requested="meta")
    assert it["policy"] == "meta" and it["html"] == ""                               # the switch at 'meta'


def test_a_full_page_becomes_host_url_fetch_content_and_links():
    item = _page("/")
    assert item["policy"] == "full" and item["bytes"] > 100
    rows = rc.rows_for(item)
    assert rows["key"] == "http://fixture.test/" and rows["host"] == ("fixture.test", 0, item["bytes"])
    url = rows["url"]
    assert url[0] == "http://fixture.test/" and url[2] == 200 and url[4] == "html" and url[5] == "page"
    assert url[9] == rows["content_hash"] and isinstance(url[10], int) and url[11] == "web.fetch"
    assert rows["fetch"][0] == rows["key"] and rows["fetch"][3] == 12 and rows["fetch"][6] == "web.fetch"
    c = rows["content"]
    assert c[0] == rows["content_hash"] and c[1] == rows["key"] and c[3] == "html"
    assert all(0 <= b < 65536 for b in c[5:9])
    dst = {ln[1] for ln in rows["links"]}
    assert "http://fixture.test/a" in dst and "http://fixture.test/files/report.pdf" in dst
    assert "http://fixture.test/a?utm_source=newsletter" not in dst            # canonical keys only
    assert all(ln[0] == rows["key"] for ln in rows["links"])


def test_a_pdf_a_304_and_an_error_make_the_right_rows():
    pdf = _page("/files/report.pdf")
    rows = rc.rows_for(pdf)
    assert rows["url"][4] == "pdf" and rows["url"][5] == "file" and rows["links"] == []
    assert rows["content"] is not None                                           # text is hashed for files too
    nm = rc.enqueue_item("https://h.example/etag", status=304, content_type="", requested="full", caller="web.fetch")
    rows = rc.rows_for(nm)
    assert rows["content"] is None and rows["url"][2] == 304 and rows["host"][1] == 0
    err = rc.enqueue_item("https://h.example/x", status=0, error="timeout", requested="full")
    rows = rc.rows_for(err)
    assert rows["host"][1] == 1 and rows["fetch"][9] == "timeout" and rows["url"][4] is None


def test_redirects_record_the_final_key_and_simhash_fits_bigint():
    item = rc.enqueue_item("https://www.Example.com/old?utm_source=x", final_url="https://example.com/d",
                           status=200, content_type="text/html", html="<html><body>" + "words " * 50 + "</body></html>",
                           requested="full")
    rows = rc.rows_for(item)
    assert rows["key"] == "https://example.com/old" and rows["url"][14] == "https://example.com/d"
    sh = rows["url"][10]
    assert -(1 << 63) <= sh < (1 << 63) and rc.unsigned64(sh) == rc.unsigned64(rows["content"][4])


def test_query_rows_and_budget():
    q = rc.query_row("lab space cambridge", "searxng", "web.search", "search",
                     ["https://a.example/x?utm_source=1", "https://a.example/x", "https://b.example/"])
    assert q[0] == "lab space cambridge" and q[4] == 2 and q[5] == ["https://a.example/x", "https://b.example/"]
    b = rc.budget_state({"web_urls": 10 << 30, "web_links": 7 << 30}, 20)
    assert b["state"] == "warn" and b["share"] == 0.85
    assert rc.budget_state({"web_urls": 1 << 30}, 20)["state"] == "ok"
    assert rc.budget_state({"web_urls": 21 << 30}, 20)["state"] == "over"
    assert len(rc.DDL) >= 6 and all(t in " ".join(rc.DDL) for t in rc.MAP_TABLES)


# ── the writer ────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, calls):
        self.calls = calls

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql.split()[0] + " " + sql.split()[1] if " " in sql else sql, a))

    async def executemany(self, sql, rows):
        self.calls.append(("many", sql.split("(")[0].strip().replace("INSERT INTO ", ""), list(rows)))

    def transaction(self):
        return self

    async def __aenter__(self):
        self.calls.append(("tx", "begin", ()))
        return self

    async def __aexit__(self, *a):
        self.calls.append(("tx", "end", ()))


class _Pool:
    def __init__(self):
        self.calls = []

    def acquire(self):
        return _Acq(self)


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool.calls)

    async def __aexit__(self, *a):
        return False


def test_writer_batches_into_one_transaction_and_runs_ddl_once():
    pool = _Pool()
    w = iw.ReceiptWriter(lambda: pool, rows_in_thread=False)

    async def go():
        assert w.enqueue(_page("/")) and w.enqueue(_page("/b")) and w.enqueue(_page("/files/report.pdf"))
        assert w.enqueue({"query": "rents", "engine": "searxng", "caller": "web.search", "result_urls": ["http://fixture.test/b"]})
        await w.drain()
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())
    kinds = [c[0] for c in pool.calls]
    assert kinds.count("execute") >= len(rc.DDL)                                  # the DDL ran
    manys = [c[1] for c in pool.calls if c[0] == "many"]
    assert manys[:3] == ["web_hosts", "web_urls", "web_fetches"] and "web_content" in manys and "web_links" in manys
    assert "web_queries" in manys
    rows = {c[1]: c[2] for c in pool.calls if c[0] == "many"}
    assert len(rows["web_urls"]) == 3 and len(rows["web_fetches"]) == 3 and len(rows["web_queries"]) == 1
    assert w.stats["flushed"] == 3 and w.stats["batches"] == 1 and w.stats["queries"] == 1 and w.stats["errors"] == 0
    tx = [c for c in pool.calls if c[0] == "tx"]
    assert tx[0][1] == "begin" and tx[-1][1] == "end" and len(tx) == 2                 # one transaction


def test_enqueue_is_cheap_never_blocks_and_counts_drops():
    w = iw.ReceiptWriter(lambda: None, queue_max=100, rows_in_thread=False)

    async def go():
        item = _page("/a")
        t0 = time.perf_counter()
        for _ in range(1000):
            w.enqueue(item)
        per_call_us = (time.perf_counter() - t0) / 1000 * 1e6
        assert per_call_us < 1000, per_call_us                                    # ≤ 1 ms each (PLAN §6)
        assert w.stats["enqueued"] == 100 and w.stats["dropped_full"] == 900         # full = drop, never wait
        await w.drain()                                                            # no store: dropped, counted
        assert w.stats["dropped_no_store"] == 100 and w.stats["errors"] == 0
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())
    assert w.enqueue(_page("/a")) is False                                          # no loop: dropped, not raised


def test_a_store_error_is_counted_and_the_writer_goes_on():
    class _Bad(_Pool):
        def acquire(self):
            raise RuntimeError("pg down")

    w = iw.ReceiptWriter(lambda: _Bad(), rows_in_thread=False)

    async def go():
        w.enqueue(_page("/a"))
        r = await w.flush([_page("/a")])
        assert r == {"error": 1} and w.stats["errors"] == 1 and "pg down" in w.stats["last_error"]
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())


# ── web_client's hook ─────────────────────────────────────────────────────────

def test_fetch_page_calls_the_hooks_with_the_result():
    httpx = pytest.importorskip("httpx")
    from vera.web import web_client as wc
    seen = []
    wc.FETCH_HOOKS.append(lambda out: seen.append(dict(out)))
    try:
        async def go():
            async with httpx.AsyncClient(transport=site.mock_transport(), follow_redirects=True) as c:
                return await wc.fetch_page("http://fixture.test/old", client=c, use_api=False, use_reader=False,
                                           throttle=False, caller="test", purpose="page")
        out = asyncio.new_event_loop().run_until_complete(go())
    finally:
        wc.FETCH_HOOKS.pop()
    assert out["status"] == 200 and out["final_url"] == "http://fixture.test/d"
    assert len(seen) == 1 and seen[0]["caller"] == "test" and seen[0]["content_type"].startswith("text/html")
    assert seen[0]["etag"] and seen[0]["last_modified"]
    bad = []
    wc.FETCH_HOOKS.append(lambda out: 1 / 0)
    try:
        async def go2():
            async with httpx.AsyncClient(transport=site.mock_transport(), follow_redirects=True) as c:
                return await wc.fetch_page("http://fixture.test/a", client=c, use_api=False, use_reader=False, throttle=False)
        out = asyncio.new_event_loop().run_until_complete(go2())
    finally:
        wc.FETCH_HOOKS.pop()
    assert out["status"] == 200 and not out.get("error")                               # a raising hook never hurts
