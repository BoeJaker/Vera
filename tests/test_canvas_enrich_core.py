"""enrich_core: what else is relevant to a turn, run safely, drawn as browsable records."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.canvas import enrich_core as ec  # noqa: E402

MEASURED = {
    "markets.alerts.list": {"shape": "values", "ms": 95, "note": ""},
    "markets.evolve.tick": {"shape": "items", "ms": 56, "note": ""},          # measured, but 'tick' changes things
    "markets.backtest.list": {"shape": "items", "ms": 138, "note": " empty when measured"},
    "markets.bars": {"err": "dataset_id required", "ms": 57},                # needs an argument
    "sysmon.status": {"shape": "values", "ms": 40, "note": ""},
    "fabric.graphs.snapshot": {"shape": "graph", "ms": 5583, "note": ""},    # too slow to run unasked
}
READS = {"markets.alerts.list", "markets.evolve.tick", "sysmon.status", "fabric.graphs.snapshot", "markets.backtest.list"}
is_read = lambda n: n in READS


def found(*pairs):
    return [{"name": n, "score": s, "description": n + ". does a thing"} for n, s in pairs]


def test_terms_and_topic_drop_the_filler():
    assert ec.topic("Could you please show me the latest crypto market data?") == "latest crypto market data"
    assert ec.terms("BTC btc eth") == ["btc", "eth"]


def test_gates_follow_what_the_turn_is_about():
    g = ec.gates("latest crypto market data")
    assert g["finance"] and g["fresh"] and g["fresh_or_world"]
    g2 = ec.gates("refactor the canvas resolver")
    assert not g2["finance"] and not g2["fresh_or_world"] and g2["always"]


def test_writes_reads_the_verbs_of_a_name():
    assert ec.writes("markets.evolve.tick") and ec.writes("canvas.add") and ec.writes("evolve.sandbox.restart")
    assert not ec.writes("markets.alerts.list") and not ec.writes("sysmon.status")


def test_dashboard_candidates_are_measured_cheap_reads_that_write_nothing():
    got = ec.dashboard_candidates(found(("markets.alerts.list", 8), ("markets.evolve.tick", 9), ("markets.bars", 9),
                                        ("markets.backtest.list", 9), ("fabric.graphs.snapshot", 9), ("sysmon.status", 3)),
                                  MEASURED, is_read)
    assert [c["cap"] for c in got] == ["markets.alerts.list"]
    assert got[0]["args"] == {} and got[0]["kind"] == "widget" and got[0]["why"].startswith("relevant to the turn")


def test_a_glance_beats_the_machinery_and_the_weak_tail_is_dropped():
    M = {"markets.overview.board": {"shape": "items", "ms": 90, "note": ""}, "markets.news.sources": {"shape": "items", "ms": 50, "note": ""},
         "markets.sim.templates": {"shape": "items", "ms": 50, "note": ""}, "markets.sentiment.map": {"shape": "values", "ms": 60, "note": ""},
         "markets.symbols.popular": {"shape": "items", "ms": 60, "note": ""}}
    f = [{"name": "markets.news.sources", "score": 9, "description": "Configured news sources."},
         {"name": "markets.sim.templates", "score": 9, "description": "Simulation templates."},
         {"name": "markets.sentiment.map", "score": 7, "description": "Crypto and market sentiment by asset."},
         {"name": "markets.overview.board", "score": 9, "description": "The market overview: every asset, its latest price and move."},
         {"name": "markets.symbols.popular", "score": 3, "description": "Popular symbols."}]
    got = [c["cap"] for c in ec.dashboard_candidates(f, M, lambda n: True, turn="latest crypto market data")]
    assert got[:2] == ["markets.overview.board", "markets.sentiment.map"], got
    assert "markets.news.sources" not in got and "markets.symbols.popular" not in got, got


def test_plan_for_a_markets_question():
    p = ec.plan("latest crypto market data", found(("markets.alerts.list", 8)), MEASURED, is_read)
    caps = [s["cap"] for s in p["steps"]]
    assert caps == ["markets.overview", "markets.news.feed", "web.search", "fabric.query", "markets.alerts.list"]
    news = next(s for s in p["steps"] if s["cap"] == "markets.news.feed")
    assert news["args"]["query"] == "latest crypto market data" and news["kind"] == "news"


def test_plan_for_an_internal_question_does_not_search_the_web():
    p = ec.plan("refactor the canvas resolver keys", [], MEASURED, is_read)
    assert [s["cap"] for s in p["steps"]] == ["fabric.query"]


def test_plan_sources_switch_parts_off():
    p = ec.plan("latest crypto news", found(("markets.alerts.list", 8)), MEASURED, is_read, sources=("memory",))
    assert [s["cap"] for s in p["steps"]] == ["fabric.query"]
    p2 = ec.plan("latest crypto news", found(("markets.alerts.list", 8)), MEASURED, is_read, max_widgets=0)
    assert "markets.alerts.list" not in [s["cap"] for s in p2["steps"]]


def test_records_from_every_known_answer_and_nothing_cut():
    web = {"results": [{"url": "https://www.coindesk.com/a%d" % i, "title": "BTC up %d" % i, "snippet": "s", "engine": "ddg"} for i in range(3)]
           + [{"url": "https://x.io/%d" % i, "title": "t%d" % i} for i in range(40)]}
    r = ec.records_from("web.search", web)
    assert len(r) == 43 and r[0]["domain"] == "coindesk.com" and r[0]["meta"] == {"engine": "ddg"}
    news = ec.records_from("markets.news.feed", {"headlines": [{"title": "ETF flows", "snippet": "x", "url": "https://n.io/1"}]})
    assert news[0]["title"] == "ETF flows" and news[0]["url"] == "https://n.io/1"
    fab = ec.records_from("fabric.query", {"results": [{"id": "r1", "dataset_id": "d1", "score": 0.61234,
                                                        "text": "Bitcoin halving notes. More text follows here."}]})
    assert fab[0]["title"] == "Bitcoin halving notes" and fab[0]["ref"] == {"record_id": "r1", "dataset_id": "d1"}
    assert fab[0]["score"] == 0.612


def test_records_item_and_keys():
    assert ec.records_item("web.search", {"query": "q"}, {"results": []}) is None
    it = ec.records_item("web.search", {"query": "q"}, {"results": [{"url": "https://a.io", "title": "A"}]}, kind="web", query="q")
    assert it["kind"] == "web" and it["title"] == "Web · q" and it["total"] == 1 and it["source"] == "web.search"
    assert ec.item_key("markets.overview", {}, "widget") == "enrich:markets.overview"
    k1 = ec.item_key("web.search", {"query": "a"}, "web")
    assert k1.startswith("enrich:web.search:") and k1 == ec.item_key("web.search", {"query": "a"}, "web")
    assert k1 != ec.item_key("web.search", {"query": "b"}, "web")


def test_nested_rows_are_found_and_a_grouping_flattened():
    ov = {"groups": [{"name": "Crypto", "assets": [{"key": "BTC", "name": "Bitcoin", "last": 68212.5, "change_1d": 1.8}],
                      "count": 1, "breadth_1d": 0.6},
                     {"name": "FX", "assets": [{"key": "EURUSD", "name": "Euro", "last": 1.0831, "change_1d": -0.1}]}],
          "count": 2, "asof": "2026-09-27"}
    r = ec.records_from("markets.overview", ov)
    assert [x["title"] for x in r] == ["Bitcoin", "Euro"]
    assert r[0]["meta"]["group"] == "Crypto" and r[0]["meta"]["last"] == "68,212.5", r[0]["meta"]
    assert r[0]["meta"]["change_1d"] == "1.8" and r[0]["meta"]["key"] == "BTC"
    sm = {"tracked": [{"key": k, "name": k.upper(), "score": None, "label": "n/a"} for k in ("btc", "eth", "sol")],
          "benchmarks": [{"key": "spx", "name": "S&P 500", "score": 0.4, "label": "bullish", "summary": "risk on"}]}
    assert len(ec.records_from("markets.sentiment.map", sm)) == 3      # the longest list


def test_entities_decoded_duplicates_and_boilerplate_dropped():
    r = ec.records_from("fabric.query", {"results": [
        {"id": "a", "text": "Today&#x27;s market &amp; more. Body."},
        {"id": "b", "text": "Today&#x27;s market &amp; more. Body."},
        {"id": "c", "text": "Privacy Policy This Privacy Policy describes what we collect."},
        {"id": "d", "text": "Crypto fear and greed index. Explained."}]})
    assert [x["title"] for x in r] == ["Today's market & more", "Crypto fear and greed index"]
    w = ec.records_from("web.search", {"results": [{"url": "https://a.io/x/", "title": "A"}, {"url": "https://a.io/x", "title": "A again"}]})
    assert len(w) == 1


def test_overlap_by_page():
    a = [{"url": "https://a.io/1"}, {"url": "https://b.io/2"}, {"url": "https://c.io/3"}]
    b = [{"url": "https://a.io/1/"}, {"url": "https://b.io/2"}]
    assert ec.overlap(a, b) == 1.0 and ec.overlap(a, [{"url": "https://z.io"}]) == 0.0 and ec.overlap([], b) == 0.0


def test_a_fabric_record_shown_unasked_must_be_about_the_turn():
    fab = {"results": [{"id": "1", "text": "Back to jobs. Category Manager - market leader in games.", "score": 0.5},
                       {"id": "2", "text": "Crypto fear and greed index. Explained.", "score": 0.3}]}
    it = ec.records_item("fabric.query", {}, fab, kind="memory", query="latest crypto market data")
    assert [x["id"] for x in it["items"]] == ["2"]
    assert ec.records_item("fabric.query", {}, {"results": [fab["results"][0]]}, kind="memory", query="latest crypto market data") is None
    assert ec.specific_terms("show me the latest market data") == []      # nothing specific: nothing filtered
    assert len(ec.about([{"title": "x"}], "show me the latest market data")) == 1
