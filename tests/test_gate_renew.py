"""GPU-gate renewable-lease heartbeat — the 2026-08-18 wedge fix.

A generation holds a gate slot; if it is cancelled/crashed mid-flight and its
heartbeat stops, the slot must expire QUICKLY (short lease TTL) instead of
wedging the node for the 30-min hard TTL and blocking every later loop-planner
call. A LIVE generation renews the short lease well before expiry via the
heartbeat, so it never loses its slot. These tests pin the pure primitives:

  - lease_ttl_ms is short, and comfortably longer than the renew interval (so a
    live slot survives a missed beat);
  - renew is owner-fenced (only the holder can refresh the TTL) — a peer that
    took over an expired slot is never resurrected under the original owner.

`tests/conftest.py` marks this module `critical` (added there) so it joins the
merge gate. Pure + deterministic — a tiny fake Redis, no real I/O.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import ollama_gate as gate  # noqa: E402


def test_lease_ttl_is_short_and_longer_than_renew_interval():
    ttl_s = gate.lease_ttl_ms() / 1000.0
    interval = gate.renew_interval_s()
    assert ttl_s <= 300, "renewable lease TTL must be short (self-heals fast)"
    assert ttl_s < gate.ttl_ms() / 1000.0, "renewable TTL must be shorter than the hard fallback"
    # Two missed beats must not expire a live slot.
    assert interval * 2 < ttl_s, "renew interval must be < half the lease TTL"


class _FakeRedis:
    """Just enough Redis to exercise the owner-fenced Lua for renew/release."""
    def __init__(self):
        self.store = {}   # key -> value
        self.pttl = {}    # key -> ttl ms

    async def set(self, k, v, nx=False, px=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        if px is not None:
            self.pttl[k] = px
        return True

    async def eval(self, lua, numkeys, key, arg1, arg2=None):
        # Emulate the owner-fenced RENEW/RELEASE: act only if value == arg1.
        if self.store.get(key) != arg1:
            return 0
        if "pexpire" in lua:
            self.pttl[key] = int(arg2)
            return 1
        if "del" in lua:
            self.store.pop(key, None)
            self.pttl.pop(key, None)
            return 1
        return 0


def test_renew_refreshes_ttl_for_owner():
    r = _FakeRedis()
    lease = {"key": "vera:ollama:gate:gpu:slot:0", "owner": "hostA:1:abc"}
    asyncio.run(r.set(lease["key"], lease["owner"], nx=True, px=1000))
    ok = asyncio.run(gate.renew(r, lease, 90000))
    assert ok is True
    assert r.pttl[lease["key"]] == 90000


def test_renew_is_owner_fenced():
    r = _FakeRedis()
    key = "vera:ollama:gate:gpu:slot:0"
    # A DIFFERENT owner holds the slot now (ours expired and got re-taken).
    asyncio.run(r.set(key, "hostB:2:xyz", nx=True, px=90000))
    stale_lease = {"key": key, "owner": "hostA:1:abc"}
    ok = asyncio.run(gate.renew(r, stale_lease, 90000))
    assert ok is False, "must NOT renew a slot owned by someone else"
    assert r.store[key] == "hostB:2:xyz"   # peer's slot untouched


def test_renew_none_inputs_safe():
    assert asyncio.run(gate.renew(None, {"key": "k", "owner": "o"}, 1000)) is False
    assert asyncio.run(gate.renew(_FakeRedis(), None, 1000)) is False
