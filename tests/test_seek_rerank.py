"""memory.seek's cross-encoder rerank (four-minds FM-7.3): the head's order comes from nlp.rerank, the head stays
above the rest, any failure keeps the fused order; behind memory.rerank, off by default."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import fusion_core as FC  # noqa: E402

pytestmark = pytest.mark.critical


def test_the_cross_encoder_orders_the_head_above_the_rest():
    lifted = FC.rerank_lift([0.1, 3.0, -2.0, 1.5], top=0.8)
    order = sorted(range(4), key=lambda i: -lifted[i])
    assert order == [1, 3, 0, 2]                         # the cross-encoder's order
    assert min(lifted) > 0.8 and max(lifted) == 1.6      # every head item above every other candidate (<= top)
    assert FC.rerank_lift([], 1.0) == [] and FC.rerank_lift([1.0, 2.0], 0)[1] == 2.0


def test_seek_reranks_before_it_normalises_and_keeps_the_fused_order_on_failure():
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    body = src[src.index("async def cap_memory_seek("):src.index("async def cap_memory_read(")]
    assert body.index('if _rerank_wanted(_rr_mode):') < body.index("# Normalise fused scores")
    # where nobody waits (not_chat), the cross-encoder gets the longer background bound (12 Oct: 2.7-2.9 s at night)
    assert '_RERANK_BACKGROUND_TIMEOUT_S if _rr_mode == "not_chat" else _RERANK_TIMEOUT_S' in body
    assert "timeout_s or _RERANK_TIMEOUT_S" in src
    helper = src[src.index("async def _rerank_head("):src.index("async def _seek_cache_get(")]
    assert 'cap.get("raw") or cap.get("func")' in helper and "asyncio.wait_for(" in helper
    assert "_fc.RERANK_TOP" in helper and "if len(by_idx) == len(head):" in helper
    for outcome in ('"timeout"', '"error"', '"partial"', '"unavailable"'):
        assert outcome in helper
    assert FC.RERANK_TOP == 30


def test_the_switch_is_registered_off():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["memory.rerank"]
    assert f.modes == ("off", "on", "not_chat") and f.default == "off" and f.package == "FM-7.3"


def test_not_chat_reranks_everywhere_but_a_live_chat_turns_own_seek(monkeypatch):
    # owner 12 Oct: rerank where nobody waits on the reply (research, discovery, loops, background), not in live chat
    import ast
    import contextvars
    import types
    src = open(os.path.join(ROOT, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"sys": sys}
    for fn in ("_live_chat_seek", "_rerank_wanted"):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn)
        exec(compile(ast.get_source_segment(src, node), "memory_retrieval.py", "exec"), ns)
    orch = types.SimpleNamespace(RUNNING_CAPS=contextvars.ContextVar("rc", default=()),
                                 CALLER_KIND=contextvars.ContextVar("ck", default=""))
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", orch)
    want = ns["_rerank_wanted"]

    def case(caps, kind=""):
        ctx = contextvars.copy_context()
        def run():
            orch.RUNNING_CAPS.set(caps)
            orch.CALLER_KIND.set(kind)
            return want("not_chat")
        return ctx.run(run)

    assert case(("memory.seek",)) is False                              # the browser's chat tool call
    assert case(("memory.seek",), "claude") is True                     # an MCP agent
    assert case(("research.run", "memory.seek")) is True                # research
    assert case(("discovery.topic", "memory.seek")) is True             # discovery
    assert case(("dag.agent_loop_v7", "memory.seek")) is True           # a loop
    assert case(()) is True                                             # no capability context (a job)
    assert want("on") is True and want("off") is False
