"""Chat turns as Loop Lab records, in their own list (four-minds FM-0.3 slice 2, reviewed with compute-roles)."""
import asyncio
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.evolve import result_ingest_core as ric  # noqa: E402

pytestmark = pytest.mark.critical

TURN = {"type": "chat.turn", "turn_id": "t1", "agent": "vera", "kind": "triggered", "ttft_ms": 1200,
        "elapsed_ms": 3400, "chars": 420, "ok": True, "phases": [], "session_id": "chat-1"}


def test_builder_shapes_a_chat_record():
    compact, detail = ric.run_record_from_chat_turn(TURN, ingested_at="2026-10-06T20:00:00Z")
    assert compact["run_id"] == "chat:t1" and compact["task"] == "chat:vera" and compact["source"] == "chat"
    assert compact["ttft_ms"] == 1200 and compact["ok"] and compact["error"] == "" and compact["elapsed_s"] == 3.4
    assert detail["ingested_at"] == "2026-10-06T20:00:00Z"
    no_token = dict(TURN, ttft_ms=None, ok=False)
    assert ric.run_record_from_chat_turn(no_token)[0]["error"] == "no-token"
    assert ric.run_record_from_chat_turn({"type": "other"}) is None


class FakeRedis:
    def __init__(self):
        self.lists, self.kv = {}, {}

    async def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)

    async def ltrim(self, k, a, b):
        self.lists[k] = self.lists.get(k, [])[a:b + 1]

    async def lrange(self, k, a, b):
        return self.lists.get(k, [])[a:b + 1]

    async def set(self, k, v):
        self.kv[k] = v

    async def expire(self, k, s):
        return True


def test_ingest_uses_its_own_list_and_cap(monkeypatch):
    from vera.evolve import task_history_capabilities as TH
    r = FakeRedis()
    ev = types.SimpleNamespace(_redis=lambda: r, KEY_CHAT_TURNS="vera:evolve:chat_turns",
                               KEY_CHAT_TURN="vera:evolve:chat_turn:", CHAT_TURNS_CAP=2,
                               KEY_RUNS="vera:evolve:runs")
    monkeypatch.setattr(TH, "_mods", lambda: (None, ev))
    fn = getattr(TH.cap_evolve_chat_ingest, "__wrapped__", TH.cap_evolve_chat_ingest)
    res = asyncio.run(fn(turns=[TURN, dict(TURN, turn_id="t2"), dict(TURN, turn_id="t3"), {"type": "x"}]))
    assert res == {"ok": True, "ingested": 3, "skipped": 1}
    kept = [json.loads(x)["run_id"] for x in r.lists["vera:evolve:chat_turns"]]
    assert kept == ["chat:t3", "chat:t2"] and "vera:evolve:runs" not in r.lists
    assert "vera:evolve:chat_turn:chat:t1" in r.kv


def test_census_ingest_still_refuses_other_sources():
    src = open(os.path.join(ROOT, "vera", "evolve", "task_history_capabilities.py"), encoding="utf-8").read()
    assert 'if str(source or "census") != "census":' in src


def test_runs_reads_chat_only_when_asked():
    src = open(os.path.join(ROOT, "vera", "evolve", "evolve_capabilities.py"), encoding="utf-8").read()
    assert 'if src == "chat":' in src and 'if src == "all":' in src


def test_the_chat_post_is_bounded_and_backgrounded():
    src = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    seg = src[src.index("def _record_chat_turn("):src.index("def cap_chat_turns_stats(")]
    assert 'asyncio.wait_for(_call_registered_cap("evolve.chat.ingest", turn=rec)' in seg
    assert "timeout=2.0" in seg and "create_task" in seg and "await " not in seg.split("def _record_chat_turn(")[1].split("@capability")[0]
