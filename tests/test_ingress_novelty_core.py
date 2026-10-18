"""Novelty on receipts (vera/fabric/ingress_novelty_core.py + the writer; web-ingress WI-5a): known, near and new
content scored from what the map holds, the host's novel yield, and the writer's batch lookups through a fake pool."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "web-ingress-bench"))

from vera.fabric import ingress_core as ic  # noqa: E402
from vera.fabric import ingress_novelty_core as nc  # noqa: E402
from vera.fabric import ingress_receipts_core as rc  # noqa: E402
from vera.fabric import ingress_writer as iw  # noqa: E402
import fixture_site as site  # noqa: E402

LONG = ("the quick brown fox jumps over the lazy dog while the committee reviewed the quarterly figures " * 12)


def test_score_known_near_new():
    sh = ic.simhash64(LONG)
    assert nc.score("h1", ["h1", "h9"], [], sh) == {"novelty": 0.0, "kind": "known", "canonical": "h1"}
    near = sh ^ (1 << 3) ^ (1 << 40)
    r = nc.score("h2", ["h9"], [("h7", near), ("h8", sh ^ 0xFFFF)], sh)
    assert r["kind"] == "near" and r["canonical"] == "h7" and r["novelty"] == round(2 / 64, 3)
    assert nc.score("h3", [], [("h8", sh ^ 0xFFFF)], sh) == {"novelty": 1.0, "kind": "new", "canonical": ""}
    assert nc.score("h3", [], [("h3", sh)], sh)["kind"] == "new"            # itself is not a neighbour
    assert nc.score("", [], [], None)["kind"] == "new"


def test_band_params_and_host_yield():
    assert nc.band_params([[1, 2, 3, 4], [1, 9, 3, 7]]) == [[1], [2, 9], [3], [4, 7]]
    assert nc.host_yield(None, True) == 1.0 and nc.host_yield(None, False) == 0.0
    assert nc.host_yield(1.0, False) == 0.9 and nc.host_yield(0.5, True) == 0.55
    assert nc.summarise([{"kind": "new"}, {"kind": "near"}, {"kind": "new"}]) == {"known": 0, "near": 1, "new": 2}


class _Conn:
    def __init__(self, calls, known, cand):
        self.calls, self.known, self.cand = calls, known, cand

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql.split()[0], a))

    async def executemany(self, sql, rows):
        self.calls.append(("many", sql.split("(")[0].strip().replace("INSERT INTO ", "").replace("UPDATE ", "update:"), list(rows)))

    async def fetch(self, sql, *a):
        if sql == rc.LOOKUP_HASHES:
            return [(h,) for h in a[0] if h in self.known]
        if sql == rc.LOOKUP_BANDS:
            return list(self.cand)
        return []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _Pool:
    def __init__(self, known=(), cand=()):
        self.calls, self.known, self.cand = [], set(known), list(cand)

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self_):
                return _Conn(pool.calls, pool.known, pool.cand)

            async def __aexit__(self_, *a):
                return False
        return _A()


def _item(path, **over):
    r = site.respond("GET", path)
    body = r.body.decode()
    d = dict(url="http://fixture.test" + path, status=r.status, content_type=r.headers.get("content-type", ""),
             html=body, caller="web.fetch", purpose="page", requested="full")
    d.update(over)
    return rc.enqueue_item(**d)


def test_writer_scores_a_batch_against_the_map():
    a = rc.rows_for(_item("/a"))
    pool = _Pool(known={a["content_hash"]}, cand=[])
    w = iw.ReceiptWriter(lambda: pool, rows_in_thread=False, novelty_getter=lambda: True)

    async def go():
        w.enqueue(_item("/a"))                        # known: the map holds this hash
        w.enqueue(_item("/b"))                        # new
        w.enqueue(_item("/b", url="http://fixture.test/b2"))   # the same content twice in one batch: known the 2nd time
        await w.drain()
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())
    rows = {c[1]: c[2] for c in pool.calls if c[0] == "many"}
    nov = {r[0]: (r[1], r[2]) for r in rows[next(k for k in rows if k.startswith("update:web_urls"))]}
    assert nov["http://fixture.test/a"] == (0.0, "known")
    assert nov["http://fixture.test/b"] == (1.0, "new")
    assert nov["http://fixture.test/b2"] == (0.0, "known")
    yields = rows[next(k for k in rows if k.startswith("update:web_hosts"))]
    assert sorted(y[1] for y in yields) == [0.0, 0.0, 1.0]
    assert w.stats["novel"] == 1 and w.stats["known"] == 2 and w.stats["errors"] == 0


def test_writer_skips_scoring_when_the_switch_is_off_or_the_lookup_fails():
    pool = _Pool()
    w = iw.ReceiptWriter(lambda: pool, rows_in_thread=False, novelty_getter=lambda: False)

    async def go():
        w.enqueue(_item("/a"))
        await w.drain()
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())
    assert not any(c[1].startswith("update:") for c in pool.calls if c[0] == "many") and w.stats["flushed"] == 1

    class _BadConn(_Conn):
        async def fetch(self, sql, *a):
            raise RuntimeError("no such column")

    class _BadPool(_Pool):
        def acquire(self):
            pool = self

            class _A:
                async def __aenter__(self_):
                    return _BadConn(pool.calls, set(), [])

                async def __aexit__(self_, *a):
                    return False
            return _A()

    bad = _BadPool()
    w2 = iw.ReceiptWriter(lambda: bad, rows_in_thread=False, novelty_getter=lambda: True)

    async def go2():
        w2.enqueue(_item("/a"))
        await w2.drain()
        w2.stop()

    asyncio.new_event_loop().run_until_complete(go2())
    assert w2.stats["flushed"] == 1 and w2.stats["errors"] == 0           # the receipt still lands; novelty is skipped
    assert "novelty" in " ".join(rc.DDL) and "ALTER TABLE web_urls ADD COLUMN IF NOT EXISTS novelty REAL" in rc.DDL


def test_novelty_sql_types_every_parameter_and_runs_after_the_receipts():
    """18 Oct on prod: `$2` in UPDATE_HOST_YIELD was deduced numeric vs double precision, the UPDATE failed, and in the
    receipts' transaction it rolled back the whole batch. Every parameter is cast; novelty has its own transaction."""
    import re
    for sql in (rc.UPDATE_URL_NOVELTY, rc.UPDATE_HOST_YIELD):
        assert all(p.endswith(("::real", "::text")) for p in re.findall(r"\$\d::\w+", sql))
        bare = re.findall(r"\$\d(?!::)", sql)
        assert bare == [], (sql, bare)
    src = (ROOT / "vera" / "fabric" / "ingress_writer.py").read_text(encoding="utf-8")
    i_receipts = src.index("await conn.executemany(rc.UPSERT_HOST, hosts)")
    i_novelty = src.index("await conn.executemany(rc.UPDATE_URL_NOVELTY, novelty_rows)")
    assert i_novelty > i_receipts and "a novelty failure must never cost a receipt" in src


