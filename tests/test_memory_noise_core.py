"""Noise ranks last in recall (owner 2026-10-04: noisy records stay in the fabric but rank last). The cases are taken
from a 300-record random sample of prod's default recall pool, 2026-10-06 (shared planning E29)."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.fabric import memory_noise_core as N  # noqa: E402

NOISE = [
    ("event", "Remote IDE instance created: DESKTOP-X · Vera (vscode-client)", "status"),
    ("event", "Session started with agent assistant", "status"),
    ("event", "[inspect] snapshot 20260805-014459-areas — 3788 files", "status"),
    ("event", "[http.get]", "status"),
    ("event", "Cap: browser.search: {\"query\": \"use the caps\", \"max_results\": 5}", "status"),
    ("event", "[CALL chat.stream] agent_name=assistant, model=x", "status"),
    ("event", "[browser.search] news: To get the current Bash user, run whoami", "status"),
    ("dag", "DAG(chat_dag): exec.bash.run", "status"),
    ("dag", "Step 1: netscan.lan.scan → hosts (complete)", "status"),
    ("observation", "[remote/claude] DESKTOP-X · Vera: create a test file", "status"),
    ("observation", "File: pokedex.py (1903b) by pokedex_fixer", "status"),
    ("event", " [plan #1] {\"thought\": \"First, search the registry\"}", "status"),
    ("message", "[agentic loop v7] run ended without a final answer: cancelled by user", "status"),
    ("message", "The capability exec.bash.run ran and returned: {\"ok\":true} Answer the user's question", "scaffold"),
    ("message", "The capability \"web.scout\" does not exist in the registry. You MUST only use", "scaffold"),
    ("message", "Goal: scan 192.168.0.0/24\nExecution history so far:\nStep 1: none", "scaffold"),
    ("message", "Goal: an indicator of if maxhodl.com is up\nStyle preference: dashboard", "scaffold"),
    ("message", "hello [INTEGRATED CAPABILITY MODE — 6 caps available] You can invoke these", "scaffold"),
    ("message", "so tell me about mempalace [Capability execution: ENABLED. tool_mode=plan]", "scaffold"),
    ("event", "## Result — lead with the direct answer/outcome of the goal: the substance, not the process.", "scaffold"),
    ("message", "thanks [Retrieved Context] [memory] hello [Retrieved Context] [memory] hello", "injected-context"),
    ("observation", "{\"ok\": true, \"query\": \"could you use the\", \"results\": []}", "tool-json"),
    ("observation", "{\"image_b64\": \"iVBORw0KGgo\"}", "tool-json"),
    ("dream", "# Project Reflection — no signal\n\nAll sensors returned no usable data.", "empty-report"),
    ("dream", "*Dream cycle project_compose produced no synthesis.*", "empty-report"),
    ("message", "[[loop: create a pomodoro app]]", "directive"),
    ("message", "hello", "near-empty"),
    ("message", "[Agent error: ]", "near-empty"),
    ("event", "Bash User Query", "title"),
]

KEEP = [
    ("message", "get trending topics from reddit and create full podcast scripts based on the ideas you find"),
    ("message", "The current bash user is **`boejaker`**."),
    ("message", "can you recall my name"),
    ("event", "## Result The total disk usage of `/workspace` is **15M**. The five largest files are"),
    ("event", "Query `syslog_errors` to confirm database connection and schema integrity"),
    ("dream", "# Morning News Brief - April 27, 2026\n## Tim Cook's Apple and Electric Aspirations"),
    ("dream_report", "# Source Code Review — Continue Snapshot `x` · 1 file(s) reviewed · 3 issue(s)"),
    ("dream", "# Executive Action Brief\n\nBased on current data the ingest is healthy.\n" + "x " * 80
     + "earlier the sensor said no signal"),
    ("message", "Research completed. Result: ## Summary Based on a comprehensive analysis"),
]


@pytest.mark.parametrize("rtype,text,why", NOISE)
def test_noise_is_named(rtype, text, why):
    assert why in N.noise_reasons({"record_type": rtype, "text": text})


@pytest.mark.parametrize("rtype,text", KEEP)
def test_real_memories_are_not_noise(rtype, text):
    assert N.noise_reasons({"record_type": rtype, "text": text}) == []


class _Rec:
    def __init__(self, rid, text, importance=0.5, rtype="message"):
        self.id, self.text, self.importance, self.record_type = rid, text, importance, rtype

    def to_dict(self):
        return {"id": self.id, "text": self.text, "record_type": self.record_type}


def test_recall_ranks_noise_after_every_real_memory():
    try:
        M = importlib.import_module("Vera.vera.fabric.memory")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")
    pairs = [(_Rec("n1", "Session started with agent assistant"), 0.99),
             (_Rec("c1", "the user prefers dark themes in every panel"), 0.30),
             (_Rec("n2", "hello [Retrieved Context] [memory] dark themes"), 0.95),
             (_Rec("c2", "dark themes were applied to the markets panel"), 0.40)]
    hits = M.HybridMemoryStore._rank_pairs(pairs, 10)
    assert [h["record"]["id"] for h in hits] == ["c2", "c1", "n1", "n2"]
    assert hits[2]["noise"] == ["status"] and "noise" not in hits[0]
    assert [h["record"]["id"] for h in M.HybridMemoryStore._rank_pairs(pairs, 2)] == ["c2", "c1"]


def _arun(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Conn:
    ROWS = [{"id": "a", "record_type": "event", "text": "Session started with agent assistant", "marked": False},
            {"id": "b", "record_type": "message", "text": "the user prefers dark themes", "marked": True},
            {"id": "c", "record_type": "message", "text": "the user prefers light themes", "marked": False},
            {"id": "d", "record_type": "message", "text": "hello", "marked": True}]

    def __init__(self):
        self.marked, self.unmarked = {}, []

    async def fetch(self, sql, *a):
        if "AS marked" in sql:
            return [r for r in self.ROWS if r["id"] > a[0]]
        if "jsonb_build_object('noise'" in sql:
            self.marked.update(zip(a[0], a[1]))
            return [{"id": i} for i in a[0]]
        if "metadata - 'noise'" in sql:
            self.unmarked += list(a[0])
            return [{"id": i} for i in a[0]]
        return []


class _Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        c = self.c

        class _Ctx:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return _Ctx()


def test_marking_the_stored_noise_counts_first_then_marks_and_clears(monkeypatch):
    import json
    try:
        H = importlib.import_module("Vera.vera.fabric.memory_hooks")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")
    conn, kv = _Conn(), {}

    async def pool():
        return _Pool(conn)

    class _R:
        async def get(self, k):
            return kv.get(k)

        async def set(self, k, v):
            kv[k] = v
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_redis", lambda: _R())
    monkeypatch.setattr(H, "_memory", lambda: (None, None))
    monkeypatch.setattr(H, "_NOISE_RUN_TASK", None)
    fn = getattr(H.memory_noise_mark, "__wrapped__", H.memory_noise_mark)
    dry = _arun(fn(trace_id="t-1"))                           # the dispatcher always passes trace_id
    assert dry["dry"] is True and dry["would_mark"] == 1 and dry["would_unmark"] == 1 and conn.marked == {}
    out = _arun(H._noise_apply())
    assert json.loads(conn.marked["a"]) == ["status"]          # newly noise: marked with why
    assert conn.unmarked == ["b"]                              # a mark the rules no longer give is cleared
    assert out["marked"] == 1 and out["unmarked"] == 1 and json.loads(kv[H._NOISE_RUN_KEY])["scanned"] == 4

@pytest.mark.parametrize("sent,said", [
    ("can you run some backtests on bitcoin\n\n[Retrieved Context]\n[memory] tell me about x",
     "can you run some backtests on bitcoin"),
    ("can you recall my name [INTEGRATED CAPABILITY MODE — 3 caps available] You can invoke", "can you recall my name"),
    ("thanks [STUDIO — visual & execution tools enabled by the user] - Pictures", "thanks"),
    ("so tell me about mempalace [Capability execution: ENABLED. tool_mode=plan]", "so tell me about mempalace"),
    ("plain question with no block", "plain question with no block"),
    ("[Retrieved Context]\n[memory] only a block", "[Retrieved Context]\n[memory] only a block"),
])
def test_only_what_the_user_typed_is_kept(sent, said):
    assert N.user_said(sent) == said

def test_memory_is_searched_with_what_the_user_typed():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vera", "fabric", "memory_hooks.py"), encoding="utf-8").read()
    head = src[src.index("async def get_agent_memory_context_v2("):]
    head = head[:head.index("MEMORY, _ = _memory()")]
    assert "query = _noise_core.user_said(query)" in head          # before either tier searches