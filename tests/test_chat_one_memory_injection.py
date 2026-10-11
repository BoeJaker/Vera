"""One memory injection per chat turn (owner, 2026-10-11): an agent with memory_inject gets memory from the server's
two-tier recall, so the page leaves its own memory nodes out of "[Retrieved Context]" (fabric, web and notes stay)."""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.critical


def _page():
    return open(os.path.join(ROOT, "vera", "chat", "chat_panel.html"), encoding="utf-8").read()


def test_the_page_skips_its_memory_nodes_for_a_server_injecting_agent():
    src = _page()
    assert "const _CTX_MEM_SRCS=new Set(['memory','vector','both','graph']);" in src
    assert "function _ctxSkipMem(agentRec){ return !!(agentRec&&agentRec.memory_inject); }" in src
    body = src[src.index("function buildCtxInj(forMsg, agentRec){"):src.index("function _ctxInjIds(")]
    assert ".filter(n=>!(_skipMem&&_CTX_MEM_SRCS.has(n.source)))" in body
    assert body.index("_CTX_MEM_SRCS.has(n.source)") < body.index(".slice(0,k)")     # before the top-k cut


def test_the_send_path_passes_the_agent_and_the_usage_ids_follow():
    src = _page()
    assert "ctxInj=buildCtxInj(msg, a);" in src and "ctx_ids:(ctxInj?_ctxInjIds(msg, a):[])," in src
    ids = src[src.index("function _ctxInjIds(forMsg, agentRec){"):]
    assert "if(_ctxSkipMem(agentRec)) return [];" in ids[:400]


def test_the_server_side_injection_is_the_agents_memory_inject():
    src = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()
    assert "getattr(agent, 'memory_inject', False) and session_id" in src
    assert "get_agent_memory_context_v2(" in src
