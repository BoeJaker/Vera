"""Chat replay capture (four-minds FM-0.3, owner yes 17 Oct): kept only with the switch on, redacted, never
in the way of the turn, and into the corpus only case by case."""
import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import chat_replay_core as CR  # noqa: E402
from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def test_the_switch_is_registered_off_by_default_and_not_for_census_arms():
    f = SB.REGISTRY["chat.replay_capture"]
    assert f.default == "off" and f.modes == ("off", "on") and f.census is False


def test_only_on_and_never_the_corpus_runs_own_sessions():
    assert CR.wanted("on", "abc") is True
    assert CR.wanted("off", "abc") is False
    assert CR.wanted("on", "chat-corpus-12") is False


def test_a_row_is_the_exact_body_with_credentials_redacted():
    body = {"message": "my api_key=sk-123 please", "agent_name": "vera",
            "history": json.dumps([{"role": "user", "content": "token: abc"}]), "system_prefix": "You are Vera",
            "nested": {"x": ["password=hunter2"]}}

    def cred(s):
        return s.replace("sk-123", "<redacted>").replace("hunter2", "<redacted>")
    r = CR.row(body, "s1", cred)
    assert r["body"]["message"] == "my api_key=<redacted> please"
    assert r["body"]["nested"]["x"] == ["password=<redacted>"]
    assert r["body"]["system_prefix"] == "You are Vera" and r["agent"] == "vera"
    assert r["message_chars"] == len(body["message"]) and not r["truncated"] and len(r["digest"]) == 16
    assert body["message"] == "my api_key=sk-123 please"          # the caller's body is not changed


def test_a_huge_body_is_cut_and_says_so():
    r = CR.row({"message": "x" * (CR.MAX_BODY_CHARS + 10)}, "s", None)
    assert r["truncated"] and len(r["body"]["_truncated_json"]) == CR.MAX_BODY_CHARS


def test_preview_and_corpus_case():
    assert len(CR.preview("a " * 500)) == CR.PREVIEW_CHARS
    c = CR.corpus_case(7, {"message": "hi", "agent_name": "vera", "history": "[]", "cap_hint": "x"}, "good")
    assert c["source"] == "replay:7" and c["request"]["cap_hint"] == "x" and c["note"] == "good"


def test_the_chat_endpoint_hands_the_body_over_without_awaiting():
    src = open(os.path.join(ROOT, "vera/agents/agents.py"), encoding="utf-8").read()
    ep = src[src.index('@APP.post("/agents/chat/stream")'):]
    ep = ep[:ep.index("agent = None")]
    assert "_replay.capture(body, session_id)" in ep and "await _replay" not in ep


# ── the module ──

class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, sql, *args):
        if sql.startswith("INSERT"):
            self.rows.append({"session_id": args[0], "agent": args[1], "body": json.loads(args[4])})
        return "INSERT 0 1"


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return _Conn(pool.rows)

            async def __aexit__(self, *a):
                return False
        return _C()


@pytest.mark.parametrize("mode,session,kept", [("on", "s1", 1), ("off", "s1", 0), ("on", "chat-corpus-3", 0)])
def test_capture_writes_only_when_wanted(monkeypatch, mode, session, kept):
    from vera.minds import chat_replay as M
    rows = []
    monkeypatch.setattr(M._orch, "PG_POOL", _Pool(rows), raising=False)
    monkeypatch.setattr(M, "_DDL_DONE", True)
    monkeypatch.setattr(M, "_MODE", {"mode": "off", "at": 0.0})

    async def eff(**kw):
        return {"modes": {"chat.replay_capture": mode}}
    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "minds.effective", {"func": eff})

    async def go():
        M.capture({"message": "hello", "agent_name": "vera"}, session)
        await asyncio.gather(*list(M._PENDING))
    asyncio.run(go())
    assert len(rows) == kept
    if kept:
        assert rows[0]["body"]["message"] == "hello" and rows[0]["body"]["_meta"]["truncated"] is False


def test_capture_never_raises_on_bad_input():
    from vera.minds import chat_replay as M
    M.capture(None, "s")                     # no running loop, no body: silently nothing
    M.capture({"message": "x"}, "s")


def test_purge_needs_a_target():
    from vera.minds import chat_replay as M
    out = asyncio.run(M.cap_chat_replay_purge())
    assert out["ok"] is False
