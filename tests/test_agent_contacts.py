"""Agent contacts: who to message about a board item (session names like vera-62)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.board import contacts_core as cc  # noqa: E402

pytestmark = pytest.mark.critical


def test_make_contact_checks_fields():
    c = cc.make_contact("claude:4dfcd766", "vera-62", "claude-code", ["four-minds", " ", "recon-fixes"])
    assert c["session_name"] == "vera-62" and c["units"] == ["four-minds", "recon-fixes"]
    assert cc.reach_line(c) == "Reach: vera-62 (claude-code, claude:4dfcd766)"
    for bad in (("", "vera-62"), ("claude:x", "has space"), ("claude:x", "")):
        with pytest.raises(ValueError):
            cc.make_contact(*bad)
    with pytest.raises(ValueError):
        cc.make_contact("claude:x", "vera-1", tool="carrier-pigeon")


def test_merge_accumulates_units_and_keeps_the_note():
    old = cc.make_contact("claude:a", "vera-1", units=["u1"], note="hi")
    new = cc.make_contact("claude:a", "vera-1", units=["u2"])
    m = cc.merge(old, new)
    assert m["units"] == ["u1", "u2"] and m["note"] == "hi"


def test_stale_after_a_week():
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    c = cc.make_contact("claude:a", "vera-1", last_seen=(now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert cc.stale(c, now) and not cc.stale(dict(c, last_seen="2026-10-04T00:00:00Z"), now)


def test_identities_on_item_owner_first_then_commenters():
    item = {"agent": "claude:owner", "labels": ["agent:codex:run1", "bug"],
            "comments": [{"from": "claude:c1"}, {"from": "claude:owner"}, {"frm": "loop-lab-scheduler"}]}
    assert cc.identities_on_item(item) == ["claude:owner", "codex:run1", "claude:c1", "loop-lab-scheduler"]
    reg = {"claude:c1": cc.make_contact("claude:c1", "vera-d2")}
    assert cc.contacts_for(cc.identities_on_item(item), reg)["claude:c1"]["session_name"] == "vera-d2"


# ── the board path with a fake Redis ────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.h = {}

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def xadd(self, *a, **k):      # the orchestrator's own event stream
        return None


@pytest.fixture
def board(monkeypatch):
    from vera.board import board_capabilities as B
    monkeypatch.setattr(B._orch, "REDIS", FakeRedis(), raising=False)
    return B


def test_contact_set_get_and_filters(board):
    r = asyncio.run(board.cap_agents_contact_set(agent="claude:4dfcd766", session_name="vera-62",
                                                 units="four-minds,recon-fixes"))
    assert r["ok"] and r["reach"].startswith("Reach: vera-62")
    asyncio.run(board.cap_agents_contact_set(agent="codex:run9", session_name="codex-run9", tool="codex",
                                             units="agent-ops"))
    assert [c["agent"] for c in asyncio.run(board.cap_agents_contact_get(unit="four-minds"))["contacts"]] \
        == ["claude:4dfcd766"]
    assert asyncio.run(board.cap_agents_contact_get(session_name="codex-run9"))["contacts"][0]["tool"] == "codex"
    assert asyncio.run(board.cap_agents_contact_get())["count"] == 2
    bad = asyncio.run(board.cap_agents_contact_set(agent="claude:x", session_name="no spaces please"))
    assert not bad["ok"]
