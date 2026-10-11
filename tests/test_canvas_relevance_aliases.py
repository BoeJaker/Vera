"""
The relevance pass knows a name by its symbol, from LIVE market data (owner 2026-10-11: a turn about bitcoin scored the
BTC-USD chart 0.12; "you hardcoded aliases for tickers - bad idea create a proper converter using live data you dont have
to maintain"). Groups come from the tracked universe (markets.baseline.list / overview / watchlist) and, for an entity it
does not know, one bounded markets.lookup. Here the markets capabilities are stand-ins registered in the stub registry.
"""
import asyncio
import time

from test_canvas_resolver import C, ORCH, _run

CHART = {"key": "enrich:markets.chart:b56e071a11", "type": "widget", "state": "now",
         "content": {"title": "markets · chart", "data": {"symbol": "BTC-USD", "key": "yahoo:BTC-USD", "last": 83009.9}}}
OTHER = {"key": "widget:trace-1", "type": "widget", "state": "now", "content": {"title": "Chart", "data": [1, 2, 3]}}
CALLS = []


def _register(baseline=None, lookup=None):
    async def base(**kw):
        CALLS.append(("baseline", kw)); return {"assets": baseline or []}
    async def look(query="", limit=5, **kw):
        CALLS.append(("lookup", query)); return {"results": (lookup or {}).get(query, [])}
    async def empty(**kw):
        return {}
    ORCH.CAPABILITY_REGISTRY.update({"markets.baseline.list": {"func": base}, "markets.lookup": {"func": look},
                                     "markets.overview": {"func": empty}, "markets.watchlist.list": {"func": empty}})
    C._ALIAS.update({"at": 0.0, "of": {}, "task": None}); C._ALIAS_LOOK.clear(); CALLS.clear()


def _build():
    _run(C._alias_build())


def test_the_universe_links_a_name_to_its_symbol():
    _register(baseline=[{"symbol": "BTC-USD", "name": "Bitcoin USD"}, {"symbol": "ETH-USD", "name": "Ethereum USD"}])
    _build()
    amap = _run(C._rel_alias_map(["bitcoin"]))
    s = C._rel_score(CHART, "", "what is the bitcoin price today", ["bitcoin"], [], set(), {}, amap)
    assert s["signals"].get("entity") == 0.85 and s["score"] >= 0.75
    assert "usd" not in amap.get("bitcoin", set())          # the quote currency is never a link


def test_an_unknown_entity_is_looked_up_once_and_kept():
    _register(baseline=[], lookup={"solana": [{"symbol": "SOL-USD", "name": "Solana USD"}]})
    _build()
    named = {"key": "widget:sol", "type": "widget", "state": "now", "content": {"title": "chart", "data": {"symbol": "SOL-USD"}}}
    amap = _run(C._rel_alias_map(["Solana"]))
    assert C._rel_score(named, "", "solana price", ["Solana"], [], set(), {}, amap)["signals"].get("entity") == 0.85
    _run(C._rel_alias_map(["Solana"]))
    assert [c for c in CALLS if c[0] == "lookup"] == [("lookup", "solana")]   # cached


def test_a_near_miss_lookup_is_not_a_link():
    _register(baseline=[], lookup={"apple": [{"symbol": "APLE", "name": "Apple Hospitality REIT"}, {"symbol": "PINE", "name": "Pineapple Corp"}]})
    amap = _run(C._rel_alias_map(["apple"]))
    assert "pine" not in amap.get("apple", set())


def test_unrelated_items_stay_out_and_no_markets_means_no_aliases():
    ORCH.CAPABILITY_REGISTRY.pop("markets.lookup", None); ORCH.CAPABILITY_REGISTRY.pop("markets.baseline.list", None)
    C._ALIAS.update({"at": 0.0, "of": {}, "task": None}); C._ALIAS_LOOK.clear()
    amap = _run(C._rel_alias_map(["bitcoin"]))
    assert amap == {}
    s = C._rel_score(OTHER, "", "what is the bitcoin price today", ["bitcoin"], [], set(), {}, amap)
    assert "entity" not in s["signals"] and s["score"] < 0.35


def test_compound_tokens_give_their_parts():
    w = C._rel_words("yahoo:BTC-USD markets.chart")
    assert {"btc", "usd", "yahoo", "markets", "chart"} <= w and "yahoo:btc-usd" in w


def test_no_hand_written_ticker_table():
    src = open(C.__file__, encoding="utf-8").read()
    assert "_REL_ALIASES" not in src and '"bitcoin": (' not in src
