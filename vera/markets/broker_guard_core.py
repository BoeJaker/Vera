"""Pure rules for REAL broker orders (markets.broker.*).

Real orders used to need only two request fields (`confirm=true` on the order,
`can_trade` set by `markets.broker.set_trading`). Both could be set by any
caller of the unauthenticated API, so a web page the owner opened could place
a real order. These rules move the decision to something a remote caller
cannot supply:

- arming: enabling trading needs a short-lived code delivered out of band
  (Telegram, or a 0600 file on the host). Only its hash is stored; it expires
  in minutes and is single-use.
- expiry: an armed account trades for a bounded number of hours, then must be
  armed again. An account that predates this has no expiry recorded and counts
  as not armed.
- limits: a per-order base-unit cap, per-order and per-day notional caps, and an
  optional symbol allow-list, stored on the account and checked on every order.
- idempotency: an order carries a client_order_id; the same id never places a
  second order.

No I/O here; markets_capabilities.py does the storage, delivery and broker calls.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

ARM_TTL_S = 600                 # an arm code is valid for 10 minutes
DEFAULT_TRADE_HOURS = 8
MAX_TRADE_HOURS = 24


def new_arm_code() -> str:
    """A 6-digit code a person can type from a phone."""
    return f"{secrets.randbelow(10**6):06d}"


def hash_code(code: str, account_id: str) -> str:
    return hashlib.sha256(f"{account_id}:{(code or '').strip()}".encode()).hexdigest()


def verify_code(stored_hash: Optional[str], code: str, account_id: str) -> bool:
    if not stored_hash or not code:
        return False
    return hmac.compare_digest(stored_hash, hash_code(code, account_id))


def _parse(ts: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def trade_until(now: datetime, hours: float) -> str:
    h = min(max(float(hours or DEFAULT_TRADE_HOURS), 0.25), MAX_TRADE_HOURS)
    return (now + timedelta(hours=h)).isoformat()


def trading_active(trade_until_iso: Optional[str], now: datetime) -> bool:
    """Armed and not yet expired. A missing value (a row from before arming) is not armed."""
    d = _parse(trade_until_iso or "")
    return bool(d and now < d)


@dataclass(frozen=True)
class Limits:
    max_order_amount: float = 0.0       # base units per order; 0 = no cap
    max_order_notional: float = 0.0     # quote currency per order; 0 = no cap
    max_daily_notional: float = 0.0     # quote currency per UTC day; 0 = no cap
    symbols: tuple = ()                 # allow-list; empty = any symbol

    @classmethod
    def from_row(cls, row: dict) -> "Limits":
        import json
        syms = row.get("symbols") or ""
        try:
            parsed = json.loads(syms) if isinstance(syms, str) and syms.strip() else (syms or [])
        except ValueError:
            parsed = []
        return cls(float(row.get("max_order_amount") or 0), float(row.get("max_order_notional") or 0),
                   float(row.get("max_daily_notional") or 0),
                   tuple(str(s).upper() for s in parsed if str(s).strip()))


def check_order(limits: Limits, *, symbol: str, amount: float, price: Optional[float],
                day_notional_so_far: float) -> Optional[str]:
    """None when the order may go; otherwise the reason it is refused.

    `price` is the limit price, or a live quote for a market order, or None when
    neither is known. A notional cap with no price refuses rather than guesses."""
    if amount <= 0:
        return "amount must be positive"
    if limits.symbols and symbol.upper() not in limits.symbols:
        return f"{symbol} is not on this account's symbol allow-list"
    if limits.max_order_amount and amount > limits.max_order_amount:
        return f"amount {amount} exceeds the per-order cap {limits.max_order_amount}"
    needs_price = bool(limits.max_order_notional or limits.max_daily_notional)
    if needs_price and not (price and price > 0):
        return "no price to check the notional caps against: use a limit order or a quoted symbol"
    notional = amount * float(price or 0)
    if limits.max_order_notional and notional > limits.max_order_notional:
        return f"order notional {notional:.2f} exceeds the per-order cap {limits.max_order_notional}"
    if limits.max_daily_notional and day_notional_so_far + notional > limits.max_daily_notional:
        return (f"order would bring today's notional to {day_notional_so_far + notional:.2f}, "
                f"over the daily cap {limits.max_daily_notional}")
    return None


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def clean_client_order_id(value: str) -> str:
    v = "".join(ch for ch in (value or "") if ch.isalnum() or ch in "-_.:")[:64]
    return v