def test_a_failing_novelty_update_keeps_the_receipts():
    class _NovConn(_Conn):
        async def executemany(self, sql, rows):
            if sql.startswith("UPDATE"):
                raise RuntimeError("inconsistent types deduced for parameter $2")
            return await super().executemany(sql, rows)

    class _NovPool(_Pool):
        def acquire(self):
            pool = self

            class _A:
                async def __aenter__(self_):
                    return _NovConn(pool.calls, pool.known, pool.cand)

                async def __aexit__(self_, *a):
                    return False
            return _A()

    pool = _NovPool()
    w = iw.ReceiptWriter(lambda: pool, rows_in_thread=False, novelty_getter=lambda: True)

    async def go():
        w.enqueue(_item("/a"))
        await w.drain()
        w.stop()

    asyncio.new_event_loop().run_until_complete(go())
    manys = [c[1] for c in pool.calls if c[0] == "many"]
    assert "web_hosts" in manys and "web_urls" in manys and "web_fetches" in manys
    assert w.stats["flushed"] == 1 and w.stats["errors"] == 0 and w.stats["novelty_errors"] == 1



def test_frontier_adjust_and_the_collect_decision():
    assert nc.frontier_adjust(None) == 0.0
    assert nc.frontier_adjust(1.0) == 0.15 and nc.frontier_adjust(0.5) == 0.0
    assert nc.frontier_adjust(0.05) == -0.12 and nc.frontier_adjust(0.2) == -0.09
    from vera.fabric import discovery_ledger_core as DL
    d = DL.collect_decision("https://h.example/a", True, crawl_id="c1", score=0.82,
                            parts={"before_novelty": 0.67, "novelty": 0.15, "host_yield": 1.0}, rank=0, depth=2)
    assert d["question_id"] == "discovery.collect_worth" and d["answer"] == "collect"
    assert d["detail"]["parts"]["novelty"] == 0.15 and d["detail"]["rank"] == 0 and d["state_ref"]["kind"] == "crawl_link"


def test_the_crawl_uses_novelty_only_behind_its_switch():
    import ast
    src = (ROOT / "vera" / "fabric" / "discovery.py").read_text(encoding="utf-8")
    i = src.index('_nf = _ingress_switch("discovery.novelty_frontier") == "on"')
    block = src[i:i + 3500]
    assert "_ingress_host_yields" in block and "frontier_adjust" in block and "_record_collect" in block
    assert block.index("if _nf:") < block.index("frontier_adjust")
    from vera.minds import switchboard_core as SB
    assert SB.REGISTRY["discovery.novelty_frontier"].default == "off"
