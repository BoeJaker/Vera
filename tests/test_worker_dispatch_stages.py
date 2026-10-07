"""Node workers receive node-safe work, in stages (user, 2026-09-28: "ensure
they can receive a full set of tasks - could be done in stages and the stages
defined as various types of vera worker deployment").

Until then nothing was sent: no capability declared mode="distributed", which
had been switched off because results went missing - every process read the
shared result stream through ONE consumer group, so each result reached one
reader, often not the process waiting for it. Results now go to the asking
process's own stream.

Pure: names in, decisions out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.workers import worker_placement_core as W   # noqa: E402

pytestmark = pytest.mark.critical

ALL_FREE = {c: 1 for c in W.CLASSES}


def _d(cap, stage, **kw):
    args = dict(stage=stage, free=ALL_FREE, is_worker=False, is_sandbox=False, args_ok=True)
    args.update(kw)
    return W.offload_decision(cap, env={}, **args)


def test_stages_are_cumulative_deployment_types():
    names = [s["name"] for s in W.STAGES]
    assert names == ["idle", "nlp", "compute", "llm", "media", "full"]
    prev = set()
    for s in W.STAGES:
        assert prev <= set(s["classes"]), s["name"]
        prev = set(s["classes"])
    assert set(W.STAGES[-1]["classes"]) == set(W.CLASSES)


def test_what_each_stage_sends():
    assert not _d("nlp.ner", 0)[0]
    assert _d("nlp.ner", 1)[0] and not _d("text.summarize_x", 1)[0]
    assert _d("math.add", 2)[0] and not _d("llm.generate", 2)[0]
    assert _d("llm.generate", 3)[0] and not _d("stt.transcribe", 3)[0]
    assert _d("stt.transcribe", 4)[0]
    assert _d("stt.transcribe", 5)[0] and _d("llm.generate", 5)[0]


def test_never_host_bound_work():
    for cap in ("evolve.pipeline.promote", "docker.run", "nlp.config.set", "image.generate"):
        ok, why = _d(cap, 5)
        assert not ok, cap


def test_only_to_a_free_worker_else_the_host_runs_it():
    ok, why = _d("nlp.ner", 1, free={"nlp": 0})
    assert not ok and "no free" in why
    assert _d("nlp.ner", 1, free={"nlp": 1})[0]


def test_never_from_a_worker_a_sandbox_or_with_unsendable_args():
    assert not _d("nlp.ner", 5, is_worker=True)[0]
    assert not _d("nlp.ner", 5, is_sandbox=True)[0]
    assert not _d("nlp.ner", 5, args_ok=False)[0]
    assert not _d("nlp.ner", 5, exclude=["nlp"])[0]
    assert not _d("nlp.ner", 5, exclude=["nlp.ner"])[0]


def test_free_workers_counts_idle_and_subtracts_in_flight():
    ws = [{"role": "node-worker", "status": "idle", "classes": '["general", "nlp"]'},
          {"role": "node-worker", "status": "idle", "classes": ["nlp"]},
          {"role": "node-worker", "status": "running:nlp.ner", "classes": ["nlp"]},
          {"role": "host", "status": "idle", "classes": ""}]
    assert W.free_workers(ws) == {"general": 1, "nlp": 2}
    assert W.free_workers(ws, {"nlp": 2}) == {"general": 1, "nlp": 0}


def test_results_come_back_to_the_asking_process():
    a, b = W.reply_stream("LLM-100-aa"), W.reply_stream("LLM-200-bb")
    assert a != b and a.startswith("vera:results:p:")
    assert W.REPLY_TTL_S > 0


def test_result_listeners_wait_for_redis_instead_of_exiting():
    """Both listeners start with lifespan, before _connect_backends has a
    connection. An early `if not REDIS: return` made them exit at once, so on
    prod no dispatched result was ever read (no "Result listener started" line
    at any boot) - the real cause of the old stuck-pending jobs."""
    import ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "vera", "capability_orchestration.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for fn in ("result_listener", "reply_listener"):
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == fn)
        assert "while REDIS is None" in ast.get_source_segment(src, node), fn
        for stmt in node.body[:4]:     # no early exit before the loop
            assert not (isinstance(stmt, ast.If)
                        and any(isinstance(b, ast.Return) for b in stmt.body)), fn
