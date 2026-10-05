"""context.recall feeds the retrieval.timing window (four-minds FM-7.0)."""
import ast
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import retrieval_timing_core as rtc  # noqa: E402

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "fabric", "context.py")
TEXT = open(SRC, encoding="utf-8").read()


def _recorder():
    fn = next(n for n in ast.parse(TEXT).body
              if isinstance(n, ast.FunctionDef) and n.name == "_recall_timing_record")
    ns = {"Dict": dict, "Any": object, "List": list}
    exec(compile(ast.get_source_segment(TEXT, fn), SRC, "exec"), ns)
    return ns["_recall_timing_record"]


@pytest.fixture
def window(monkeypatch):
    w = rtc.Window(10)
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory", types.SimpleNamespace(RETRIEVAL_TIMING=w))
    return w


def test_each_source_and_how_it_ended(window):
    rec = _recorder()
    rec({"entities": ([], []), "worldview": RuntimeError("x")},
        {"entities": 120.5, "worldview": 30.0, "related_qa": "late", "x": "cached"}, 1500.0)
    s = window.summary()["context.recall"]["stages"]
    assert s["entities"]["outcomes"] == {"ok": 1} and s["worldview"]["outcomes"] == {"error": 1}
    assert s["related_qa"]["outcomes"] == {"late": 1} and s["related_qa"]["p50"] == 1500.0
    assert s["x"]["outcomes"] == {"cached": 1}


def test_whole_answer_cache_hits_are_counted(window):
    _recorder()({}, {}, 0.0, cached_whole=True)
    assert window.summary()["context.recall"]["stages"]["answer_cache"]["outcomes"] == {"cached": 1}


def test_no_window_no_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "Vera.vera.fabric.memory", types.SimpleNamespace())
    monkeypatch.delitem(sys.modules, "memory", raising=False)
    _recorder()({"a": 1}, {"a": 5.0}, 5.0)


def test_both_returns_are_recorded():
    seg = TEXT[TEXT.index("async def cap_context_recall("):TEXT.index("async def cap_context_recall_fabric(")]
    assert seg.count("_recall_timing_record(") == 2
