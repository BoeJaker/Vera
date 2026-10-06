"""The retrieval eval set from seeks and reads (four-minds FM-7.0)."""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import retrieval_eval_core as REC  # noqa: E402
from vera.minds import ledger_core as LC  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_seek_is_a_valid_ranking_decision_without_its_text():
    d = REC.seek_decision("what did we decide about backups", ["r1", "r2", "r3"], origin="delegate")
    assert LC.check_decision(d) == [] and d["answer"] == ["r1", "r2", "r3"] and d["qtype"] == "ranking"
    assert "backups" not in str(d)            # the query is only hashed into the row


def test_a_read_credits_the_newest_seek_once_with_its_rank():
    t = [0.0]
    rs = REC.RecentSeeks(window_s=100, clock=lambda: t[0])
    rs.add("dec_a", ["x", "y", "z"])
    t[0] = 10
    rs.add("dec_b", ["y", "q"])
    assert rs.match("y") == ("dec_b", 1)
    assert rs.match("y") == ("dec_a", 2)         # the older seek can still be credited once
    assert rs.match("y") is None and rs.match("nope") is None
    t[0] = 500
    assert rs.match("z") is None                 # outside the window


def test_outcome_and_summary():
    o = REC.read_outcome("dec_" + "a" * 32, 2)
    assert LC.check_outcome(o) == [] and o["label"] == "used" and o["detail"]["rank"] == 2
    s = REC.summarize([{"used_ranks": [1]}, {"used_ranks": [3, 5]}, {"used_ranks": []}, {"used_ranks": [2]}])
    assert s == {"seeks": 4, "used": 3, "use_rate": 0.75, "hit@1": 0.333, "hit@3": 1.0, "mrr": round((1 + 1/3 + 1/2) / 3, 3)}


def test_hooks_never_raise_and_send_in_the_background(monkeypatch):
    from vera.fabric import retrieval_eval as RE
    sent = []

    async def fake_append(row):
        sent.append(row)

    async def fake_store(h, q):
        return None
    monkeypatch.setattr(RE, "_append", fake_append)
    monkeypatch.setattr(RE, "_store_query", fake_store)
    monkeypatch.setattr(RE, "RECENT", REC.RecentSeeks())

    async def go():
        RE.on_seek("q", ["a", "b"])
        RE.on_read("b")
        RE.on_read("never-seen")
        RE.on_seek("q", [])                      # nothing ranked: no decision
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(go())
    assert [r["schema"] for r in sent] == [LC.DECISION_SCHEMA, LC.OUTCOME_SCHEMA]
    assert sent[1]["detail"]["rank"] == 2 and sent[1]["decision_id"] == sent[0]["decision_id"]


def test_seek_and_read_are_wired():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    assert "_reval.on_seek(query, [r[\"id\"] for r in results if r.get(\"id\")])" in src
    assert "_reval.on_read(record_id)" in src
