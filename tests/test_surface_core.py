"""surface_core: the map from a turn to the estate's surfaces (canvas-enrichment slice 3)."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_spec = importlib.util.spec_from_file_location("surface_core", _ROOT / "vera" / "canvas" / "surface_core.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

S = lambda props=None, req=None: {"schema": {"type": "object", "properties": props or {}, "required": req or []}}
REG = {
    "markets.overview": S(), "markets.quotes": S(), "markets.news.feed": S({"symbol_key": {}, "query": {}}),
    "markets.symbol.chart": S({"symbol": {}}, ["symbol"]), "markets.fetch": S({"symbol": {}}),
    "markets.sources": S(), "markets.portfolio.positions": S(), "markets.broker.list": S(),
    "business.dashboard": S(), "business.txn.list": S({"limit": {}}), "business.account.upsert": S(),
    "docker.ps": S({"host_id": {}}), "nodes.storage": S(), "web.search": S({"query": {}}),
}
MEASURED = {"markets.overview": {"shape": "items"}, "markets.quotes": {"shape": "items"}, "markets.sources": {"shape": "items"},
            "markets.portfolio.positions": {"shape": "items"}, "business.dashboard": {"shape": "values"},
            "business.txn.list": {"shape": "items"}, "nodes.storage": {"shape": "items"}}
LAYOUTS = [{"key": "markets-board", "widgets": [{"record": {"id": "mk-ov", "form": "table", "source": "markets.overview", "title": "Overview", "read": {"args": {}}}},
                                                 {"record": {"id": "sec", "form": "section", "source": ""}}]}]
PANELS = [{"id": "markets-studio", "label": "Markets Studio"}, {"id": "business-panel", "label": "Business"}, {"id": "notebook", "label": "Notebook"}]
IDX = sc.build(REG, MEASURED, LAYOUTS, PANELS)


def test_domains_by_namespace_accounts_first():
    assert sc.domain_of("markets.portfolio.positions") == "trading_account"
    assert sc.domain_of("markets.overview") == "markets"
    assert sc.domain_of("business.txn.list") == "business_accounts"
    assert sc.domain_of("business.product.list") == "" and sc.domain_of("docker.ps") == "estate"


def test_the_index_holds_reads_widgets_and_panels_and_never_a_write():
    ids = {(s["kind"], s["id"]) for s in IDX["surfaces"]}
    assert ("read", "markets.overview") in ids and ("widget", "markets-board:mk-ov") in ids and ("panel", "markets-studio") in ids
    assert not any(s["id"] in ("markets.fetch", "business.account.upsert") for s in IDX["surfaces"])     # fetch / upsert write
    assert not any(s["id"] == "markets-board:sec" for s in IDX["surfaces"])
    chart = next(s for s in IDX["surfaces"] if s["id"] == "markets.symbol.chart")
    assert chart["takes"] == {"symbol": "TICKER"} and chart["needs"] == ["symbol"]
    assert IDX["counts"]["markets"]["widget"] == 1


def test_a_markets_turn_with_a_ticker_fills_the_symbol_and_brings_the_board():
    ents = [{"text": "BTC/USD", "type": "TICKER"}]
    r = sc.find(IDX, "how is the btc price doing today?", ents)
    assert r["domains"][0]["id"] == "markets"
    got = {(s["kind"], s.get("cap") or s.get("panel")): s for s in r["surfaces"]}
    assert ("widget", "markets.overview") in got
    assert got[("read", "markets.symbol.chart")]["args"] == {"symbol": "BTC/USD"}
    assert not any(s.get("cap") == "markets.sources" for s in r["surfaces"])          # machinery is not a glance


def test_my_account_brings_both_accounts_named_apart():
    r = sc.find(IDX, "what's going on with my account?", [])
    ids = [d["id"] for d in r["domains"]]
    assert "trading_account" in ids and "business_accounts" in ids
    labels = {s["domain"]: s["domain_label"] for s in r["surfaces"]}
    assert labels.get("trading_account", "").startswith("your trading account")
    assert labels.get("business_accounts", "").startswith("your business accounts")


def test_a_business_turn_is_business_not_trading():
    r = sc.find(IDX, "show me this month's revenue and expenses for the business", [])
    assert r["domains"][0]["id"] == "business_accounts" and "trading_account" not in [d["id"] for d in r["domains"]]


def test_nothing_specific_finds_nothing():
    assert sc.find(IDX, "thanks, that's great", [])["surfaces"] == []


def test_a_read_in_one_line_rows_by_name_with_a_figure_or_a_records_figures():
    s = sc.summarise({"positions": [{"symbol": "BTC/USD", "qty": 0.5, "pnl": 120.5}, {"symbol": "ETH/USD", "qty": 2}], "ok": True})
    assert s.startswith("2 rows: BTC/USD (qty 0.5); ETH/USD (qty 2)")
    assert "revenue 1,200" in sc.summarise({"revenue": 1200, "expenses": 800, "ok": True})


def test_a_read_leads_with_the_rows_the_turn_named_with_all_their_figures():
    quotes = {"items": [{"key": "yahoo:SPXL", "name": "SPXL", "last": 288.6}] * 3
              + [{"key": "yahoo:BTC-USD", "name": "Bitcoin", "last": 84884.3, "change_pct": 1.69, "volume": 2.8e10}]}
    s = sc.summarise(quotes, focus=["btc/usd".upper()])
    assert s.startswith("4 rows; for what was asked: Bitcoin (last 84,884, change_pct 1.69)"), s
    assert "SPXL" not in s
    assert sc.summarise(quotes, focus=["ETH"]).startswith("4 rows: SPXL")      # nothing named in it: as before


def test_the_note_names_each_item_with_what_it_belongs_to_and_stays_short():
    placed = [{"title": "Portfolio positions", "domain_label": "your trading account (broker, portfolio, positions)", "summary": "2 rows: BTC/USD"},
              {"title": "Ledger", "domain_label": "your business accounts (ledger, orders, inventory, tax)", "summary": "revenue 1200"}]
    note = sc.prompt_block(placed, pending=1)
    assert note.startswith("## On the canvas beside this reply")
    assert "- [your trading account (broker, portfolio, positions)] Portfolio positions: 2 rows: BTC/USD" in note
    assert "- [your business accounts (ledger, orders, inventory, tax)] Ledger: revenue 1200" in note
    assert "1 more item is still arriving" in note
    assert len(sc.prompt_block(placed * 40)) <= 1200 and sc.prompt_block([]) == ""


def test_a_registry_with_odd_entries_still_builds():
    reg = dict(REG, **{"markets.alias": "markets.overview", "markets.weird": {"schema": "not a schema"},
                       "markets.listy": {"schema": {"properties": ["symbol"], "required": "symbol"}}})
    idx = sc.build(reg, MEASURED, LAYOUTS + [["not", "a", "layout"], {"widgets": ["id-only"]}], PANELS + ["markets-x"])
    assert any(s["id"] == "markets.overview" for s in idx["surfaces"]) and not any(s["id"] == "markets.alias" for s in idx["surfaces"])


def test_headline_reads_lead_and_a_passing_time_word_does_not_bring_the_diary():
    reg = dict(REG, **{"cal.events.list": S()})
    idx = sc.build(reg, dict(MEASURED, **{"cal.events.list": {"shape": "events"}}), LAYOUTS, PANELS)
    r = sc.find(idx, "what's going on with my account?", [])
    tr = [s.get("cap") for s in r["surfaces"] if s["domain"] == "trading_account"]
    assert tr and tr[0] == "markets.portfolio.positions" and r["surfaces"][0].get("lead")
    biz = [s.get("cap") for s in r["surfaces"] if s["domain"] == "business_accounts"]
    assert biz and biz[0] == "business.dashboard"
    r2 = sc.find(idx, "how is the btc price doing this week?", [{"text": "BTC", "type": "TICKER"}])
    assert [d["id"] for d in r2["domains"]] == ["markets"]


def test_a_read_the_turn_named_comes_before_the_headlines_and_a_series_is_summarised():
    reg = dict(REG, **{"markets.chart": S({"symbol": {}})})
    idx = sc.build(reg, MEASURED, LAYOUTS, PANELS)
    r = sc.find(idx, "how is bitcoin doing?", [{"text": "BTC", "type": "TICKER"}], max_items=3)
    top2 = r["surfaces"][:2]
    assert {x["cap"] for x in top2} == {"markets.chart", "markets.symbol.chart"} and all(x["args"] == {"symbol": "BTC"} for x in top2)
    s = sc.summarise({"t": [1, 2, 3], "equity": [100.0, 105.0, 110.0], "ok": True})
    assert s == "3 points: equity last 110 (+10.0%)"


def test_upkeep_is_never_run_and_a_charts_figures_lead_its_summary():
    assert not sc.is_read("markets.history.repair") and not sc.is_read("markets.history.audit") and sc.is_read("markets.chart")
    # a chart says its last close, the latest bar's move and the move over the window it shows - with the window named
    # (owner, 2026-10-04: "change_pct 39.46" over 120 days read as today's move; "8.488e+04" for a price)
    bars = [{"t": i, "open": 60000 + i, "high": 61000 + 200 * i, "low": 59000 + 100 * i, "close": c}
            for i, c in enumerate([60000.0, 70000.0, 83474.6, 84884.3])]
    s = sc.summarise({"symbol": "BTC-USD", "timeframe": "1d", "last": 84884.3, "change_pct": 39.46, "count": 4, "bars": bars})
    assert s == "BTC-USD - price 84,884 (the latest close); latest day +1.69%; over the 4 days shown +41.5%; range 59,000 to 61,600", s
    # bars with no closes: the record's own figures, written as a person writes them
    assert sc.summarise({"symbol": "BTC-USD", "last": 84884.3, "change_pct": 4.83, "bars": [{"t": 1}] * 3}) == "BTC-USD - last 84,884; change_pct 4.83 (3 rows)"
    assert [sc._fmt(x) for x in (84884.3, 1.6912, 0.000421, 2.8e10, 7, True)] == ["84,884", "1.691", "0.00042", "28 bn", "7", "True"]
