"""discover_recall must not run its synchronous SQLite reads on the event loop (RF-02).

web.search calls discover_recall beside every live search; its LIKE scans (and a
10 s SQLite busy_timeout) used to run inside the coroutine and stall the loop.
Source-level guard: the module imports the fabric, so it is checked by AST.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "fabric", "discovery.py")


def _defs():
    with open(SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_reads_live_in_a_plain_function():
    d = _defs()
    sync = d["_discover_recall_sync"]
    assert isinstance(sync, ast.FunctionDef)
    assert not any(isinstance(n, ast.Await) for n in ast.walk(sync))
    assert "execute" in ast.dump(sync)


def test_the_coroutine_only_hands_off_to_a_thread():
    d = _defs()
    coro = d["discover_recall"]
    assert isinstance(coro, ast.AsyncFunctionDef)
    dumped = ast.dump(coro)
    assert "to_thread" in dumped and "_discover_recall_sync" in dumped
    assert "execute" not in dumped
