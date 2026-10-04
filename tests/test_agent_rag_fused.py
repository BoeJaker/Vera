"""Agent RAG queries its datasets at once and fuses them by rank (RF-57).

agents.py has app-level side effects, so agent_rag_retrieve is lifted out with ast and run
against a fake fabric.query.
"""
import ast
import asyncio
import logging
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import fusion_core  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "agents", "agents.py")


def _load(answers, delays=None, bound=0.5):
    text = open(SRC, encoding="utf-8").read()
    fn = next(n for n in ast.parse(text).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "agent_rag_retrieve")
    calls = []

    async def fake_cap(name, text="", dataset_id="", top_k=4, **kw):
        calls.append(dataset_id)
        await asyncio.sleep((delays or {}).get(dataset_id, 0))
        return {"results": answers.get(dataset_id, [])}

    ns = {"asyncio": asyncio, "log": logging.getLogger("t"), "_fusion": fusion_core,
          "_call_registered_cap": fake_cap, "_RAG_DATASET_TIMEOUT_S": bound,
          "AgentRecord": object, "List": list, "Dict": dict, "Any": object}
    exec(compile(ast.get_source_segment(text, fn), SRC, "exec"), ns)
    return ns["agent_rag_retrieve"], calls


def _rec(*fabric):
    return types.SimpleNamespace(name="a", rag_dataset="own", rag_inject_limit=4,
                                 knowledge_sources=[{"type": "web", "target": "x"}]
                                 + [{"type": "fabric", "target": d} for d in fabric])


def _rows(prefix, n, score):
    return [{"id": f"{prefix}{i}", "text": f"{prefix} text {i}", "score": score} for i in range(n)]


def test_rank_fusion_not_raw_scores():
    # "big" reports scores on a far larger scale; raw sorting gave it every slot.
    fn, _ = _load({"own": _rows("o", 3, 0.4), "big": _rows("b", 3, 40.0)})
    out = asyncio.run(fn(_rec("big"), "q", limit=4))
    assert [r["record_id"] for r in out] == ["o0", "b0", "o1", "b1"]


def test_a_slow_dataset_costs_only_its_own_hits():
    fn, calls = _load({"own": _rows("o", 2, 0.5), "slow": _rows("s", 2, 0.9)},
                      delays={"slow": 2.0}, bound=0.2)
    out = asyncio.run(fn(_rec("slow"), "q", limit=4))
    assert sorted(calls) == ["own", "slow"]
    assert [r["record_id"] for r in out] == ["o0", "o1"]


def test_datasets_run_concurrently_and_dedupe():
    fn, calls = _load({"own": _rows("o", 1, 0.5), "d1": _rows("a", 1, 0.5)},
                      delays={"own": 0.3, "d1": 0.3}, bound=1.0)
    loop_t = asyncio.run(_timed(fn(_rec("d1", "d1", "own"), "q")))
    assert sorted(calls) == ["d1", "own"]
    assert loop_t < 0.55


def test_empty_query_and_text_fallback():
    fn, calls = _load({"own": [{"id": "w", "summary": "", "text": "fetched page"}]})
    assert asyncio.run(fn(_rec(), "  ")) == [] and calls == []
    assert asyncio.run(fn(_rec(), "q"))[0]["text"] == "fetched page"


async def _timed(coro):
    t = asyncio.get_running_loop().time()
    await coro
    return asyncio.get_running_loop().time() - t
