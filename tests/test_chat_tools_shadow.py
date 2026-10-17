"""The chat tool shortlist in shadow (four-minds FM-3.2/3.3): the page's offered list parsed from the message, the
used capabilities from the reply, recall@k for both lists; behind tools.shortlist.chat, legacy by default."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import chat_tools_core as CT  # noqa: E402

pytestmark = pytest.mark.critical

MSG = ("what's the bitcoin price?\n\n[INTEGRATED CAPABILITY MODE — 3 caps available]\n"
       "You can invoke these capabilities directly ONLY if they help answer the user's request.\n\n"
       "  markets.quotes(symbols*:string) — Latest quotes\n"
       "  info.seek(question*:string) — Find information\n"
       "  web.search(query*:string, limit:integer=5) — Search the web\n\n"
       "Formats:\n  [[cap:cap.name {\"param\":value}]]              — run immediately\n")


def test_the_page_list_is_read_from_the_manifest_only():
    assert CT.offered(MSG) == ["markets.quotes", "info.seek", "web.search"]
    assert CT.offered("hello there") == [] and CT.offered("") == []
    assert "cap.name" not in CT.offered(MSG)            # the Formats example is not a capability


def test_used_capabilities_come_from_run_and_suggest_markers():
    reply = ('Let me check. [[cap:markets.quotes {"symbols":"BTC"}]] and maybe '
             '[[suggest:cap web.search {"query":"btc"}]] [[cap:markets.quotes {}]]')
    assert CT.used(reply) == ["markets.quotes", "web.search"] and CT.used("no tools") == []


def test_recall_for_both_lists():
    rows = [CT.row("s1", "q", ["a.x", "b.y"], ["c.z", "a.x"], ["a.x"]),
            CT.row("s2", "q", ["a.x"], ["d.w", "e.v"], ["d.w", "f.u"]),
            CT.row("s3", "q", ["a.x"], ["a.x"], [])]
    s = CT.summarize(iter(rows))
    assert s["turns"] == 3 and s["turns_with_use"] == 2
    assert s["client"]["recall@28"] == round(1 / 3, 3) and s["server"]["recall@28"] == round(2 / 3, 3)
    assert s["client"]["median_position"] == 1 and s["server"]["median_position"] == 1.5
    assert s["unoffered_rate"] == round(2 / 3, 3)
    assert CT.summarize([]) == {"turns": 0, "turns_with_use": 0}


def test_the_chat_stream_records_the_shadow_after_the_reply():
    src = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    # recorded in shadow and, since 17 Oct, in 'on' too (the safe shortlist offered to the model)
    assert 'await _switch_mode("tools.shortlist.chat", body.get("minds"), default="legacy") in ("shadow", "on")' in src
    assert "_sl_task = asyncio.create_task(_sl_fn(text=_bare_q, k=28))" in src
    assert "await _sl_task" not in src
    i = src.index("_record_chat_turn(_ph, agent.name, elapsed_ms, _resp_chars, session_id)")
    assert "asyncio.ensure_future(_record_tools_shadow(" in src[i:i + 400]
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["tools.shortlist.chat"]
    assert f.modes == ("legacy", "shadow", "on") and f.default == "legacy" and f.package == "FM-3.3"
