"""The real-money broker capabilities end to end against a temporary SQLite store
and a fake broker: no order without out-of-band arming, a wrong or reused arm
code is refused, expiry turns trading off, limits refuse, the same
client_order_id never places twice, and an exception records 'unknown'.

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.markets.markets_capabilities as mc  # noqa: E402  (fails loudly, never skips)
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY  # noqa: E402


def raw(name):
    """The undecorated function: no activity, memory or cache side effects."""
    return CAPABILITY_REGISTRY[name]["raw"]


def run(c):
    return asyncio.run(c)


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = str(tmp_path / "fabric.db")

    def conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(mc, "_sqlite_conn", conn)
    from Vera.vera import state_paths
    monkeypatch.setattr(state_paths, "state_dir", lambda *p: tmp_path.joinpath(*p) if p else tmp_path)
    (tmp_path / "markets").mkdir()
    monkeypatch.delitem(sys.modules, "telegram_capabilities", raising=False)
    placed = []

    def fake_order(row, symbol, side, otype, amount, price):
        placed.append((symbol, side, amount))
        if symbol == "BOOM/USD":
            raise TimeoutError("broker timeout")
        return {"id": f"b{len(placed)}", "status": "new", "price": price or None}

    monkeypatch.setattr(mc, "_prov_order", fake_order)
    monkeypatch.setattr(mc, "_prov_quote", lambda row, symbol: 100.0)

    async def no_event(evt):
        return None

    monkeypatch.setattr(mc, "emit_event", no_event)
    mc._broker_insert_sync({"id": "a1", "exchange": "alpaca-paper", "label": "test", "k_key": "",
                            "k_secret": "", "k_password": "", "can_trade": 1,  # a legacy 'enabled' row
                            "created_at": "2026-10-01T00:00:00+00:00"})
    return {"tmp": tmp_path, "placed": placed}


def _code(world):
    return (world["tmp"] / "markets" / "broker-arm-a1.txt").read_text().strip()


def order(**kw):
    args = dict(id="a1", symbol="BTC/USD", side="buy", type="market", amount=1.0, confirm=True)
    args.update(kw)
    return run(raw("markets.broker.order")(**args))


def test_legacy_enabled_row_cannot_trade_without_arming(world):
    r = order()
    assert "not armed" in r["error"] and world["placed"] == []


def test_arm_then_enable_then_order_records_it(world):
    a = run(raw("markets.broker.arm")(id="a1"))
    assert a["ok"] and "file" in a["sent_to"] and "code" not in str(a)
    assert "wrong arm code" in run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code="nope"))["error"]
    s = run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code=_code(world)))
    assert s["ok"] and s["can_trade"]
    # the code is single use
    again = run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code=_code(world)))
    assert "no arm code pending" in again["error"]
    r = order(client_order_id="ord-1")
    assert r["ok"] and r["order"]["client_order_id"] == "ord-1" and len(world["placed"]) == 1
    dup = order(client_order_id="ord-1")
    assert dup["duplicate"] and len(world["placed"]) == 1          # never placed twice
    rec = mc._order_get_sync("ord-1")
    assert rec["status"] == "placed" and rec["broker_order_id"] == "b1"


def test_expired_trading_refuses(world, monkeypatch):
    run(raw("markets.broker.arm")(id="a1"))
    run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code=_code(world), hours=1))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mc._broker_update_sync("a1", {"trade_until": past})
    assert "not armed" in order()["error"]


def test_limits_need_the_arm_code_and_are_enforced(world):
    assert "no arm code pending" in run(raw("markets.broker.limits.set")(id="a1", arm_code="x", max_order_notional=50))["error"]
    run(raw("markets.broker.arm")(id="a1"))
    lim = run(raw("markets.broker.limits.set")(id="a1", arm_code=_code(world), max_order_notional=150,
                                       max_daily_notional=250, symbols=["BTC/USD"]))
    assert lim["ok"]
    run(raw("markets.broker.arm")(id="a1"))
    run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code=_code(world)))
    assert "allow-list" in order(symbol="ETH/USD")["error"]
    assert "per-order cap" in order(amount=2.0)["error"]                 # 2 x 100 > 150
    assert order(amount=1.0, client_order_id="d1")["ok"]                 # 100
    assert order(amount=1.0, client_order_id="d2")["ok"]                 # 200 so far
    assert "daily cap" in order(amount=1.0, client_order_id="d3")["error"]   # would be 300 > 250


def test_exception_records_unknown_and_blocks_retry(world):
    run(raw("markets.broker.arm")(id="a1"))
    run(raw("markets.broker.set_trading")(id="a1", enabled=True, arm_code=_code(world)))
    r = order(symbol="BOOM/USD", client_order_id="t1")
    assert "unknown" in r["error"]
    assert mc._order_get_sync("t1")["status"] == "unknown"
    again = order(symbol="BOOM/USD", client_order_id="t1")
    assert again["duplicate"] and len(world["placed"]) == 1


def test_disable_needs_no_code(world):
    r = run(raw("markets.broker.set_trading")(id="a1", enabled=False))
    assert r["ok"] and r["can_trade"] is False
