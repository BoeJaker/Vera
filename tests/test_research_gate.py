"""Research generation goes through the shared generation slot, origin header and request log (RF-50).

researcher_api is a large module with app-level side effects, so its three gate helpers
are lifted out of the source with ast and run against a fake orchestrator.
"""
import ast
import asyncio
import os
import sys
import time
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "research", "researcher_api.py")
NAMES = ("_vera_orch", "_research_origin_headers", "_research_gate")


def _load():
    src = open(SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    parts = [ast.get_source_segment(src, n) for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES]
    # decorators are not part of the segment; re-apply asynccontextmanager to the gate
    code = "\n\n".join(parts).replace("async def _research_gate", "@asynccontextmanager\nasync def _research_gate")
    import logging
    ns = {"sys": sys, "asyncio": asyncio, "time": time, "uuid": uuid, "datetime": datetime, "timezone": timezone,
          "asynccontextmanager": asynccontextmanager, "log": logging.getLogger("t"),
          "_effective_timeout": lambda inst, t: t, "OllamaInstance": object}
    exec(compile(code, SRC, "exec"), ns)
    return ns


def _fake_orch(events, fail=False):
    m = types.ModuleType("Vera.vera.capability_orchestration")

    @asynccontextmanager
    async def _ollama_slot(iid, timeout=None):
        if fail:
            raise Exception("queue timeout on %s" % iid)
        events.append(("acquire", iid))
        try:
            yield None
        finally:
            events.append(("release", iid))

    m._ollama_slot = _ollama_slot
    m._ollama_log_append = lambda e: events.append(("log", e))
    m.vera_origin_header = lambda job_type="", req_id="", cap="": {"X-Vera-Origin": "prod|%s|%s|%s" % (job_type, req_id, cap)}
    return m


class Inst:
    vera_iid = "gpu-250"; model = "m"; name = "n"; tier = "writer"


def _run(ns, inst, body=None):
    async def go():
        async with ns["_research_gate"](inst, "job1", 42):
            if body: body()
    asyncio.run(go())


def test_slot_taken_released_and_logged(monkeypatch):
    ev = []
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", _fake_orch(ev))
    ns = _load()
    _run(ns, Inst())
    kinds = [e[0] for e in ev]
    assert kinds == ["acquire", "release", "log"]
    entry = ev[-1][1]
    assert entry["job_type"] == "research" and entry["status"] == "done" and entry["instance"] == "gpu-250"
    assert entry["prompt_chars"] == 42 and "elapsed_s" in entry


def test_error_inside_is_logged_and_slot_released(monkeypatch):
    ev = []
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", _fake_orch(ev))
    ns = _load()
    with pytest.raises(RuntimeError):
        _run(ns, Inst(), body=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ("release", "gpu-250") in ev and ev[-1][1]["status"] == "error"


def test_queue_timeout_fails_open_and_says_so(monkeypatch):
    ev = []
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", _fake_orch(ev, fail=True))
    ns = _load()
    _run(ns, Inst())
    assert ev[-1][0] == "log" and ev[-1][1]["gate"] == "timeout_proceeded"


def test_standalone_is_a_noop(monkeypatch):
    monkeypatch.delitem(sys.modules, "Vera.vera.capability_orchestration", raising=False)
    monkeypatch.delitem(sys.modules, "capability_orchestration", raising=False)
    ns = _load()
    _run(ns, Inst())                      # no orchestrator: nothing to acquire or log
    assert ns["_research_origin_headers"]("j") == {}


def test_hand_configured_node_is_not_gated(monkeypatch):
    ev = []
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", _fake_orch(ev))
    ns = _load()
    inst = Inst(); inst.vera_iid = ""
    _run(ns, inst)
    assert [e[0] for e in ev] == ["log"]


def test_stream_ollama_uses_the_gate_and_origin_header():
    src = open(SRC, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "stream_ollama")
    dumped = ast.dump(fn)
    assert "_research_gate" in dumped and "_research_origin_headers" in dumped
