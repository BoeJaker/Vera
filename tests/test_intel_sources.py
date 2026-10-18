"""A topic reads sources besides web search through the data fabric's sources
system: intel registers (or reuses) a fabric source, the fabric pulls it, and the
sweep triages what is new there like any web result."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.intel import intel_sources_core as srcs  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the pure part ────────────────────────────────────────────────────────────

def test_each_kind_plans_a_fabric_source():
    a = srcs.plan_source("arxiv", "cs.AI")
    assert a["url"] == "https://rss.arxiv.org/rss/cs.AI" and a["source_type"] == "rss" and a["interval"] == 12 * 3600
    assert a["id"].startswith("intel-src-") and a["dataset_id"] == "intel_src_" + a["id"][10:]
    assert srcs.plan_source("github_releases", "n8n-io/n8n")["url"].endswith("/n8n-io/n8n/releases.atom")
    u = srcs.plan_source("usgs", "4.5")
    assert u["source_type"] == "api" and u["url"].endswith("/4.5_day.geojson") and u["config"]["geo_field"]
    assert srcs.plan_source("youtube", "@3blue1brown") == {**srcs.plan_source("youtube", "@3blue1brown")} \
        and "discover" in srcs.plan_source("youtube", "@3blue1brown")
    assert "discover" in srcs.plan_source("feed", "https://simonwillison.net/")
    assert srcs.plan_source("api", "https://x.test/data.json")["source_type"] == "api"
    for bad in (("usgs", "7"), ("arxiv", "drop table"), ("api", "ftp://x"), ("nope", "x"), ("feed", "")):
        assert "error" in srcs.plan_source(*bad), bad
    # the same thing read twice is one fabric source, whoever adds it
    assert srcs.source_id("arxiv", "cs.AI") == srcs.source_id("arxiv", " CS.ai ")
    assert srcs.plan_source("usgs", "4.5", interval_h=0.01)["interval"] == 900          # never hammered


def test_records_from_any_source_become_hits():
    rss = {"data": {"title": "New agent framework", "link": "https://blog.test/a", "published": "Wed, 08 Oct 2026 09:00:00 GMT",
                    "summary": "<p>It ships</p>"}}
    h = srcs.record_hit(rss, kind="feed")
    assert h["url"] == "https://blog.test/a" and h["snippet"] == "It ships" and h["published"] == "2026-10-08T09:00:00Z"
    quake = {"data": {"title": "M 6.1 - off Japan", "link": "https://earthquake.usgs.gov/x", "published": _iso(NOW),
                      "geo": {"lat": 36.0, "lon": 141.0}}}
    assert srcs.record_hit(quake, kind="usgs")["places"][0]["lat"] == 36.0
    uk = srcs.parse_bbox("-11,49,2,61")
    assert srcs.record_hit(quake, kind="usgs", bbox=uk) is None                           # outside the box
    assert srcs.record_hit({"data": '{"title": "x", "link": "https://a.test"}'}, kind="feed")["url"] == "https://a.test"
    assert srcs.parse_bbox("1,2,3") is None and srcs.parse_bbox("10,0,5,1") is None and srcs.parse_bbox("a,b,c,d") is None


def test_freshness_and_staleness():
    assert srcs.fresh(_iso(NOW - timedelta(days=2)), NOW) and not srcs.fresh(_iso(NOW - timedelta(days=30)), NOW)
    assert srcs.fresh("", NOW)                                                          # undated: kept
    assert srcs.stale("", 3600, NOW) and srcs.stale(_iso(NOW - timedelta(hours=2)), 3600, NOW)
    assert not srcs.stale(_iso(NOW - timedelta(minutes=30)), 3600, NOW)


# ── end to end against the stand-in orchestration ────────────────────────────

def _with_fabric(tmp_path):
    from test_intel_capabilities import _load
    mod, orch = _load(tmp_path)
    reg = orch.CAPABILITY_REGISTRY
    fab = {}                                     # the fabric's sources
    records = {}                                 # dataset -> records
    now = datetime.now(timezone.utc)
    feed_items = [
        {"title": "BoE rate decision lifts AI valuation demand", "link": "https://feed.test/boe-ai", "published": _iso(now),
         "summary": "Lenders turn to automated valuations after the BoE decision"},
        {"title": "Acme pilot reaches production", "link": "https://feed.test/acme-pilot", "published": _iso(now),
         "summary": "The Acme valuation pilot is live"},
        {"title": "An old pilot story", "link": "https://feed.test/old-pilot", "published": _iso(now - timedelta(days=60)),
         "summary": "pilot from long ago"},
        {"title": "Weather notes", "link": "https://feed.test/weather", "published": _iso(now), "summary": "rain"},
    ]

    async def sources_add(url="", source_type="rss", label="", dataset_id="", interval=300, tags="", config="",
                          id="", trace_id=None):
        orch.CALLS.append(("fabric.sources.add", {"id": id, "url": url, "source_type": source_type, "config": config}))
        fab[id] = {"id": id, "url": url, "dataset_id": dataset_id, "interval": interval, "last_pulled": "",
                   "pull_count": 0}
        return {"id": id, "label": label, "dataset_id": dataset_id, "url": url}

    async def sources_pull(source_id="", trace_id=None):
        orch.CALLS.append(("fabric.sources.pull", {"source_id": source_id}))
        s = fab[source_id]
        s.update(last_pulled=_iso(datetime.now(timezone.utc)), pull_count=s["pull_count"] + 1)
        stamp = _iso(datetime.now(timezone.utc))          # the fabric's created_at for this pull's records
        records[s["dataset_id"]] = [{"data": d, "text": d["title"], "created_at": stamp} for d in feed_items]
        return {"ingested": len(feed_items), "dataset_id": s["dataset_id"]}

    async def sources_list(trace_id=None):
        return {"sources": list(fab.values())}

    async def sources_delete(source_id="", trace_id=None):
        orch.CALLS.append(("fabric.sources.delete", {"source_id": source_id}))
        fab.pop(source_id, None)
        return {"deleted": True}

    async def discover(url="", kind="", trace_id=None):
        return {"feeds": [{"url": "https://feed.test/rss.xml", "title": "Feed Test"}]} if "feed.test" in url else {"feeds": [], "note": "none"}

    old_browse = reg["fabric.browse"]["func"]

    async def browse(dataset_id, limit=50, offset=0, trace_id=None):
        if dataset_id in records:
            return {"records": records[dataset_id][:limit], "has_more": False}
        return await old_browse(dataset_id, limit=limit, offset=offset)

    for n, f in (("fabric.sources.add", sources_add), ("fabric.sources.pull", sources_pull), ("fabric.sources", sources_list),
                 ("fabric.sources.delete", sources_delete), ("fabric.sources.discover_feed", discover),
                 ("fabric.browse", browse)):
        reg[n] = {"func": f, "raw": f}
    return mod, orch, fab


def test_a_topic_reads_a_feed_through_the_fabric(tmp_path):
    import asyncio
    mod, orch, fab = _with_fabric(tmp_path)
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="AI property valuation", brief="AI valuation pilots")["topic"]
    added = R("intel.source.add", topic_id=t["id"], kind="feed", value="https://feed.test/")
    assert added["ok"] and added["pulled"] == 4 and added["source"]["url"] == "https://feed.test/rss.xml"
    sid = added["source"]["id"]
    assert fab[sid]["dataset_id"] == added["source"]["dataset_id"]
    assert "Feed Test" in added["source"]["label"]

    sw = R("intel.sweep", topic_id=t["id"])["swept"][0]
    assert sw["from_sources"] == 3                       # three fresh items; the 60-day-old one is skipped
    sigs = {s["url"]: s for s in mod.store().signals(t["id"])}
    boe = sigs["https://feed.test/boe-ai"]
    assert boe["source"] == f"fabric:{sid}" and "https://feed.test/old-pilot" not in sigs
    # triaged like web results: the same story from the feed joins the web signal it repeats ...
    web_pilot = next(s for s in sigs.values() if s["url"] == "https://news.example/acme-pilot")
    assert "https://feed.test/acme-pilot" in (web_pilot.get("also") or [])
    # ... and the reranker scores the on-topic item above the weather note
    w = sigs.get("https://feed.test/weather")
    assert w is None or (w.get("score") or 0) < boe["score"]
    # a second sweep finds nothing new and does not pull a fresh source again
    pulls = sum(1 for n, _ in orch.CALLS if n == "fabric.sources.pull")
    assert R("intel.sweep", topic_id=t["id"])["swept"][0]["from_sources"] == 0
    assert sum(1 for n, _ in orch.CALLS if n == "fabric.sources.pull") == pulls
    assert mod.store().get_topic(t["id"])["sources"][0]["cursor"]           # the sweep remembered how far it read

    lst = R("intel.source.list", topic_id=t["id"])
    assert lst["sources"][0]["pulls"] >= 1 and not lst["sources"][0]["missing"]
    assert {k["kind"] for k in lst["kinds"]} == set(srcs.KINDS)


def test_a_shared_source_outlives_one_topic(tmp_path):
    import asyncio
    mod, orch, fab = _with_fabric(tmp_path)
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    a = R("intel.topic.save", name="Agents")["topic"]
    b = R("intel.topic.save", name="LLM research")["topic"]
    s1 = R("intel.source.add", topic_id=a["id"], kind="arxiv", value="cs.AI")["source"]
    s2 = R("intel.source.add", topic_id=b["id"], kind="arxiv", value="cs.AI")["source"]
    assert s1["id"] == s2["id"] and len(fab) == 1                       # one fabric source, two readers
    r = R("intel.source.remove", topic_id=a["id"], source_id=s1["id"])
    assert r["ok"] and not r["removed_from_fabric"] and r["still_read_by"] == [b["id"]] and len(fab) == 1
    r = R("intel.source.remove", topic_id=b["id"], source_id=s1["id"])
    assert r["removed_from_fabric"] and not fab                         # the last reader takes it away
    assert "error" in R("intel.source.remove", topic_id=b["id"], source_id=s1["id"])


def test_bad_input_is_refused_before_the_fabric_sees_it(tmp_path):
    import asyncio
    mod, orch, fab = _with_fabric(tmp_path)
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="Quakes")["topic"]
    assert "error" in R("intel.source.add", topic_id="nope", kind="arxiv", value="cs.AI")
    assert "error" in R("intel.source.add", topic_id=t["id"], kind="usgs", value="4.5", bbox="1,2,3")
    assert "error" in R("intel.source.add", topic_id=t["id"], kind="feed", value="https://nofeed.example/")
    assert "error" in R("intel.source.add", topic_id=t["id"], kind="api", value="https://x.test/a.json", mapping="[1]")
    assert not fab
    ok = R("intel.source.add", topic_id=t["id"], kind="usgs", value="4.5", bbox="-11,49,2,61")
    assert ok["ok"] and ok["source"]["bbox"] == [-11.0, 49.0, 2.0, 61.0]
    cfg = [kw for n, kw in orch.CALLS if n == "fabric.sources.add"][-1]["config"]
    assert '"geo_field": "geometry.coordinates"' in cfg


def test_telegram_channels_and_site_names(tmp_path):
    import asyncio
    p = srcs.plan_source("telegram", "https://t.me/s/durov")
    assert p["source_type"] == "telegram" and p["url"] == "https://t.me/s/durov" and p["config"] == {"channel": "durov"}
    assert "error" in srcs.plan_source("telegram", "not a channel!")
    mod, orch, fab = _with_fabric(tmp_path)

    async def discover(url="", kind="", trace_id=None):          # a page whose feed link is titled "Atom"
        return {"feeds": [{"url": "https://feed.test/atom.xml", "title": "Atom"}], "site": "Feed Test Weblog"}
    orch.CAPABILITY_REGISTRY["fabric.sources.discover_feed"] = {"func": discover, "raw": discover}
    R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
    t = R("intel.topic.save", name="Agents")["topic"]
    assert R("intel.source.add", topic_id=t["id"], kind="feed", value="https://feed.test/")["source"]["label"] == \
        "Website or feed: Feed Test Weblog"


def test_purge_also_removes_the_fabric_copies(tmp_path):
    import asyncio, types
    mod, orch, fab = _with_fabric(tmp_path)
    ingested, deleted = [], []

    async def ingest_dataset(dataset_id="", data=None, source="", tags=None, source_id="", defer_embedding=False):
        ingested.extend(data or [])
    sys.modules["data_fabric"] = types.SimpleNamespace(ingest_dataset=ingest_dataset)

    async def delete_record(record_id="", dataset_id="", trace_id=None):
        deleted.append((record_id, dataset_id))
        return {"ok": True}
    orch.CAPABILITY_REGISTRY["fabric.delete_record"] = {"func": delete_record, "raw": delete_record}
    try:
        R = lambda n, **kw: asyncio.run(orch.CAPABILITY_REGISTRY[n]["func"](**kw))   # noqa: E731
        t = R("intel.topic.save", name="AI property valuation", brief="AI valuation pilots")["topic"]
        R("intel.sweep", topic_id=t["id"])
        ids = {s["id"] for s in mod.store().signals(t["id"])}
        assert ids and {row["_id"] for row in ingested} >= {f"intel-sig-{i}" for i in ids}   # stable fabric ids
        r = R("intel.topic.delete", id=t["id"], purge=True)
        assert r["ok"] and r["fabric_records_removed"] == len(ids)
        assert {rid for rid, _ in deleted} == {f"intel-sig-{i}" for i in ids} and {d for _, d in deleted} == {mod.SIGNALS_DATASET}
        assert "fabric_records_removed" not in R("intel.topic.delete", id="nope")    # no purge, no fabric work
    finally:
        sys.modules.pop("data_fabric", None)


def test_a_connection_source_reads_an_endpoint_and_carries_no_url_or_key():
    p = srcs.plan_source("connection", "Guardian /search?q=house+prices")
    assert p["source_type"] == "api" and p["connection"] == "guardian" and p["path"] == "/search?q=house+prices"
    assert p["url"] == "" and "key" not in str(p).lower().replace("house", "")
    assert p["id"] == srcs.plan_source("connection", "guardian /search?q=house+prices")["id"]     # shared by topics
    assert "error" in srcs.plan_source("connection", "/search?q=x")                               # no connection id
    assert "error" in srcs.plan_source("connection", "guardian https://evil.example/x")           # a full URL
    assert srcs.KINDS["connection"]["mapping"]


def test_indicator_feeds_read_through_connections():
    import ast, types
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "intel", "intel_capabilities.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ind_feed"]
    import typing
    ns = {"Dict": typing.Dict, "Any": typing.Any, "Optional": typing.Optional, "pm": types.SimpleNamespace(CLOB="https://clob", MANIFOLD="https://mf")}
    exec(compile(ast.Module(body=fn, type_ignores=[]), "x", "exec"), ns)
    f = ns["_ind_feed"]
    w = f({"dataset_id": "wiki:en:Bank of England"})
    assert w["conn"] == "wikimedia" and "Bank%20of%20England" in w["path"] and "{today:%Y%m%d}" in w["path"]
    assert f({"dataset_id": "pm:polymarket:t1", "market": {"source": "polymarket", "token": "t1"}})["conn"] == "polymarket_clob"
    assert f({"dataset_id": "pm:manifold:m1", "market": {"source": "manifold", "id": "m1"}})["path"].startswith("/bets?contractId=m1")
    assert f({"dataset_id": "mkt.macro.fred_unrate.1d"}) is None          # stored bars stay with markets
    assert "_api(" in src and src.count("await _http_json(") == 1       # every keyless read goes through _api
