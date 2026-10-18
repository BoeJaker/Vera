"""Usage signals (context-and-ui CU-M.2): which memories a chat turn was given, and whether the reply drew on them.

Owner, 2026-10-04: useless session memories fade, useful ones stay. The half-life (CU-M.3) reads these counts.
"""
import asyncio


def _arun(coro):
    """Run a coroutine on a private loop. asyncio.run() would clear the thread's current loop when it ends, and a
    later test calling asyncio.get_event_loop() then fails (Python 3.11) - order-dependent failures elsewhere."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.fabric import memory_usage_core as u  # noqa: E402

DECISION = "Decided: the census calendar runs nightly at midnight using the weekday template"


def test_a_reply_that_carries_the_memory_used_it():
    used, score = u.judge("We agreed the census calendar runs nightly at midnight with a weekday template.", DECISION)
    assert used and score >= 0.35


def test_an_unrelated_reply_did_not():
    assert u.judge("Bitcoin is around 60k today.", DECISION) == (False, 0.0)


def test_six_words_in_a_row_count_even_in_a_long_reply():
    rec = "the gate does not check panel javascript so parse inline scripts yourself before you commit"
    reply = "Remember: " + "filler words about something else entirely " * 30 + \
            "the gate does not check panel javascript so parse them."
    assert u.judge(reply, rec)[0]


def test_a_passing_mention_is_not_use():
    assert not u.judge("The census ran.", DECISION)[0]


def test_empty_sides_are_not_use():
    assert u.judge("", DECISION) == (False, 0.0) and u.judge("anything at all here", "") == (False, 0.0)


def test_judge_all_once_per_id():
    out = u.judge_all("census calendar nightly midnight weekday template", [
        {"id": "a", "text": DECISION}, {"id": "a", "text": "other"}, {"id": "", "text": DECISION}, {"id": "b", "text": "x"}])
    assert [o["id"] for o in out] == ["a", "b"] and out[0]["used"] and not out[1]["used"]


@pytest.mark.parametrize("sid, reply, ok", [
    ("chat-123", "a reply", True), ("census-run-4", "a reply", False), ("probe-needs-x", "a reply", False),
    ("chat-123", "   ", False), ("", "a reply", False), ("intent-core-probe-1", "a reply", False),
])
def test_only_real_conversation_counts(sid, reply, ok):
    assert u.counts_toward_use(sid, reply) is ok


# -- record_turn_usage against a fake pool (the gate container imports the app as Vera.vera) -------------------------
def _hooks():
    try:
        return importlib.import_module("Vera.vera.fabric.memory_hooks")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")


class _Conn:
    def __init__(self, texts, faded=()):
        self.texts, self.rows, self.sql, self.faded = texts, [], [], set(faded)

    async def fetch(self, sql, ids):
        if sql.lstrip().startswith("UPDATE vera_memories"):     # the revive: only faded rows come back
            return [{"id": i} for i in ids if i in self.faded]
        return [{"id": i, "text": self.texts[i]} for i in ids if i in self.texts]

    async def execute(self, sql, *a):
        self.sql.append(sql)

    async def executemany(self, sql, rows):
        self.rows.extend(rows)

    async def fetchval(self, sql):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


def test_a_turn_counts_what_it_was_given_and_what_it_used(monkeypatch):
    H = _hooks()
    conn = _Conn({"page-1": DECISION, "page-2": "the canvas header holds the explode switch"})

    async def pool():
        return _Pool(conn)
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_USAGE_TABLE_READY", False)
    H._INJECTED.pop("chat-9", None)
    H._note_injected("chat-9", [{"id": "agent-1", "text": "the weekday template runs the census calendar nightly at midnight"}])
    out = _arun(H.record_turn_usage("chat-9", "The census calendar runs nightly at midnight on the weekday template.",
                                          ["page-1", "page-2"], [{"id": "deep-1", "text": "dream director narrates loops"}]))
    got = dict(conn.rows)
    assert set(got) == {"agent-1", "page-1", "page-2", "deep-1"}
    assert got["agent-1"] == 1 and got["page-1"] == 1 and got["page-2"] == 0 and got["deep-1"] == 0
    assert out == {"injected": 4, "used": 2, "revived": 0}
    assert any("CREATE TABLE IF NOT EXISTS vera_memory_usage" in q for q in conn.sql)
    assert "chat-9" not in H._INJECTED                          # a turn's slot is consumed


def test_a_probe_turn_is_not_counted(monkeypatch):
    H = _hooks()
    conn = _Conn({"page-1": DECISION})

    async def pool():
        return _Pool(conn)
    monkeypatch.setattr(H, "_usage_pool", pool)
    out = _arun(H.record_turn_usage("probe-needs-1", "census calendar nightly", ["page-1"], []))
    assert out == {"injected": 0, "used": 0} and conn.rows == []


def test_usage_top_before_any_turn_says_so(monkeypatch):
    H = _hooks()

    async def pool():
        return _Pool(_Conn({}))
    monkeypatch.setattr(H, "_usage_pool", pool)
    fn = getattr(H.memory_usage_top, "__wrapped__", H.memory_usage_top)
    out = _arun(fn(order="ignored"))
    assert out["rows"] == [] and "no turn" in out.get("note", "")


# -- wiring --------------------------------------------------------------------------------------------------------
ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_wiring():
    a, c, h = _src("vera/agents/agents.py"), _src("vera/chat/chat_panel.html"), _src("vera/fabric/memory_hooks.py")
    assert '_mh_u.record_turn_usage(session_id, "".join(_resp_head), _page_ids, _deep_items))' in a
    assert "return asyncio.create_task(\n                    _mh_u.record_turn_usage(" in a
    assert "ctx_ids:(ctxInj?_ctxInjIds(msg, a):[])," in c and "function _ctxInjIds(forMsg, agentRec){" in c
    assert ("    _note_injected(session_id, ranked[:limit])\n    if _reval is not None:\n"
            "        _reval.on_chat_recall(session_id, query, [it.get(\"id\") for it in ranked[:limit] if it.get(\"id\")])\n"
            "    return _chatmem_format(ranked[:limit])") in h
    assert 'out["items"] = [{"id": it.get("id"), "text": it.get("text", "")} for it in items if it.get("id")]' in h
    w = _src("vera/widgets/widget_registry.py")
    assert '"id": "memory:usage-used"' in w and '"id": "memory:usage-ignored"' in w


def test_a_faded_memory_the_reply_used_is_revived(monkeypatch):
    # owner, 2026-10-05: a strong, useful match must surface again and not be locked out for good
    H = _hooks()
    conn = _Conn({"page-1": DECISION}, faded={"page-1"})
    neo_updates = []

    class _Neo:
        async def update(self, rid, upd):
            neo_updates.append((rid, upd))

    class _Mem:
        _backends = {"neo4j": _Neo()}

    async def pool():
        return _Pool(conn)
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_memory", lambda: (_Mem(), None))
    monkeypatch.setattr(H, "_USAGE_TABLE_READY", True)
    out = _arun(H.record_turn_usage("chat-r", "The census calendar runs nightly at midnight on the weekday template.",
                                     ["page-1"], []))
    assert out["revived"] == 1 and neo_updates == [("page-1", {"archived": False})]
