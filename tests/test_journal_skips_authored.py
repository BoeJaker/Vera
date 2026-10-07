"""A code-authoring step's journal record needs no model call (four-minds Q2, 2026-10-05).

finalize replaces such a step's summary with a fixed sentence ("Authored `x`; its syntax was
VERIFIED ..."), so the journal model was distilling a template. The file is the one fact later
steps need; it is recorded directly.
"""
import ast
import asyncio
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "dag", "dag_workshop_capabilities.py")
TEXT = open(SRC, encoding="utf-8").read()


def _extract():
    fn = next(n for n in ast.parse(TEXT).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v6_journal_extract")

    async def no_model(*a, **k):
        raise AssertionError("the journal called the model for an authored step")

    ns = {"Dict": dict, "Any": object, "List": list, "Optional": object,
          "_v6_journal_derive": lambda res: {"files": ["/workspace/old.txt"], "tools": ["code.author"]},
          "now_iso": lambda: "2026-10-06T00:00:00Z", "_safe_ollama_generate_dw": no_model}
    exec(compile(ast.get_source_segment(TEXT, fn), SRC, "exec"), ns)
    return ns["_v6_journal_extract"]


def test_an_authored_step_is_recorded_without_the_model():
    res = {"ok": True, "met": True, "finalized": True, "authored_path": "/workspace/app.py",
           "summary": "Authored `/workspace/app.py`; its syntax was VERIFIED by the code author " * 3}
    rec = asyncio.run(_extract()({"id": 2, "title": "write app"}, res, "goal",
                                 model="m", instance_id="", prefer_gpu=False))
    assert rec["files"][0] == "/workspace/app.py" and "/workspace/old.txt" in rec["files"]
    assert rec["source"] == "authored" and "authored /workspace/app.py" in rec["note"]


def test_finalize_marks_the_authored_path():
    assert 'res["authored_path"] = _authored' in TEXT
