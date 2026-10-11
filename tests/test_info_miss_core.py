"""Live info-seeking misses (plan SK-4.3): a question about the world answered without a lookup, and pushback. Pure."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.agents import info_miss_core as M                # noqa: E402


def test_a_reply_that_looked_something_up_is_not_a_miss():
    r = 'Let me check. [[cap:info.seek {"question": "latest on n8n"}]]'
    assert M.lookups_called(r) == ["info.seek"]
    assert M.classify("latest on n8n", r, wants_web=True, prefetched=False) is None
    assert M.classify("x", "[[suggest:cap web.research {\"query\": \"x\"}]]", wants_web=True, prefetched=True) is None


def test_the_8_oct_case_is_a_miss():
    # "latest AI/ML updates" answered from training with invented items
    m = M.classify("what are the latest AI/ML updates", "Here are the latest: GPT-5.5 launched...", wants_web=True,
                   prefetched=False)
    assert m["kind"] == "no_lookup"
    assert M.classify("latest AI news", "From the results above [1]...", wants_web=True,
                      prefetched=True)["kind"] == "snippets_only"


def test_nothing_is_a_miss_when_the_web_was_not_wanted_or_there_is_no_reply():
    assert M.classify("write a haiku", "Leaves fall...", wants_web=False, prefetched=False) is None
    assert M.classify("latest news", "", wants_web=True, prefetched=False) is None
    assert M.lookups_called("[[cap:cal.events.list {}]]") == []


@pytest.mark.parametrize("text", [
    "that's out of date", "That is wrong", "you made that up", "is that still true?", "this is old news",
    "are you sure about that?", "check that online", "that's not right",
])
def test_pushback(text):
    assert M.pushback(text)


@pytest.mark.parametrize("text", ["thanks, that's great", "what's the latest on MCP", "write it up", "ok"])
def test_not_pushback(text):
    assert M.pushback(text) is None


def test_the_turn_end_hook_is_wired():
    src = (Path(__file__).resolve().parents[1] / "vera" / "agents" / "agents.py").read_text(encoding="utf-8")
    fin = src[src.index("_record_chat_turn(_ph, agent.name, elapsed_ms, _resp_chars, session_id)"):][:200]
    assert '_note_info_miss(session_id, _bare_q, "".join(_resp_head), _web_auto)' in fin
    assert '"info.misses"' in src and "_INFO_MISS_KEY" in src
