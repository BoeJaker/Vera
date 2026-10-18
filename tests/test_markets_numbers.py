"""S19 / D23-2: the markets numbers every long backtest and the portfolio rest on.

B1 portfolio.history valued each asset with the whole ledger's quantity; B2 bars before Sep 2001 (stored in ms, under
1e12) were kept as seconds; B4 two concurrent sim orders spent the same cash; B5 positions came from the newest 2,000
orders only. The pure helpers are tested directly; the sim write runs on a real SQLite file."""
import importlib.util
import os
import sqlite3
import threading

import pytest

pytestmark = pytest.mark.critical
np = pytest.importorskip("numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MK = os.path.join(ROOT, "vera", "markets")


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(MK, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def lab():
    return _load("markets_lab_capabilities")


@pytest.fixture(scope="module")
def studio():
    return _load("markets_studio_capabilities")


def test_a_1990_bar_is_read_in_seconds(lab):
    ms_1990 = 631152000000                          # 1990-01-01 in ms: under the old 1e12 test
    assert lab.bar_seconds(ms_1990) == 631152000
    assert lab.bar_seconds(1700000000) == 1700000000          # already seconds
    an = _load("markets_analysis_capabilities")
    arr = an.bars_to_arrays([{"timestamp": ms_1990, "close": 1}, {"timestamp": 1700000000000, "close": 2}])
    assert list(arr["t"]) == [631152000, 1700000000]


def _iso_to_ms(s):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000) if s else None


def test_a_two_asset_ledger_is_the_sum_of_its_one_asset_curves(lab):
    day0 = 1704067200                                # 2024-01-01
    grid = [day0 + i * 86400 for i in range(3)]
    closes = {"a": (grid, [10.0, 11.0, 12.0]), "b": (grid, [100.0, 90.0, 80.0])}
    tx_a = [{"symbol_key": "a", "side": "buy", "qty": 5, "price": 10, "ts": "2024-01-01T00:00:00"}]
    tx_b = [{"symbol_key": "b", "side": "buy", "qty": 1, "price": 100, "ts": "2024-01-02T00:00:00"}]
    va, ca = lab.portfolio_curve(tx_a, closes, grid, _iso_to_ms)
    vb, cb = lab.portfolio_curve(tx_b, closes, grid, _iso_to_ms)
    v, c = lab.portfolio_curve(tx_a + tx_b, closes, grid, _iso_to_ms)
    assert v == [round(x + y, 2) for x, y in zip(va, vb)] and c == [round(x + y, 2) for x, y in zip(ca, cb)]
    assert v == [50.0, 145.0, 140.0]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "mkt.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE mkt_sim_accounts (id TEXT PRIMARY KEY, name TEXT, start_cash REAL, cash REAL,
                                       currency TEXT, meta TEXT, created_at TEXT);
        CREATE TABLE mkt_sim_orders (id TEXT PRIMARY KEY, account_id TEXT, symbol_key TEXT, side TEXT, qty REAL,
                                     price REAL, fees REAL DEFAULT 0, note TEXT, source TEXT, ts TEXT);
        INSERT INTO mkt_sim_accounts (id, start_cash, cash) VALUES ('acc', 100, 100);
    """)
    conn.commit()
    conn.close()
    return path


def _order(studio, path, oid, side, qty, price=10.0):
    conn = sqlite3.connect(path, timeout=10)
    try:
        return studio.sim_write_order(conn, oid=oid, account_id="acc", symbol_key="x", side=side, qty=qty,
                                      price=price, fees=0.0, note="", source="t", ts=oid)
    finally:
        conn.close()


def _cash(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT cash FROM mkt_sim_accounts WHERE id='acc'").fetchone()[0]
    finally:
        conn.close()


def test_two_concurrent_buys_cannot_spend_the_same_cash(studio, db):
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(_order(studio, db, f"o{i}", "buy", 6)))
               for i in range(2)]                       # each wants 60 of the 100
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is None for r in results) == [False, True]
    assert _cash(db) == pytest.approx(40.0)


def test_a_sell_is_checked_against_the_position_held_now(studio, db):
    assert _order(studio, db, "b1", "buy", 5) is None                # 5 held, cash 50
    assert _order(studio, db, "s1", "sell", 3) is None               # 2 held, cash 80
    err = _order(studio, db, "s2", "sell", 3)
    assert err and "position" in err
    assert _cash(db) == pytest.approx(80.0)
