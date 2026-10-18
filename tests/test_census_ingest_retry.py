"""Roadmap E3 (compute-roles, done 11 Oct 2026): a census row whose ingest finds prod down is queued and
retried, instead of one restart switching ingest off for the rest of the census."""
import ast
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def _load(answers):
    """_ingest_once / flush_pending / ingest_row from run_census.py with a fake curl that answers in
    turn from `answers` (None = no response)."""
    tree = ast.parse(SRC)
    body = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in
                                     ("_ingest_once", "flush_pending", "ingest_row"))
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_INGEST" for t in n.targets))]
    calls = []

    def curl(args, timeout=0):
        calls.append(json.loads(args[-1])["arguments"]["row"]["id"])
        a = answers.pop(0) if answers else None
        return "" if a is None else json.dumps(a)
    ns = {"json": json, "curl": curl, "log": lambda *a: None, "BASE": "https://x", "SESSION_ID": "s"}
    exec(compile(ast.Module(body=body, type_ignores=[]), "rc", "exec"), ns)
    return ns, calls


OK = {"content": {"ok": True, "added": True}}


def test_down_rows_queue_and_flush_after_the_next_good_ingest():
    ns, calls = _load([None, None, OK, OK, OK])
    for i in range(3):
        ns["ingest_row"]({"id": "g%d" % i}, "default", "run1")
    assert calls == ["g0", "g1", "g2", "g0", "g1"]
    assert ns["_INGEST"]["pending"] == [] and ns["_INGEST"]["unavailable"] is False


def test_only_a_missing_capability_turns_ingest_off():
    ns, calls = _load([{"detail": "Unknown capability evolve.result.ingest"}])
    ns["ingest_row"]({"id": "a"}, "default", "run1")
    ns["ingest_row"]({"id": "b"}, "default", "run1")
    assert ns["_INGEST"]["unavailable"] is True and calls == ["a"]


def test_end_of_census_flushes_the_queue():
    assert 'if _INGEST["pending"] and not _INGEST["unavailable"]:\n        flush_pending()' in SRC
