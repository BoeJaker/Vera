"""Research's memory source reads the record inside memory.search results (RF-80).

researcher_api has app-level side effects, so gather_memory is lifted out of the source
with ast and run against a fake memory module.
"""
import ast
import asyncio
import logging
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "research", "researcher_api.py")


class Citation:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _load():
    src = open(SRC, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "gather_memory")
    ns = {"sys": sys, "log": logging.getLogger("t"), "Citation": Citation, "uuid": __import__("uuid"),
          "time": __import__("time")}
    exec(compile(ast.get_source_segment(src, fn), SRC, "exec"), ns)
    return ns["gather_memory"]


def _fake_memory(results):
    class Mem:
        async def search(self, query, limit=20):
            return results
    m = types.ModuleType("memory")
    m.MEMORY = Mem()
    return m


def test_wrapped_results_become_citations(monkeypatch):
    rows = [{"record": {"id": "m1", "text": "Redis moved to SSPL", "category": "chat",
                        "tags": ["redis"], "created_at": "2026-10-01"}, "score": 0.42},
            {"record": {"id": "m2", "text": "", "full_text": ""}, "score": 0.1}]
    monkeypatch.setitem(sys.modules, "memory", _fake_memory(rows))
    cits = asyncio.run(_load()("redis license"))
    assert len(cits) == 1
    assert "Redis moved to SSPL" in (getattr(cits[0], "snippet", "") + getattr(cits[0], "full_text", ""))


def test_plain_dicts_still_work(monkeypatch):
    rows = [{"id": "m3", "text": "plain dict row", "category": "notes"}]
    monkeypatch.setitem(sys.modules, "memory", _fake_memory(rows))
    assert len(asyncio.run(_load()("x"))) == 1
