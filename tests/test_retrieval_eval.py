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


class _FakeRedis:
    def __init__(self):
        self.lst, self.kv = [], {}

    async def lpush(self, k, v):
        self.lst.insert(0, v)

    async def ltrim(self, k, a, b):
        self.lst = self.lst[a:b + 1]

    async def lrange(self, k, a, b):
        return self.lst[a:b + 1]

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True


def test_hooks_never_raise_and_send_in_the_background(monkeypatch):
    import types as _types
    from vera.fabric import retrieval_eval as RE
    sent = []

    async def fake_append(row):
        sent.append(row)

    async def fake_store(h, q):
        return None
    monkeypatch.setattr(RE, "_append", fake_append)
    monkeypatch.setattr(RE, "_store_query", fake_store)
    monkeypatch.setattr(RE, "RECENT", REC.RecentSeeks())
    _orch_ns = _types.SimpleNamespace(REDIS=_FakeRedis(), CALLER_KIND=None)   # ONE store for seek and read
    monkeypatch.setattr(RE, "_orch", lambda: _orch_ns)

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



def test_pick_seeks_newest_first_within_the_window():
    entries = [{"did": "b", "ids": ["y", "q"], "ts": 100.0}, {"did": "a", "ids": ["x", "y"], "ts": 90.0},
               {"did": "old", "ids": ["y"], "ts": 0.0}]
    assert REC.pick_seeks(entries, "y", now=110.0, window_s=50) == [("b", 1), ("a", 2)]
    assert REC.pick_seeks(entries, "none", now=110.0) == []


def test_a_read_in_another_process_is_credited_once(monkeypatch):
    import types as _types
    from vera.fabric import retrieval_eval as RE
    sent, red = [], _FakeRedis()

    async def fake_append(row):
        sent.append(row)
    monkeypatch.setattr(RE, "_append", fake_append)
    monkeypatch.setattr(RE, "_orch", lambda: _types.SimpleNamespace(REDIS=red))
    monkeypatch.setattr(RE, "RECENT", REC.RecentSeeks())       # an empty local list: another process
    asyncio.run(RE._remember("dec_x", ["a", "b"]))
    asyncio.run(RE._credit("b"))
    asyncio.run(RE._credit("b"))                               # second read: no second credit
    assert [(o["decision_id"], o["detail"]["rank"]) for o in sent] == [("dec_x", 2)]


def test_a_chat_recall_is_a_ranking_decision_and_used_memories_score_at_their_rank():
    d = REC.chat_recall_decision("where did we get to with the lan scanner", ["a", "b", "c"], session_id="s1")
    assert LC.check_decision(d) == [] and d["question_id"] == REC.CHAT_Q and d["answer"] == ["a", "b", "c"]
    assert "scanner" not in str(d)
    outs = REC.chat_used_outcomes(d["decision_id"], ["a", "b", "c"], ["c", "zz", "a", "a"])
    assert [(o["source"], o["detail"]["rank"]) for o in outs] == [("chat.reply", 1), ("chat.reply", 3)]
    assert REC.chat_used_outcomes(d["decision_id"], ["a"], []) == []


def test_chat_turns_record_only_when_their_use_is_judged(monkeypatch):
    from vera.fabric import retrieval_eval as RE
    sent = []

    async def fake_append(row):
        sent.append(row)

    async def fake_store(h, q):
        return None
    monkeypatch.setattr(RE, "_append", fake_append)
    monkeypatch.setattr(RE, "_store_query", fake_store)
    monkeypatch.setattr(RE, "_CHAT", {})

    async def go():
        RE.on_chat_recall("s1", "what is the proxmox layout", ["r1", "r2"])
        assert sent == []                                  # nothing until the turn is judged
        RE.on_chat_used("s1", RE.take_chat("s1"), ["r2"])
        RE.on_chat_used("s1", RE.take_chat("s1"), ["r2"])  # already taken: nothing more
        RE.on_chat_recall("s2", "q", ["x"])
        RE.take_chat("s2")                                 # a turn that did not count: dropped
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert [r["schema"] for r in sent] == [LC.DECISION_SCHEMA, LC.OUTCOME_SCHEMA]
    assert sent[0]["question_id"] == REC.CHAT_Q and sent[1]["source"] == "chat.reply"
    assert sent[1]["detail"]["rank"] == 2 and sent[1]["decision_id"] == sent[0]["decision_id"]
    assert RE._CHAT == {}


def test_memory_hooks_wire_the_chat_eval():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_hooks.py"), encoding="utf-8").read()
    assert "_reval.on_chat_recall(session_id, query," in src
    assert "_chat_pending = _reval.take_chat(session_id)" in src
    assert "_reval.on_chat_used(session_id, _chat_pending, used_ids)" in src
