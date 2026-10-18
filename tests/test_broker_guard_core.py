"""Real-money order rules (vera/markets/broker_guard_core.py): arming is out of
band and single-use, armed trading expires, limits and the symbol allow-list
refuse, and a capped market order without a price is refused, not guessed."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.critical

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.markets import broker_guard_core as g  # noqa: E402

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def test_arm_code_shape_and_hash_binding():
    c = g.new_arm_code()
    assert len(c) == 6 and c.isdigit()
    h = g.hash_code(c, "acct-1")
    assert g.verify_code(h, c, "acct-1")
    assert not g.verify_code(h, c, "acct-2")          # bound to the account
    assert not g.verify_code(h, "000000" if c != "000000" else "111111", "acct-1")
    assert not g.verify_code(None, c, "acct-1") and not g.verify_code(h, "", "acct-1")


def test_trading_expiry_and_legacy_rows():
    until = g.trade_until(NOW, 8)
    assert g.trading_active(until, NOW + timedelta(hours=7, minutes=59))
    assert not g.trading_active(until, NOW + timedelta(hours=8, seconds=1))
    assert not g.trading_active(None, NOW)               # a row from before arming
    assert not g.trading_active("garbage", NOW)
    capped = g.trade_until(NOW, 999)
    assert g.trading_active(capped, NOW + timedelta(hours=23)) and not g.trading_active(capped, NOW + timedelta(hours=25))


def test_no_limits_allows_a_positive_order():
    assert g.check_order(g.Limits(), symbol="BTC/USDT", amount=0.1, price=None, day_notional_so_far=0) is None
    assert g.check_order(g.Limits(), symbol="BTC/USDT", amount=0, price=1, day_notional_so_far=0)


def test_symbol_allow_list_and_amount_cap():
    lim = g.Limits(max_order_amount=1.0, symbols=("BTC/USDT",))
    assert "allow-list" in g.check_order(lim, symbol="ETH/USDT", amount=0.5, price=None, day_notional_so_far=0)
    assert "per-order cap" in g.check_order(lim, symbol="btc/usdt", amount=2, price=None, day_notional_so_far=0)
    assert g.check_order(lim, symbol="btc/usdt", amount=0.5, price=None, day_notional_so_far=0) is None


def test_notional_caps_need_a_price_and_are_enforced():
    lim = g.Limits(max_order_notional=1000, max_daily_notional=1500)
    assert "no price" in g.check_order(lim, symbol="X", amount=1, price=None, day_notional_so_far=0)
    assert "per-order cap" in g.check_order(lim, symbol="X", amount=2, price=600, day_notional_so_far=0)
    assert g.check_order(lim, symbol="X", amount=1, price=900, day_notional_so_far=0) is None
    assert "daily cap" in g.check_order(lim, symbol="X", amount=1, price=900, day_notional_so_far=700)


def test_limits_from_row_tolerates_missing_and_bad_values():
    lim = g.Limits.from_row({"max_order_amount": None, "symbols": "not json"})
    assert lim == g.Limits()
    lim = g.Limits.from_row({"max_order_notional": 50, "symbols": '["btc/usdt"]'})
    assert lim.max_order_notional == 50 and lim.symbols == ("BTC/USDT",)


def test_client_order_id_is_sanitised():
    assert g.clean_client_order_id("abc-1_2.3:x") == "abc-1_2.3:x"
    assert g.clean_client_order_id("a b;c'd") == "abcd"
    assert len(g.clean_client_order_id("x" * 200)) == 64
