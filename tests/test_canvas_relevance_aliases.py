"""
The relevance pass knows a name by its ticker (owner 2026-10-11: a turn about bitcoin scored the BTC-USD chart 0.12, so the
chart left the desk's focus). Compound tokens (yahoo:btc-usd) are split into their parts, and entities and turn words are
expanded with a small name <-> ticker table. Runs the real scoring against the stub orchestrator.
"""
from test_canvas_resolver import C

CHART = {"key": "enrich:markets.chart:b56e071a11", "type": "widget", "state": "now",
         "content": {"title": "markets · chart", "data": {"symbol": "BTC-USD", "key": "yahoo:BTC-USD", "last": 83009.9}}}
OTHER = {"key": "widget:trace-1", "type": "widget", "state": "now", "content": {"title": "Chart", "data": [1, 2, 3]}}


def _score(b, text, ents):
    return C._rel_score(b, "", text, ents, [], set(), {})


def test_bitcoin_turn_reaches_the_btc_chart_as_an_entity():
    s = _score(CHART, "what is the bitcoin price today", ["bitcoin"])
    assert s["signals"].get("entity") == 0.85 and s["score"] >= 0.75


def test_a_ticker_turn_reaches_a_named_item_too():
    named = {"key": "records:n", "type": "records", "state": "now", "content": {"title": "Ethereum news"}}
    assert _score(named, "eth this week", ["ETH"])["signals"].get("entity") == 0.85


def test_unrelated_items_stay_out():
    s = _score(OTHER, "what is the bitcoin price today", ["bitcoin"])
    assert "entity" not in s["signals"] and s["score"] < 0.35


def test_compound_tokens_give_their_parts():
    w = C._rel_words("yahoo:BTC-USD markets.chart")
    assert {"btc", "usd", "yahoo", "markets", "chart"} <= w and "yahoo:btc-usd" in w


def test_ordinary_words_are_not_tickers():
    assert "dot" not in C._rel_expand({"polka"}) and C._rel_expand({"link"}) == {"link"}
