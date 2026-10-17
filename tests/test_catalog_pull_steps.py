"""DA-04c (c): a model pull is steps of the call that started it - the run element on the desk follows its trace from
'queued' through the download to 'done' / 'error' (vera/catalog/catalog_capabilities.py _pull_step). Offline: the
function is taken from the source and run against a stand-in emit_step; the wiring is checked in the source."""
import ast
import asyncio
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vera", "catalog", "catalog_capabilities.py")
SRC = open(SRC_PATH, encoding="utf-8").read()


def _pull_step_fn(sent):
    tree = ast.parse(SRC)
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_pull_step")
    mod = ast.Module(body=[node], type_ignores=[])
    fake = types.ModuleType("Vera.vera.run_steps")

    async def emit_step(cap, trace_id, stage, **kw):
        sent.append(dict(cap=cap, trace_id=trace_id, stage=stage, **kw))
    fake.emit_step = emit_step
    saved = {k: sys.modules.get(k) for k in ("Vera", "Vera.vera", "Vera.vera.run_steps")}
    sys.modules.setdefault("Vera", types.ModuleType("Vera"))
    sys.modules.setdefault("Vera.vera", types.ModuleType("Vera.vera"))
    sys.modules["Vera.vera.run_steps"] = fake
    ns = {}
    exec(compile(mod, SRC_PATH, "exec"), ns)
    return ns["_pull_step"], saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_a_pull_is_steps_of_its_call():
    sent = []
    step, saved = _pull_step_fn(sent)
    try:
        rec = {"id": "p1", "model": "qwen3:1.7b", "instance_id": "gpu-1", "node_label": "GPU 1", "_trace": "t-1",
               "total_bytes": 0, "completed_bytes": 0, "speed_bps": 0, "eta_s": None, "pct": 0.0}
        asyncio.run(step(rec, "queued"))
        rec.update(total_bytes=1000, completed_bytes=400, speed_bps=12_000_000, eta_s=7, pct=40.0)
        asyncio.run(step(rec, "downloading"))
        rec.update(completed_bytes=1000, pct=100.0, ok=True)
        asyncio.run(step(rec, "done"))
        rec.update(error="node unreachable")
        asyncio.run(step(rec, "error"))
    finally:
        _restore(saved)
    assert [s["stage"] for s in sent] == ["queued", "downloading", "done", "error"]
    assert all(s["cap"] == "catalog.pull.start" and s["trace_id"] == "t-1" and s["pull_id"] == "p1" for s in sent)
    assert sent[0]["done"] is None and sent[0]["total"] is None          # no size yet: no bar
    assert sent[1]["done"] == 400 and sent[1]["total"] == 1000 and "12.0 MB/s" in sent[1]["text"] and "7 s left" in sent[1]["text"]
    assert sent[2]["model"] == "qwen3:1.7b" and sent[2]["total_bytes"] == 1000
    assert "node unreachable" in sent[3]["text"] and "node unreachable" not in sent[2]["text"]


def test_no_call_following_no_steps():
    sent = []
    step, saved = _pull_step_fn(sent)
    try:
        asyncio.run(step({"id": "p2", "model": "m", "_trace": ""}, "queued"))
        asyncio.run(step({"id": "p3", "model": "m"}, "queued"))
    finally:
        _restore(saved)
    assert sent == []


def test_the_pull_carries_its_call_through():
    # the cap passes its trace; the record keeps it (private: never in the answer); steps at start, while it downloads,
    # at the end; a call joining a running pull follows it from there
    assert "rec = await _pull_start(model.strip(), instance_id, trace_id=trace_id)" in SRC
    assert '"_trace": trace_id or "",' in SRC and 'return {k: v for k, v in rec.items() if not k.startswith("_")}' in SRC
    assert 'await _pull_step(rec, "queued")' in SRC and 'await _pull_step(rec, "downloading")   # its call\'s run item' in SRC
    assert 'await _pull_step(rec, "done" if rec.get("ok") else "error")' in SRC
    assert 'rec["_trace"] = trace_id\n                await _pull_step(rec, "downloading")' in SRC
