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
    assert body.index('await _switch("memory.rerank") == "on"') < body.index("# Normalise fused scores")
    helper = src[src.index("async def _rerank_head("):src.index("async def _seek_cache_get(")]
    assert 'cap.get("raw") or cap.get("func")' in helper and "asyncio.wait_for(" in helper
    assert "_fc.RERANK_TOP" in helper and "if len(by_idx) == len(head):" in helper
    for outcome in ('"timeout"', '"error"', '"partial"', '"unavailable"'):
        assert outcome in helper
    assert FC.RERANK_TOP == 30


def test_the_switch_is_registered_off():
    from vera.minds import switchboard_core as SB
    f = SB.REGISTRY["memory.rerank"]
    assert f.modes == ("off", "on") and f.default == "off" and f.package == "FM-7.3"
