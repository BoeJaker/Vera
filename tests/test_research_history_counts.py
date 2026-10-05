"""Research history counts each job's citations in one pass (2026-10-05: /api/history took 24.7 s,
one 10 000-row decode per job on the event loop)."""
import ast
import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical

SRC = os.path.join(ROOT, "vera", "research", "research_fabric.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)


def _ns():
    ns = {"json": json, "asyncio": asyncio, "time": __import__("time"), "log": __import__("logging").getLogger("t"),
          "Dict": dict, "List": list, "Any": object, "Optional": object}
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in (
                "_ts_key", "_filter_rows", "_count_rows_by", "_query_by_filter", "_counts_by_parent", "_count_by_parent"):
            exec(compile(ast.get_source_segment(TEXT, n), SRC, "exec"), ns)
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(getattr(x, "id", "") in ("_PARSE_OFF_LOOP_ROWS", "_COUNTS_CACHE", "_COUNTS_TTL_S") for x in targets):
                exec(compile(ast.get_source_segment(TEXT, n), SRC, "exec"), ns)
    return ns


def _rows():
    def row(rid, job, created, logical=None):
        data = {"job_id": job, "created_at": created}
        if logical is not None:
            data["id"] = logical
        return {"id": rid, "data": json.dumps(data), "created_at": created}
    return [row("r1", "j1", 3, "c1"), row("r2", "j1", 2, "c1"),        # same logical citation twice
            row("r3", "j1", 1, "c2"), row("r4", "j2", 5, "c3"),
            row("r5", "j2", 4, ""), row("r6", "j3", 1, "c9"),
            {"id": "r7", "data": "not json", "created_at": 0}]


class _Fab:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    async def _sqlite_query(self, dataset_id="", limit=50, offset=0):
        self.calls += 1
        return list(self.rows)


def test_one_pass_counts_equal_the_old_per_job_counts():
    ns = _ns()
    fab = _Fab(_rows())
    ns["_fabric"] = lambda: fab
    new = ns["_count_rows_by"](_rows(), "job_id")
    for job in ("j1", "j2", "j3", "nope"):
        old = asyncio.run(ns["_count_by_parent"]("ds", "job_id", job))
        assert new.get(job, 0) == old, (job, new.get(job, 0), old)
    assert new["j1"] == 2 and new["j2"] == 2 and new["j3"] == 1


def test_counts_are_read_once_and_cached():
    ns = _ns()
    fab = _Fab(_rows())
    ns["_fabric"] = lambda: fab
    a = asyncio.run(ns["_counts_by_parent"]("ds", "job_id"))
    b = asyncio.run(ns["_counts_by_parent"]("ds", "job_id"))
    assert a == b and fab.calls == 1


def test_big_batches_still_filter_the_same():
    ns = _ns()
    rows = _rows() * 60                     # over the off-loop threshold
    fab = _Fab(rows)
    ns["_fabric"] = lambda: fab
    out = asyncio.run(ns["_query_by_filter"]("ds", {"job_id": "j1"}, limit=50))
    assert sorted(r["id"] for r in out) == ["c1", "c2"]


def test_history_uses_one_count_pass():
    fn = next(n for n in ast.walk(TREE) if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_history")
    seg = ast.get_source_segment(TEXT, fn)
    assert "_counts_by_parent(DATASET_CITATIONS" in seg and "_count_by_parent(" not in seg
