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
    assert "revenue 1200" in sc.summarise({"revenue": 1200, "expenses": 800, "ok": True})


def test_the_note_names_each_item_with_what_it_belongs_to_and_stays_short():
    placed = [{"title": "Portfolio positions", "domain_label": "your trading account (broker, portfolio, positions)", "summary": "2 rows: BTC/USD"},
              {"title": "Ledger", "domain_label": "your business accounts (ledger, orders, inventory, tax)", "summary": "revenue 1200"}]
    note = sc.prompt_block(placed, pending=1)
    assert note.startswith("## On the canvas beside this reply")
    assert "- [your trading account (broker, portfolio, positions)] Portfolio positions: 2 rows: BTC/USD" in note
    assert "- [your business accounts (ledger, orders, inventory, tax)] Ledger: revenue 1200" in note
    assert "1 more item is still arriving" in note
    assert len(sc.prompt_block(placed * 40)) <= 1200 and sc.prompt_block([]) == ""
